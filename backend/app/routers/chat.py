"""POST /v1/chat: customer chat, for widget keys (public, origin-checked) and admin keys."""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import AuthContext, authenticate
from app.chat.deps import get_chat_service, get_rate_limiter
from app.chat.origins import check_origin, preflight
from app.chat.prompts import HistoryTurn
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
from app.models import Conversation, Message
from app.schemas import ChatRequestBody, ChatResponseBody

router = APIRouter(tags=["chat"])

AnyKeyAuth = Annotated[AuthContext, Depends(authenticate)]
ServiceDep = Annotated[ChatService, Depends(get_chat_service)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


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
):
    tenant = await auth.db.tenant()
    settings = TenantChatSettings.from_tenant(tenant.name, tenant.settings)
    check_origin(request, auth, settings)
    await _check_limits(limiter, auth, body, settings)

    if body.conversation_id is not None:
        conversation = await auth.db.get(Conversation, body.conversation_id)
        # Another visitor's conversation is reported exactly like a missing one.
        if conversation is None or conversation.visitor_id != body.visitor_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
            )
    else:
        conversation = Conversation(visitor_id=body.visitor_id, channel="web")
        auth.db.add(conversation)
        await auth.db.flush()

    recent = await auth.db.scalars(
        auth.db.select(Message)
        .where(Message.conversation_id == conversation.id)
        .where(Message.outcome.is_distinct_from("error"))  # failed turns are not context
        .order_by(Message.created_at.desc())
        .limit(service.config.history_messages)
    )
    history = [HistoryTurn(m.role, m.content) for m in reversed(recent)]
    auth.db.add(Message(conversation_id=conversation.id, role="user", content=body.message))
    await auth.db.commit()

    turn = ChatTurnInput(
        tenant_id=auth.tenant_id,
        conversation_id=conversation.id,
        message=body.message,
        history=history,
        settings=settings,
    )
    if body.stream:
        return StreamingResponse(
            _sse(service, turn),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _collect(service, turn)


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
