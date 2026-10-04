"""Telegram as a customer channel and as the staff alert channel.

- Customers message the tenant's bot; updates arrive at POST /v1/channels/telegram/{tenant}/webhook
  (app.routers.telegram), are verified by Telegram's secret-token header, deduplicated by
  update_id and fed to the same pipeline as the widget (app.channels.pipeline).
- Replies are plain text (no parse_mode, Markdown and citation markers removed) and split to fit
  Telegram's 4096-character limit.
- Staff alerts go to the tenant's staff chat; a reply to an alert from that chat (and only that
  chat) is sent to the customer as a team member's message.

The bot token is decrypted only to make a call and never appears in logs or errors.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import secret_box
from app.config import get_settings
from app.models import Conversation, TelegramChannel
from app.tenancy import TenantDB, tenant_db

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4096
VISITOR_PREFIX = "tg:"


class TelegramError(RuntimeError):
    """A Bot API call failed. The message never contains the bot token."""


class TelegramClient:
    def __init__(
        self, token: str, *, base_url: str | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        self._token = token
        self._base = (base_url or get_settings().telegram_api_base).rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} failed: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.is_success or not body.get("ok"):
            description = str(body.get("description") or response.status_code)
            raise TelegramError(f"{method} failed: {description.replace(self._token, '***')}")
        return body.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe", {})

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self._call(
            "setWebhook",
            {"url": url, "secret_token": secret_token, "allowed_updates": ["message"]},
        )

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call("getWebhookInfo", {})

    async def send_message(self, chat_id: int, text: str) -> list[int]:
        """Send plain text (never parse_mode), split to fit; returns the message ids."""
        ids = []
        for part in split_message(text):
            result = await self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": part, "link_preview_options": {"is_disabled": True}},
            )
            ids.append(int(result["message_id"]))
        return ids

    async def aclose(self) -> None:
        await self._client.aclose()


def default_client_factory(token: str) -> TelegramClient:
    return TelegramClient(token)


# Read at call time so tests can swap in a client over httpx.MockTransport.
CLIENT_FACTORY: Callable[[str], TelegramClient] = default_client_factory


def make_client(token: str) -> TelegramClient:
    return CLIENT_FACTORY(token)


MARKER = re.compile(r"\s?\[\d+(?:\]\[\d+)*\]")
MARKDOWN = re.compile(r"\*\*|__|`+|^#{1,6}\s+", re.MULTILINE)


def telegram_text(text: str) -> str:
    """Plain text for Telegram: no citation markers (there are no source chips there) and no
    Markdown syntax, which would show literally without parse_mode."""
    text = MARKER.sub("", text)
    text = MARKDOWN.sub("", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split at paragraph, line, sentence or word boundaries, each part at most `limit`."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[: limit + 1]
        found = [
            (position + len(separator), position > limit // 2)
            for separator in ("\n\n", "\n", ". ", "। ", " ")
            if (position := window.rfind(separator, 0, limit)) > 0
        ]
        # The strongest break in the second half of the window; otherwise the latest break of
        # any kind; otherwise (one very long word) a hard cut.
        good = [cut for cut, late in found if late]
        cut = good[0] if good else max((cut for cut, _ in found), default=limit)
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def chat_id_of(conversation: Conversation) -> int | None:
    if conversation.channel != "telegram" or not conversation.visitor_id.startswith(VISITOR_PREFIX):
        return None
    try:
        return int(conversation.visitor_id[len(VISITOR_PREFIX) :])
    except ValueError:
        return None


async def load_channel(db: TenantDB) -> TelegramChannel | None:
    return await db.scalar(db.select(TelegramChannel))


def bot_token(channel: TelegramChannel) -> str:
    return secret_box.decrypt(channel.bot_token_encrypted, channel.tenant_id)


async def send_to_customer(
    sessionmaker: async_sessionmaker[AsyncSession], conversation: Conversation, text: str
) -> bool:
    """A team member's message to a Telegram customer, through the tenant's own bot."""
    chat_id = chat_id_of(conversation)
    if chat_id is None:
        return False
    async with tenant_db(sessionmaker, conversation.tenant_id) as db:
        channel = await load_channel(db)
    if channel is None:
        return False
    client = make_client(bot_token(channel))
    try:
        await client.send_message(chat_id, telegram_text(text))
        return True
    except TelegramError as exc:
        logger.warning("Could not send a staff message on Telegram: %s", exc)
        return False
    finally:
        await client.aclose()
