"""Endpoints the dashboard needs beyond the existing admin API (admin keys only):
the overview, knowledge gaps and answering them, run traces, settings updates and webhook test
events. Every query goes through the tenant-scoped auth.db, so RLS and the app-level filter
both apply."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func

from app import analytics
from app.auth import AdminAuth
from app.chat.settings import TenantChatSettings
from app.ingestion.documents import UploadRejected, create_upload_document
from app.ingestion.queue import JobQueue, get_job_queue
from app.ingestion.storage import FileStorage, get_storage
from app.models import Message, WebhookEndpoint
from app.schemas import DocumentOut, TenantOut
from app.webhooks.events import record_event

router = APIRouter(tags=["dashboard"])

QueueDep = Annotated[JobQueue, Depends(get_job_queue)]
StorageDep = Annotated[FileStorage, Depends(get_storage)]


# --- overview ----------------------------------------------------------------------------------


@router.get("/analytics/overview")
async def analytics_overview(auth: AdminAuth) -> dict[str, Any]:
    """Conversations today and over the last 7 days, outcomes, handoff rate, median time to
    first token, tokens and estimated cost, and the newest knowledge gaps."""
    tenant = await auth.db.tenant()
    settings = TenantChatSettings.from_tenant(tenant.name, tenant.settings)
    return await analytics.overview(auth.db, settings.timezone)


# --- knowledge gaps ----------------------------------------------------------------------------


class GapGroupOut(BaseModel):
    question: str
    count: int
    last_asked_at: datetime
    examples: list[str]
    message_ids: list[uuid.UUID]
    conversation_ids: list[uuid.UUID]


class GapAnswerIn(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=3, max_length=2000)
    message_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class GapAnswerOut(BaseModel):
    document: DocumentOut
    closed: int


@router.get("/knowledge-gaps", response_model=list[GapGroupOut])
async def knowledge_gaps(auth: AdminAuth, limit: int = Query(50, ge=1, le=200)):
    """Questions the assistant could not answer, grouped by similarity, newest first."""
    groups = analytics.group_gaps(await analytics.open_gaps(auth.db))
    return [
        GapGroupOut(
            question=g.question, count=g.count, last_asked_at=g.last_asked_at,
            examples=g.examples, message_ids=g.message_ids, conversation_ids=g.conversation_ids,
        )
        for g in groups[:limit]
    ]  # fmt: skip


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:40] or "answer"


@router.post("/knowledge-gaps/answer", response_model=GapAnswerOut, status_code=201)
async def answer_gap(body: GapAnswerIn, auth: AdminAuth, storage: StorageDep, queue: QueueDep):
    """Close a gap: the answer becomes a short knowledge entry (ingested like any upload), and
    the grouped no_answer replies leave the gaps list."""
    content = f"# {body.question.strip()}\n\n{body.answer.strip()}\n".encode()
    try:
        document, created = await create_upload_document(
            auth.db, storage, f"answer-{_slug(body.question)}.md", content,
            title=body.question.strip()[:120],
        )  # fmt: skip
    except UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    if created:
        await queue.enqueue_ingest(auth.tenant_id, document.id)
    messages = await auth.db.scalars(
        auth.db.select(Message).where(
            Message.id.in_(body.message_ids),
            Message.outcome == "no_answer",
            Message.gap_closed_at.is_(None),
        )
    )
    for message in messages:
        message.gap_closed_at = func.now()
    await auth.db.commit()
    return GapAnswerOut(document=DocumentOut.model_validate(document), closed=len(messages))


# --- run traces --------------------------------------------------------------------------------


@router.get("/messages/{message_id}/trace")
async def message_trace(message_id: uuid.UUID, auth: AdminAuth) -> dict[str, Any]:
    """Everything recorded for one assistant reply: the customer's question, the searches the
    model wrote (mode, threshold decision, chunks with scores), tool calls with arguments and
    results, the model that answered and any failover, timings and tokens."""
    message = await auth.db.get(Message, message_id)
    if message is None or message.role != "assistant":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    question = await auth.db.get(Message, message.in_reply_to) if message.in_reply_to else None
    record = message.retrieval or {}
    searches = [
        {
            **s,
            "decision": "passed" if s.get("relevant") else "blocked",
            "reason": (
                "a strong keyword match" if s.get("strong_keyword_match")
                else f"top similarity {s.get('top_similarity')} vs threshold {s.get('threshold')}"
            ),
        }
        for s in record.get("searches", [])
    ]  # fmt: skip
    return {
        "message_id": message.id,
        "conversation_id": message.conversation_id,
        "created_at": message.created_at,
        "question": question.content if question else None,
        "reply": message.content,
        "outcome": message.outcome,
        "model": message.model,
        "failovers": record.get("failovers", []),
        "fallback": bool(record.get("failovers")),
        "searches": searches,
        "tools": record.get("tools", []),
        "citations": message.citations,
        "checks": record.get("checks", {}),
        "timings_ms": message.timings,
        "tokens": {"prompt": message.prompt_tokens, "completion": message.completion_tokens},
        "error": message.error,
    }


# --- settings ----------------------------------------------------------------------------------

EDITABLE = set(TenantChatSettings.model_fields)


@router.patch("/tenant/settings", response_model=TenantOut)
async def update_settings(body: dict[str, Any], auth: AdminAuth):
    """Change some settings; the others are kept. Unknown or invalid fields are rejected with
    every problem listed, rather than silently ignored."""
    unknown = sorted(set(body) - EDITABLE)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown settings: {', '.join(unknown)}")
    tenant = await auth.db.tenant()
    merged = {**(tenant.settings or {}), **body}
    try:
        validated = TenantChatSettings.model_validate(merged)
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        raise HTTPException(status_code=422, detail=problems) from None
    tenant.settings = {k: v for k, v in validated.model_dump(mode="json").items() if k in merged}
    await auth.db.commit()
    return await auth.db.tenant()


# --- webhook test event ------------------------------------------------------------------------


@router.post("/webhooks/test", status_code=202)
async def send_test_webhook(auth: AdminAuth, queue: QueueDep) -> dict[str, Any]:
    """Queue a signed webhook.test delivery to the configured endpoint (see the delivery log)."""
    endpoint = await auth.db.scalar(
        auth.db.select(WebhookEndpoint).where(WebhookEndpoint.enabled.is_(True))
    )
    if endpoint is None:
        raise HTTPException(status_code=409, detail="Set a webhook URL first")
    delivery = await record_event(
        auth.db, "webhook.test", {"message": "Test event from the dashboard"}
    )
    await auth.db.commit()
    assert delivery is not None
    await queue.enqueue_webhook(auth.tenant_id, delivery)
    return {"delivery_id": delivery, "url": endpoint.url}
