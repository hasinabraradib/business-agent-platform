"""POST /v1/chat: customer chat, for widget keys (public, origin-checked) and admin keys."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import AuthContext, authenticate
from app.channels.pipeline import AlreadyInFlight, ConversationNotFound, accept
from app.chat.deps import get_chat_service, get_rate_limiter
from app.chat.origins import check_origin, preflight
from app.chat.ratelimit import RateLimiter
from app.chat.service import (
    ChatService,
    ChatTurnInput,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    TokenEvent,
)
from app.chat.settings import TenantChatSettings
from app.config import get_settings
from app.ingestion.queue import JobQueue, get_job_queue
from app.models import Conversation, Message
from app.schemas import ChatRequestBody, ChatResponseBody, ChatUpdates

router = APIRouter(tags=["chat"])

AnyKeyAuth = Annotated[AuthContext, Depends(authenticate)]
ServiceDep = Annotated[ChatService, Depends(get_chat_service)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
QueueDep = Annotated[JobQueue, Depends(get_job_queue)]


def _too_many(detail: str, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


async def _check_limits(
    limiter: RateLimiter, auth: AuthContext, body: ChatRequestBody, settings: TenantChatSettings
) -> None:
    config = get_settings()
    retry = await limiter.per_minute(f"key:{auth.api_key_id}", config.chat_rate_per_key_per_minute)
    if retry:
        raise _too_many("Too many messages from this website; please wait a moment", retry)
    retry = await limiter.per_minute(
        f"visitor:{auth.tenant_id}:{body.visitor_id}", config.chat_rate_per_visitor_per_minute
    )
    if retry:
        raise _too_many("You're sending messages too quickly; please wait a moment", retry)
    cap = settings.daily_message_cap or config.chat_daily_message_cap
    retry = await limiter.daily(f"tenant:{auth.tenant_id}", cap)
    if retry:
        raise _too_many("The assistant has reached today's message limit; try tomorrow", retry)


@router.options("/chat", include_in_schema=False)
async def chat_preflight(request: Request) -> Response:
    return preflight(request, "POST, OPTIONS")


@router.post(
    "/chat",
    response_model=ChatResponseBody,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE when stream=true"}},
)
async def chat(
    body: ChatRequestBody,
    request: Request,
    auth: AnyKeyAuth,
    service: ServiceDep,
    limiter: LimiterDep,
    queue: QueueDep,
):
    tenant = await auth.db.tenant()
    settings = TenantChatSettings.from_tenant(tenant.name, tenant.settings)
    check_origin(request, auth, settings)
    await _check_limits(limiter, auth, body, settings)

    try:
        accepted = await accept(
            auth.db,
            settings=settings,
            channel="web",
            visitor_id=body.visitor_id,
            text=body.message,
            client_message_id=body.client_message_id,
            conversation_id=body.conversation_id,
            history_messages=service.config.history_messages,
        )
    except ConversationNotFound:
        # Another visitor's conversation is reported exactly like a missing one.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        ) from None
    except AlreadyInFlight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This message is already being answered; please retry in a moment",
        ) from None
    if accepted.replay is not None:
        return _replay(accepted.replay, accepted.conversation.id, body.stream)
    if accepted.silent:
        # A person is handling this conversation: no AI reply, enforced here, not in the prompt.
        if accepted.new_message:
            await queue.enqueue_staff_alert(auth.tenant_id, accepted.conversation.id, "message")
        return _silent(accepted.conversation, body.stream)
    turn = accepted.turn
    assert turn is not None
    if body.stream:
        return StreamingResponse(
            _sse(service, turn),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _collect(service, turn)


def _silent(conversation: Conversation, stream: bool):
    """The answer while a person handles the conversation: stored, nothing to say."""
    done = {
        "message_id": None,
        "conversation_id": conversation.id,
        "outcome": None,
        "silent": True,
        "conversation_status": conversation.status,
    }
    if stream:

        async def events() -> AsyncIterator[str]:
            yield _event("done", done)

        return StreamingResponse(events(), media_type="text/event-stream")
    return ChatResponseBody(
        conversation_id=conversation.id,
        message_id=None,
        reply="",
        outcome=None,
        citations=[],
        usage={},
        timings={},
        retrieval=None,
        silent=True,
        conversation_status=conversation.status,
    )


def _replay(reply: Message, conversation_id: uuid.UUID, stream: bool):
    """Return an already stored answer for a retried client_message_id."""
    usage = {
        "prompt_tokens": reply.prompt_tokens or 0,
        "completion_tokens": reply.completion_tokens or 0,
    }
    if stream:

        async def events() -> AsyncIterator[str]:
            yield _event("token", {"text": reply.content})
            yield _event("citations", {"citations": reply.citations})
            yield _event(
                "done",
                {
                    "message_id": reply.id,
                    "conversation_id": conversation_id,
                    "outcome": reply.outcome,
                    "usage": usage,
                    "replayed": True,
                },
            )

        return StreamingResponse(events(), media_type="text/event-stream")
    return ChatResponseBody(
        conversation_id=conversation_id,
        message_id=reply.id,
        reply=reply.content,
        outcome=reply.outcome or "answered",
        citations=reply.citations,
        usage=usage,
        timings=reply.timings,
        retrieval=reply.retrieval,
        model=reply.model,
        replayed=True,
    )


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _sse(service: ChatService, turn: ChatTurnInput) -> AsyncIterator[str]:
    async for event in service.respond(turn):
        if isinstance(event, TokenEvent):
            yield _event("token", {"text": event.text})
        elif isinstance(event, CitationsEvent):
            yield _event("citations", {"citations": event.citations})
        elif isinstance(event, DoneEvent):
            yield _event(
                "done",
                {
                    "message_id": event.message_id,
                    "conversation_id": event.conversation_id,
                    "outcome": event.outcome,
                    "usage": event.usage,
                    "timings": event.timings,
                    "conversation_status": event.conversation_status,
                },
            )
        elif isinstance(event, ErrorEvent):
            yield _event(
                "error",
                {
                    "message": event.message,
                    "message_id": event.message_id,
                    "conversation_id": event.conversation_id,
                },
            )


async def _collect(service: ChatService, turn: ChatTurnInput) -> JSONResponse | ChatResponseBody:
    citations: list[dict[str, Any]] = []
    async for event in service.respond(turn):
        if isinstance(event, CitationsEvent):
            citations = event.citations
        elif isinstance(event, DoneEvent):
            return ChatResponseBody(
                conversation_id=event.conversation_id,
                message_id=event.message_id,
                reply=event.reply,
                outcome=event.outcome,
                citations=citations,
                usage=event.usage,
                timings=event.timings,
                retrieval=event.retrieval,
                model=event.model,
                conversation_status=event.conversation_status,
            )
        elif isinstance(event, ErrorEvent):
            body = ChatResponseBody(
                conversation_id=event.conversation_id,
                message_id=event.message_id,
                reply=event.message,
                outcome="error",
                citations=[],
                usage={},
                timings={},
                retrieval=None,
                error="The assistant could not generate an answer",
            )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY, content=body.model_dump(mode="json")
            )
    raise RuntimeError("chat pipeline ended without a done or error event")  # pragma: no cover


@router.options("/chat/updates", include_in_schema=False)
async def updates_preflight(request: Request) -> Response:
    return preflight(request, "GET, OPTIONS")


@router.get("/chat/updates", response_model=ChatUpdates)
async def chat_updates(
    request: Request,
    auth: AnyKeyAuth,
    limiter: LimiterDep,
    conversation_id: uuid.UUID,
    visitor_id: Annotated[str, Query(min_length=1, max_length=128)],
    after: datetime | None = None,
) -> ChatUpdates:
    """Team members' replies for the widget, which polls while a person handles the chat."""
    tenant = await auth.db.tenant()
    settings = TenantChatSettings.from_tenant(tenant.name, tenant.settings)
    check_origin(request, auth, settings)
    retry = await limiter.per_minute(
        f"poll:{auth.tenant_id}:{visitor_id}", get_settings().chat_poll_per_visitor_per_minute
    )
    if retry:
        raise _too_many("Checking for replies too often; please wait a moment", retry)
    conversation = await auth.db.get(Conversation, conversation_id)
    owned = conversation is not None and conversation.visitor_id == visitor_id
    if not owned or conversation.channel != "web":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    query = auth.db.select(Message).where(
        Message.conversation_id == conversation.id, Message.role == "staff"
    )
    if after is not None:
        query = query.where(Message.created_at > after)
    messages = await auth.db.scalars(query.order_by(Message.created_at).limit(50))
    return ChatUpdates(
        conversation_status=conversation.status,
        messages=[{"id": m.id, "content": m.content, "created_at": m.created_at} for m in messages],
    )
