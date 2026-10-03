"""Telegram: the bot webhook (public, verified by Telegram's secret token) and admin setup."""

import hashlib
import hmac
import logging
import secrets
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import secret_box
from app.auth import AdminAuth
from app.channels.outbound import send_staff_message
from app.channels.pipeline import AlreadyInFlight, ConversationNotFound, accept
from app.channels.telegram import (
    VISITOR_PREFIX,
    TelegramError,
    bot_token,
    load_channel,
    make_client,
    telegram_text,
)
from app.chat.deps import get_chat_service, get_rate_limiter
from app.chat.ratelimit import RateLimiter
from app.chat.service import ChatService, DoneEvent, ErrorEvent
from app.chat.settings import TenantChatSettings
from app.config import get_settings
from app.db import get_sessionmaker
from app.handoff import add_staff_reply
from app.ingestion.queue import JobQueue, get_job_queue
from app.models import Conversation, StaffAlert, TelegramChannel, TelegramUpdate
from app.tenancy import TenantDB, tenant_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels/telegram", tags=["telegram"])

SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
ServiceDep = Annotated[ChatService, Depends(get_chat_service)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
QueueDep = Annotated[JobQueue, Depends(get_job_queue)]

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
LIMIT_NOTICE = "You're sending messages very quickly. Please wait a minute and try again."
DAILY_NOTICE = "We've reached today's message limit here. Please try again tomorrow."


def _not_found() -> HTTPException:
    # The same answer for an unknown tenant, a tenant without Telegram, and a wrong secret:
    # the response never tells a caller which tenants exist or have a bot.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@router.post("/{tenant_ref}/webhook", include_in_schema=False)
async def webhook(
    tenant_ref: str,
    request: Request,
    background: BackgroundTasks,
    sessionmaker: SessionmakerDep,
    service: ServiceDep,
    limiter: LimiterDep,
    queue: QueueDep,
) -> dict[str, Any]:
    try:
        tenant_id = uuid.UUID(tenant_ref)
    except ValueError:
        raise _not_found() from None
    supplied = request.headers.get(SECRET_HEADER, "")
    async with tenant_db(sessionmaker, tenant_id) as db:
        channel = await load_channel(db)
        expected = channel.webhook_secret_hash if channel is not None else None
        if not expected or not hmac.compare_digest(_hash(supplied), expected):
            raise _not_found()
        try:
            update = await request.json()
        except ValueError:
            return {"ok": True, "ignored": "not JSON"}
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            return {"ok": True, "ignored": "no update_id"}
        db.add(TelegramUpdate(update_id=update_id))
        try:
            await db.commit()
        except IntegrityError:
            return {"ok": True, "duplicate": True}  # Telegram redelivered it: handled once
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return {"ok": True, "ignored": "not a text message"}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return {"ok": True, "ignored": "no chat"}
        if channel.staff_chat_id is not None and chat_id == channel.staff_chat_id:
            handled = await _staff_reply(db, sessionmaker, channel, message)
            return {"ok": True, "staff_reply": handled}
        if chat.get("type") != "private":
            return {"ok": True, "ignored": "not a private chat"}
    # Answer Telegram at once; the model's reply is sent from a background task.
    background.add_task(
        _answer_customer,
        tenant_id=tenant_id,
        update_id=update_id,
        message=message,
        sessionmaker=sessionmaker,
        service=service,
        limiter=limiter,
        queue=queue,
    )
    return {"ok": True}


async def _staff_reply(
    db: TenantDB,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel: TelegramChannel,
    message: dict[str, Any],
) -> bool:
    """A reply, in the tenant's staff chat, to one of its staff alerts: send it to the customer
    as a team member's message. Anything else in the staff chat is staff talking: ignored."""
    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    if not isinstance(replied_to, int):
        return False
    alert = await db.scalar(
        db.select(StaffAlert).where(
            StaffAlert.chat_id == channel.staff_chat_id,
            StaffAlert.telegram_message_id == replied_to,
        )
    )
    if alert is None:
        return False
    conversation = await db.get(Conversation, alert.conversation_id, for_update=True)
    if conversation is None:
        return False
    sender = message.get("from") or {}
    via = {"via": "telegram", "user_id": sender.get("id"), "username": sender.get("username")}
    await add_staff_reply(db, conversation, message["text"], via)
    await db.commit()
    await send_staff_message(sessionmaker, conversation, message["text"])
    return True


def _customer_name(message: dict[str, Any]) -> str | None:
    sender = message.get("from") or {}
    name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
    return name[:120] or None


async def _over_limit(
    limiter: RateLimiter, tenant_id: uuid.UUID, visitor_id: str, settings: TenantChatSettings
) -> str | None:
    """The widget's limits, applied per bot instead of per API key. Returns a notice to send."""
    config = get_settings()
    if await limiter.per_minute(f"key:telegram:{tenant_id}", config.chat_rate_per_key_per_minute):
        return LIMIT_NOTICE
    if await limiter.per_minute(
        f"visitor:{tenant_id}:{visitor_id}", config.chat_rate_per_visitor_per_minute
    ):
        return LIMIT_NOTICE
    cap = settings.daily_message_cap or config.chat_daily_message_cap
    if await limiter.daily(f"tenant:{tenant_id}", cap):
        return DAILY_NOTICE
    return None


async def _answer_customer(
    *,
    tenant_id: uuid.UUID,
    update_id: int,
    message: dict[str, Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    service: ChatService,
    limiter: RateLimiter,
    queue: JobQueue,
) -> None:
    chat_id = message["chat"]["id"]
    visitor_id = f"{VISITOR_PREFIX}{chat_id}"
    async with tenant_db(sessionmaker, tenant_id) as db:
        tenant = await db.tenant()
        settings = TenantChatSettings.from_tenant(tenant.name, tenant.settings)
        channel = await load_channel(db)
        if channel is None:
            return
        token = bot_token(channel)
        notice = await _over_limit(limiter, tenant_id, visitor_id, settings)
        if notice is not None:
            # Tell them once a minute at most, rather than once per message.
            if not await limiter.per_minute(f"notice:{tenant_id}:{visitor_id}", 1):
                await _send(token, chat_id, notice)
            return
        try:
            accepted = await accept(
                db,
                settings=settings,
                channel="telegram",
                visitor_id=visitor_id,
                text=message["text"][:2000],
                client_message_id=f"tg-{update_id}",
                customer_name=_customer_name(message),
                history_messages=service.config.history_messages,
            )
        except (ConversationNotFound, AlreadyInFlight):
            return
    if accepted.replay is not None:
        await _send(token, chat_id, accepted.replay.content)
        return
    if accepted.silent:
        if accepted.new_message:
            await queue.enqueue_staff_alert(tenant_id, accepted.conversation.id, "message")
        return
    assert accepted.turn is not None
    reply = ""
    async for event in service.respond(accepted.turn):
        if isinstance(event, DoneEvent):
            reply = event.reply
        elif isinstance(event, ErrorEvent):
            reply = event.message
    if reply:
        await _send(token, chat_id, reply)


async def _send(token: str, chat_id: int, text: str) -> None:
    client = make_client(token)
    try:
        await client.send_message(chat_id, telegram_text(text))
    except TelegramError as exc:
        logger.warning("Could not answer on Telegram: %s", exc)
    finally:
        await client.aclose()


# --- admin setup -------------------------------------------------------------------------------


class TelegramSettingsIn(BaseModel):
    bot_token: str | None = Field(
        default=None, min_length=20, max_length=128, pattern=r"^\d+:[A-Za-z0-9_-]+$"
    )
    staff_chat_id: int | None = None


class TelegramSettingsOut(BaseModel):
    configured: bool
    bot_username: str | None = None
    staff_chat_id: int | None = None
    webhook_registered: bool = False


class RegisterWebhookIn(BaseModel):
    public_base_url: HttpUrl  # where this API is reachable from the internet, over https


class RegisterWebhookOut(BaseModel):
    webhook_url: str


def _out(channel: TelegramChannel | None) -> TelegramSettingsOut:
    if channel is None:
        return TelegramSettingsOut(configured=False)
    return TelegramSettingsOut(
        configured=True,
        bot_username=channel.bot_username,
        staff_chat_id=channel.staff_chat_id,
        webhook_registered=channel.webhook_secret_hash is not None,
    )


@router.get("", response_model=TelegramSettingsOut)
async def get_telegram(auth: AdminAuth) -> TelegramSettingsOut:
    return _out(await load_channel(auth.db))


@router.put("", response_model=TelegramSettingsOut)
async def put_telegram(body: TelegramSettingsIn, auth: AdminAuth) -> TelegramSettingsOut:
    """Set the bot token (checked with Telegram, stored encrypted) and/or the staff chat id."""
    channel = await load_channel(auth.db)
    if body.bot_token is not None:
        client = make_client(body.bot_token)
        try:
            me = await client.get_me()
        except TelegramError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Telegram did not accept this bot token",
            ) from None
        finally:
            await client.aclose()
        try:
            encrypted = secret_box.encrypt(body.bot_token, auth.tenant_id)
        except secret_box.SecretBoxError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from None
        if channel is None:
            channel = TelegramChannel(bot_token_encrypted=encrypted)
            auth.db.add(channel)
        else:
            channel.bot_token_encrypted = encrypted
            channel.webhook_secret_hash = None  # a new bot needs its webhook registered again
        channel.bot_username = me.get("username")
    elif channel is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Set bot_token first")
    if "staff_chat_id" in body.model_fields_set:
        channel.staff_chat_id = body.staff_chat_id
    await auth.db.commit()
    return _out(channel)


@router.post("/webhook", response_model=RegisterWebhookOut)
async def register_webhook(body: RegisterWebhookIn, auth: AdminAuth) -> RegisterWebhookOut:
    """Point the bot at this API (Telegram setWebhook) with a fresh secret token. The secret is
    sent to Telegram once and stored only as a hash; registering again rotates it."""
    if body.public_base_url.scheme != "https":
        raise HTTPException(
            status_code=422,
            detail="Telegram only delivers webhooks to https URLs",
        )
    channel = await load_channel(auth.db)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Set bot_token first")
    base = str(body.public_base_url).rstrip("/")
    url = f"{base}/v1/channels/telegram/{auth.tenant_id}/webhook"
    secret = secrets.token_urlsafe(32)  # Telegram allows A-Z, a-z, 0-9, _ and -
    client = make_client(bot_token(channel))
    try:
        await client.set_webhook(url, secret)
    except TelegramError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    finally:
        await client.aclose()
    channel.webhook_secret_hash = _hash(secret)
    await auth.db.commit()
    return RegisterWebhookOut(webhook_url=url)
