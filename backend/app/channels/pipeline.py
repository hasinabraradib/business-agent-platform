"""The channel-independent part of a customer turn.

Every channel (the website widget, Telegram) hands a customer message to accept(): it finds or
creates the conversation, stores the message once (client message ids make retries safe), and
decides whether the assistant may answer. While a person is involved (waiting_human or human)
the answer is no: the message is stored and staff are alerted, but no model is called. That is
the code-level guarantee behind the handoff; the prompt is never trusted with it.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.chat.prompts import HistoryTurn
from app.chat.service import ChatTurnInput
from app.chat.settings import TenantChatSettings
from app.models import HUMAN_STATUSES, Conversation, Message
from app.tenancy import TenantDB

STAFF_PREFIX = "(A team member wrote) "  # staff messages as seen by the model in the history
MAX_EARLIER_CHUNKS = 8


class ConversationNotFound(LookupError):
    """Missing, another visitor's, or another channel's conversation (all look the same)."""


class AlreadyInFlight(RuntimeError):
    """A concurrent retry with the same client message id is being answered."""


@dataclass
class Accepted:
    conversation: Conversation
    user_message: Message
    new_message: bool  # False for a retry of a message already stored
    replay: Message | None = None  # the stored answer to a retried message
    silent: bool = False  # a person is handling the conversation: the assistant must not reply
    turn: ChatTurnInput | None = None  # set when the assistant should answer
    history: list[HistoryTurn] = field(default_factory=list)


async def accept(
    db: TenantDB,
    *,
    settings: TenantChatSettings,
    channel: str,
    visitor_id: str,
    text: str,
    client_message_id: str | None,
    conversation_id: uuid.UUID | None = None,
    customer_name: str | None = None,
    history_messages: int = 6,
) -> Accepted:
    """Store the customer's message and decide whether the assistant answers. Commits."""
    existing = None
    if client_message_id:
        existing = await db.scalar(
            db.select(Message).where(
                Message.client_message_id == client_message_id, Message.role == "user"
            )
        )
    if existing is not None:
        conversation = await db.get(Conversation, existing.conversation_id, for_update=True)
        _check_owner(conversation, channel, visitor_id)
        answered = await db.scalar(
            db.select(Message)
            .where(Message.in_reply_to == existing.id)
            .where(Message.outcome.is_distinct_from("error"))
            .order_by(Message.created_at.desc())
        )
        if answered is not None:
            return Accepted(conversation, existing, new_message=False, replay=answered)
        user_message, new_message = existing, False
    else:
        conversation = await _conversation(db, channel, visitor_id, conversation_id)
        if customer_name and conversation.customer_name != customer_name:
            conversation.customer_name = customer_name
        if conversation.status == "resolved":
            conversation.status = "ai"  # the customer is back: the assistant answers again
        user_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=text,
            client_message_id=client_message_id,
        )
        db.add(user_message)
        try:
            await db.flush()
        except IntegrityError:
            raise AlreadyInFlight from None
        new_message = True
    conversation.updated_at = func.now()

    if conversation.status in HUMAN_STATUSES:
        await db.commit()
        return Accepted(conversation, user_message, new_message, silent=True)

    earlier = await db.scalars(
        db.select(Message)
        .where(Message.conversation_id == conversation.id, Message.id != user_message.id)
        .where(Message.created_at <= user_message.created_at)
        .where(Message.outcome.is_distinct_from("error"))  # failed turns are not context
        .order_by(Message.created_at.desc())
        .limit(history_messages)
    )
    history = [_history_turn(m) for m in reversed(earlier)]
    earlier_chunk_ids: list[uuid.UUID] = []
    for message in [m for m in earlier if m.role == "assistant"][:2]:  # the two latest replies
        for citation in message.citations or []:
            chunk_id = uuid.UUID(str(citation["chunk_id"]))
            if chunk_id not in earlier_chunk_ids and len(earlier_chunk_ids) < MAX_EARLIER_CHUNKS:
                earlier_chunk_ids.append(chunk_id)
    await db.commit()
    turn = ChatTurnInput(
        tenant_id=db.tenant_id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        message=user_message.content,
        history=history,
        settings=settings,
        earlier_chunk_ids=earlier_chunk_ids,
        visitor_id=visitor_id,
    )
    return Accepted(conversation, user_message, new_message, turn=turn, history=history)


def _check_owner(conversation: Conversation | None, channel: str, visitor_id: str) -> None:
    if (
        conversation is None
        or conversation.visitor_id != visitor_id
        or conversation.channel != channel
    ):
        raise ConversationNotFound


async def _conversation(
    db: TenantDB, channel: str, visitor_id: str, conversation_id: uuid.UUID | None
) -> Conversation:
    if conversation_id is not None:
        conversation = await db.get(Conversation, conversation_id, for_update=True)
        _check_owner(conversation, channel, visitor_id)
        return conversation
    if channel != "web":
        # Chat apps have one ongoing thread per customer: continue their latest conversation.
        conversation = await db.scalar(
            db.select(Conversation)
            .where(Conversation.channel == channel, Conversation.visitor_id == visitor_id)
            .order_by(Conversation.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if conversation is not None:
            return conversation
    conversation = Conversation(visitor_id=visitor_id, channel=channel, status="ai")
    db.add(conversation)
    await db.flush()
    return conversation


def _history_turn(message: Message) -> HistoryTurn:
    if message.role == "staff":
        # Shown to the model as an earlier reply, marked so it doesn't take credit for it or
        # contradict it after the conversation is handed back.
        return HistoryTurn("assistant", STAFF_PREFIX + message.content)
    return HistoryTurn(message.role, message.content)
