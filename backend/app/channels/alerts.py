"""Staff alerts in the tenant's Telegram staff chat (run by the worker).

"handoff": a customer needs a person (sent when request_human runs). "message": the customer
wrote again while waiting. Each alert carries the customer's name, the last messages and a link,
and is recorded in staff_alerts so a reply to it from the staff chat reaches the customer.
"""

import asyncio
import logging
import smtplib
import uuid
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.telegram import (
    TelegramError,
    bot_token,
    load_channel,
    make_client,
    telegram_text,
)
from app.chat.settings import TenantChatSettings
from app.config import get_settings
from app.models import Conversation, Message, StaffAlert
from app.tenancy import TenantDB, tenant_db

logger = logging.getLogger(__name__)

RECENT_MESSAGES = 5
SNIPPET_CHARS = 300
SPEAKERS = {"user": "Customer", "assistant": "Assistant", "staff": "Team"}


def _customer(conversation: Conversation) -> str:
    where = "Telegram" if conversation.channel == "telegram" else "website chat"
    return f"{conversation.customer_name or 'A customer'} ({where})"


def alert_text(conversation: Conversation, recent: list[Message], kind: str, link: str) -> str:
    lines = []
    if kind == "handoff":
        lines.append(f"{_customer(conversation)} would like to talk to a person.")
        if conversation.handoff_reason:
            lines.append(f"Reason: {conversation.handoff_reason}")
    else:
        lines.append(f"New message from {_customer(conversation)}, waiting for the team.")
    lines.append("")
    lines.append("Last messages:")
    for message in recent:
        text = " ".join(telegram_text(message.content).split())  # no [n] markers or Markdown
        if len(text) > SNIPPET_CHARS:
            text = text[: SNIPPET_CHARS - 1] + "…"
        lines.append(f"{SPEAKERS.get(message.role, message.role)}: {text}")
    lines.append("")
    lines.append("Reply to this message to answer the customer.")
    lines.append(f"Open: {link}")
    return "\n".join(lines)


class StaffNotifier(Protocol):
    """One way to reach a tenant's staff. Returns a short result, or None if not configured."""

    name: str

    async def notify(
        self, db: TenantDB, conversation: Conversation, subject: str, text: str
    ) -> str | None: ...


class TelegramNotifier:
    """The tenant's Telegram staff chat. Alerts are recorded so a reply to one, from that chat,
    reaches the customer."""

    name = "telegram"

    async def notify(
        self, db: TenantDB, conversation: Conversation, subject: str, text: str
    ) -> str | None:
        channel = await load_channel(db)
        if channel is None or channel.staff_chat_id is None:
            return None
        client = make_client(bot_token(channel))
        try:
            ids = await client.send_message(channel.staff_chat_id, text)
        except TelegramError as exc:
            logger.warning("Staff alert not sent on Telegram: %s", exc)
            return f"failed: {exc}"
        finally:
            await client.aclose()
        db.add_all(
            StaffAlert(
                conversation_id=conversation.id,
                chat_id=channel.staff_chat_id,
                telegram_message_id=message_id,
            )
            for message_id in ids
        )
        return f"sent {len(ids)} message(s)"


EmailSender = Callable[[EmailMessage], Awaitable[None]]


async def smtp_send(message: EmailMessage) -> None:
    settings = get_settings()

    def send() -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)

    await asyncio.to_thread(send)


class EmailNotifier:
    """An email to the tenant's staff_alert_email through the platform's SMTP server. Replies
    by email don't reach the customer: the alert links to the conversation instead."""

    name = "email"

    def __init__(self, sender: EmailSender | None = None) -> None:
        self.sender = sender or smtp_send

    async def notify(
        self, db: TenantDB, conversation: Conversation, subject: str, text: str
    ) -> str | None:
        settings = get_settings()
        tenant = await db.tenant()
        to = TenantChatSettings.from_tenant(tenant.name, tenant.settings).staff_alert_email
        if not to or not settings.smtp_host or not settings.smtp_from:
            return None
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text.replace("Reply to this message to answer the customer.\n", ""))
        try:
            await self.sender(message)
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("Staff alert email not sent: %s", exc)
            return f"failed: {type(exc).__name__}"
        return "sent"


# Read at call time, so tests (or a deployment) can change the set.
NOTIFIERS: list[StaffNotifier] = [TelegramNotifier(), EmailNotifier()]


async def send_staff_alert(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    kind: str,
) -> str:
    async with tenant_db(sessionmaker, tenant_id) as db:
        conversation = await db.get(Conversation, conversation_id)
        if conversation is None:
            return "skipped: conversation not found"
        recent = await db.scalars(
            db.select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.role.desc())
            .limit(RECENT_MESSAGES)
        )
        link = get_settings().admin_conversation_url.format(conversation_id=conversation.id)
        text = alert_text(conversation, list(reversed(recent)), kind, link)
        subject = (
            f"{_customer(conversation)} would like to talk to a person"
            if kind == "handoff"
            else f"New message from {_customer(conversation)}"
        )
        results = {}
        for notifier in NOTIFIERS:
            outcome = await notifier.notify(db, conversation, subject, text)
            if outcome is not None:
                results[notifier.name] = outcome
        await db.commit()
    if not results:
        return "skipped: no staff notifier configured (Telegram staff chat or alert email)"
    if list(results) == ["telegram"]:
        return results["telegram"]
    return "; ".join(f"{name}: {outcome}" for name, outcome in results.items())
