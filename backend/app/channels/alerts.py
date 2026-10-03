"""Staff alerts in the tenant's Telegram staff chat (run by the worker).

"handoff": a customer needs a person (sent when request_human runs). "message": the customer
wrote again while waiting. Each alert carries the customer's name, the last messages and a link,
and is recorded in staff_alerts so a reply to it from the staff chat reaches the customer.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.telegram import (
    TelegramError,
    bot_token,
    load_channel,
    make_client,
    telegram_text,
)
from app.config import get_settings
from app.models import Conversation, Message, StaffAlert
from app.tenancy import tenant_db

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


async def send_staff_alert(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    kind: str,
) -> str:
    async with tenant_db(sessionmaker, tenant_id) as db:
        channel = await load_channel(db)
        if channel is None or channel.staff_chat_id is None:
            return "skipped: no Telegram staff chat"
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
        client = make_client(bot_token(channel))
        try:
            ids = await client.send_message(channel.staff_chat_id, text)
        except TelegramError as exc:
            logger.warning("Staff alert not sent: %s", exc)
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
        await db.commit()
    return f"sent {len(ids)} message(s)"
