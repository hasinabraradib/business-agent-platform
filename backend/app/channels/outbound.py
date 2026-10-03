"""Sending a team member's message to the customer on the conversation's own channel.

Website widget: nothing to push; the widget polls GET /v1/chat/updates while a person handles
the conversation. Telegram: sent through the tenant's bot (app.channels.telegram).
"""

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Conversation

logger = logging.getLogger(__name__)


class StaffMessageSender(Protocol):
    async def __call__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        conversation: Conversation,
        text: str,
    ) -> bool: ...


async def send_staff_message(
    sessionmaker: async_sessionmaker[AsyncSession], conversation: Conversation, text: str
) -> bool:
    """True when the customer's channel has the message (or will fetch it itself)."""
    if conversation.channel == "web":
        return True  # the widget picks it up by polling
    if conversation.channel == "telegram":
        from app.channels.telegram import send_to_customer

        return await send_to_customer(sessionmaker, conversation, text)
    logger.warning("No outbound sender for channel %s", conversation.channel)
    return False


def get_staff_message_sender() -> StaffMessageSender:
    return send_staff_message
