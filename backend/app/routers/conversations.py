"""Admin conversation endpoints: browse conversations and handle human handoffs."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import AdminAuth, AuthContext
from app.channels.outbound import StaffMessageSender, get_staff_message_sender
from app.db import get_sessionmaker
from app.handoff import add_staff_reply, end_handoff
from app.ingestion.queue import JobQueue, get_job_queue
from app.models import Conversation, Message
from app.schemas import MAX_CHAT_MESSAGE_CHARS, ConversationDetail, ConversationOut, MessageOut

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _message_counts(auth: AuthContext, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await auth.db.execute_sql(
        text(
            "SELECT conversation_id, count(*) AS n FROM messages "
            "WHERE tenant_id = :tenant_id AND conversation_id = ANY(:ids) GROUP BY conversation_id"
        ),
        {"ids": ids},
    )
    return {row.conversation_id: row.n for row in rows}


QueueDep = Annotated[JobQueue, Depends(get_job_queue)]
SenderDep = Annotated[StaffMessageSender, Depends(get_staff_message_sender)]
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
Status = Literal["ai", "waiting_human", "human", "resolved"]


class StaffReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)


class StaffReplyOut(BaseModel):
    message: MessageOut
    conversation: ConversationOut
    delivered: bool  # reached the customer's channel (the widget fetches it itself)


PREVIEW_CHARS = 140


async def _summaries(auth: AuthContext, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """The newest message of each conversation and its newest customer message."""
    if not ids:
        return {}
    rows = await auth.db.execute_sql(
        text(
            "SELECT DISTINCT ON (conversation_id) conversation_id, role, content, created_at "
            "FROM messages WHERE tenant_id = :tenant_id AND conversation_id = ANY(:ids) "
            "ORDER BY conversation_id, created_at DESC, role DESC"
        ),
        {"ids": ids},
    )
    last = {row.conversation_id: row for row in rows}
    rows = await auth.db.execute_sql(
        text(
            "SELECT conversation_id, max(created_at) AS at FROM messages "
            "WHERE tenant_id = :tenant_id AND conversation_id = ANY(:ids) AND role = 'user' "
            "GROUP BY conversation_id"
        ),
        {"ids": ids},
    )
    customer = {row.conversation_id: row.at for row in rows}
    return {cid: {"last": last.get(cid), "customer_at": customer.get(cid)} for cid in ids}


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    auth: AdminAuth,
    limit: int = Query(50, ge=1, le=200),
    status: Status | None = None,
    q: str | None = Query(None, min_length=1, max_length=200),
):
    """Newest first; ?status=waiting_human lists the customers waiting for a person; ?q=
    searches customer names, visitor ids and message text."""
    query = auth.db.select(Conversation)
    if status is not None:
        query = query.where(Conversation.status == status)
    if q:
        pattern = _like(q.strip())
        matching = (
            auth.db.select(Message)
            .with_only_columns(Message.conversation_id)
            .where(Message.content.ilike(pattern))
        )
        query = query.where(
            Conversation.customer_name.ilike(pattern)
            | Conversation.visitor_id.ilike(pattern)
            | Conversation.id.in_(matching)
        )
    conversations = await auth.db.scalars(
        query.order_by(Conversation.updated_at.desc()).limit(limit)
    )
    ids = [c.id for c in conversations]
    counts = await _message_counts(auth, ids)
    summaries = await _summaries(auth, ids)
    out = []
    for c in conversations:
        summary = summaries[c.id]
        last = summary["last"]
        customer_at = summary["customer_at"]
        out.append(
            ConversationOut.model_validate(c).model_copy(
                update={
                    "message_count": counts.get(c.id, 0),
                    "last_message_preview": last.content[:PREVIEW_CHARS] if last else None,
                    "last_message_role": last.role if last else None,
                    "last_message_at": last.created_at if last else None,
                    "unread": customer_at is not None
                    and (c.staff_read_at is None or customer_at > c.staff_read_at),
                }
            )
        )
    return out


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, auth: AdminAuth):
    conversation = await auth.db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    messages = await auth.db.scalars(
        auth.db.select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.role.desc())
    )
    return ConversationDetail(
        **ConversationOut.model_validate(conversation).model_dump(exclude={"message_count"}),
        message_count=len(messages),
        messages=[MessageOut.model_validate(m) for m in messages],
    )


async def _conversation(auth: AuthContext, conversation_id: uuid.UUID) -> Conversation:
    conversation = await auth.db.get(Conversation, conversation_id, for_update=True)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


async def _out(auth: AuthContext, conversation: Conversation) -> ConversationOut:
    counts = await _message_counts(auth, [conversation.id])
    return ConversationOut.model_validate(conversation).model_copy(
        update={"message_count": counts.get(conversation.id, 0)}
    )


@router.post("/{conversation_id}/read", response_model=ConversationOut)
async def mark_read(conversation_id: uuid.UUID, auth: AdminAuth):
    """Staff opened the conversation: it is no longer unread."""
    conversation = await _conversation(auth, conversation_id)
    conversation.staff_read_at = func.now()
    await auth.db.commit()
    return await _out(auth, await _conversation(auth, conversation_id))


@router.post("/{conversation_id}/messages", response_model=StaffReplyOut, status_code=201)
async def post_staff_reply(
    conversation_id: uuid.UUID,
    body: StaffReplyIn,
    auth: AdminAuth,
    send: SenderDep,
    sessionmaker: SessionmakerDep,
):
    """Reply as a team member. The assistant stays silent until the chat is handed back."""
    conversation = await _conversation(auth, conversation_id)
    via = {"via": "admin_api", "api_key_id": str(auth.api_key_id)}
    message = await add_staff_reply(auth.db, conversation, body.text, via)
    await auth.db.commit()
    delivered = await send(sessionmaker, conversation, body.text)
    return StaffReplyOut(
        message=MessageOut.model_validate(message),
        conversation=await _out(auth, conversation),
        delivered=delivered,
    )


async def _end(auth: AuthContext, queue: JobQueue, conversation_id: uuid.UUID, new: str):
    conversation = await _conversation(auth, conversation_id)
    delivery = await end_handoff(auth.db, conversation, new)
    await auth.db.commit()
    if delivery is not None:
        await queue.enqueue_webhook(auth.tenant_id, delivery)
    return await _out(auth, conversation)


@router.post("/{conversation_id}/hand-back", response_model=ConversationOut)
async def hand_back(conversation_id: uuid.UUID, auth: AdminAuth, queue: QueueDep):
    """The assistant answers this customer again."""
    return await _end(auth, queue, conversation_id, "ai")


@router.post("/{conversation_id}/resolve", response_model=ConversationOut)
async def resolve(conversation_id: uuid.UUID, auth: AdminAuth, queue: QueueDep):
    """Close the conversation; if the customer writes again, the assistant answers."""
    return await _end(auth, queue, conversation_id, "resolved")
