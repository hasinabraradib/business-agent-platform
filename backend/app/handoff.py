"""Human handoff: conversation states, the acknowledgement, and staff actions.

    ai --request_human--> waiting_human --staff reply--> human
     ^                         |                           |
     +------- hand back -------+---------------------------+
     resolved <--- resolve --- (any state); a new customer message reopens it as ai

While a conversation is waiting_human or human the assistant does not reply at all: that is
enforced in code (app.channels.pipeline), not by the prompt. The acknowledgement sent when the
handoff starts is written by code too, from the tenant's settings, so it can't promise a reply
time or office hours the business hasn't set.
"""

import uuid
from datetime import datetime
from typing import Any

from app.chat.hours import Availability, availability, clock_text
from app.chat.settings import TenantChatSettings
from app.models import HUMAN_STATUSES, Conversation, Message
from app.tenancy import TenantDB
from app.webhooks.events import record_event


def team_availability(settings: TenantChatSettings, now: datetime) -> Availability:
    """The team is online during the opening hours (app.chat.hours)."""
    return availability(settings, now)


def _when(back_at: datetime, now: datetime, language: str) -> str:
    local_now = now.astimezone(back_at.tzinfo)
    days = (back_at.date() - local_now.date()).days
    clock = clock_text(back_at)
    if language == "banglish":
        day = "aj" if days == 0 else "kal" if days == 1 else back_at.strftime("%A")
        return f"{day} {clock} e"
    if language == "bengali":
        day = "আজ" if days == 0 else "আগামীকাল" if days == 1 else back_at.strftime("%A")
        return f"{day} {clock}-এ"
    if days == 0:
        return f"today at {clock}"
    return f"tomorrow at {clock}" if days == 1 else f"on {back_at.strftime('%A')} at {clock}"


ACKNOWLEDGEMENTS = {
    "english": {
        "lead": "I've passed this to our team.",
        "online": " They're online now and will reply here soon.",
        "offline": " They're offline right now and back {when}; they'll reply here {reply}.",
        "unknown": " They'll reply here {reply}.",
        "later": "as soon as they can",
    },
    "banglish": {
        "lead": "Apnar message amader team ke pathiye diyechi.",
        "online": " Team ekhon online ache, kichukkhoner moddhei ekhanei reply dibe.",
        "offline": " Team ekhon offline, {when} abar online hobe; {reply} ekhanei reply dibe.",
        "unknown": " Team {reply} ekhanei reply dibe.",
        "later": "jototara shombhob druto",
    },
    "bengali": {
        "lead": "আপনার বার্তা আমাদের টিমের কাছে পাঠিয়েছি।",
        "online": " টিম এখন অনলাইনে আছে, কিছুক্ষণের মধ্যেই এখানে উত্তর দেবে।",
        "offline": " টিম এখন অফলাইনে, {when} আবার অনলাইনে আসবে; {reply} এখানেই উত্তর দেবে।",
        "unknown": " টিম {reply} এখানেই উত্তর দেবে।",
        "later": "যত দ্রুত সম্ভব",
    },
}


def acknowledgement(settings: TenantChatSettings, language: str, now: datetime) -> str:
    """The one message a customer gets when the conversation is handed to the team, in their
    language. Online now: they'll reply here soon (a reply time like "within one working day"
    next to "online now" reads as a contradiction). Offline: when the team is back, and the
    business's own reply time (follow_up_promise) as the fallback promise. A tenant can replace
    either message with its own wording (handoff_online_message / handoff_offline_message, with
    {when} and {reply_time} placeholders); that wording is used for every language."""
    text = ACKNOWLEDGEMENTS.get(language, ACKNOWLEDGEMENTS["english"])
    availability = team_availability(settings, now)
    reply = settings.follow_up_promise.strip() or text["later"]
    when = ""
    if availability.online is False and availability.back_at is not None:
        when = _when(availability.back_at, now, language)
    if availability.online:
        if settings.handoff_online_message:
            return _custom(settings.handoff_online_message, when, reply)
        return text["lead"] + text["online"]
    if settings.handoff_offline_message:
        return _custom(settings.handoff_offline_message, when, reply)
    if when:
        return text["lead"] + text["offline"].format(when=when, reply=reply)
    return text["lead"] + text["unknown"].format(reply=reply)


def _custom(template: str, when: str, reply_time: str) -> str:
    """The tenant's own wording; unknown placeholders are left as written, never an error."""
    return template.replace("{when}", when).replace("{reply_time}", reply_time).strip()


def _event_data(conversation: Conversation, **extra: Any) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation.id),
        "channel": conversation.channel,
        "customer_name": conversation.customer_name,
        **extra,
    }


async def request_handoff(
    db: TenantDB, conversation: Conversation, reason: str, now: datetime
) -> tuple[bool, uuid.UUID | None]:
    """Move to waiting_human and record handoff.requested. Returns (changed, delivery id);
    asking again while a person is already involved changes nothing."""
    if conversation.status in HUMAN_STATUSES:
        return False, None
    conversation.status = "waiting_human"
    conversation.handoff_reason = reason
    conversation.handoff_requested_at = now
    delivery = await record_event(
        db, "handoff.requested", _event_data(conversation, reason=reason, at=now.isoformat())
    )
    return True, delivery


async def add_staff_reply(
    db: TenantDB, conversation: Conversation, text: str, via: dict[str, Any]
) -> Message:
    """A team member's message to the customer; the assistant stays silent from now on."""
    message = Message(
        conversation_id=conversation.id,
        role="staff",
        content=text,
        retrieval={"staff": via},  # who sent it (admin key or Telegram user), for audit
    )
    db.add(message)
    conversation.status = "human"
    await db.flush()
    return message


async def end_handoff(db: TenantDB, conversation: Conversation, status: str) -> uuid.UUID | None:
    """Hand back to the assistant (status ai) or mark resolved; handoff.resolved is recorded
    if a person was involved."""
    assert status in ("ai", "resolved")
    was_human = conversation.status in HUMAN_STATUSES
    conversation.status = status
    if not was_human:
        return None
    return await record_event(db, "handoff.resolved", _event_data(conversation, status=status))
