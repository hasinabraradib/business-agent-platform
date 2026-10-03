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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.chat.settings import WEEKDAYS, TenantChatSettings
from app.models import HUMAN_STATUSES, Conversation, Message
from app.tenancy import TenantDB
from app.webhooks.events import record_event

DAYS_AHEAD = 8  # how far to look for the team's next opening


@dataclass(frozen=True)
class Availability:
    online: bool | None  # None: the business has not set its hours
    back_at: datetime | None = None  # local time the team is next online, when offline


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def team_availability(settings: TenantChatSettings, now: datetime) -> Availability:
    """Is the team online now (within the opening hours, in the business's timezone), and if
    not, when is it back? A closing time at or before the opening time means after midnight."""
    if not settings.opening_hours:
        return Availability(online=None)
    zone = ZoneInfo(settings.timezone)
    local = now.astimezone(zone)
    upcoming: list[datetime] = []
    for offset in range(-1, DAYS_AHEAD):
        day: date = local.date() + timedelta(days=offset)
        for opens, closes in settings.opening_hours.get(WEEKDAYS[day.weekday()], []):
            start = datetime.combine(day, _clock(opens), tzinfo=zone)
            end_day = day if _clock(closes) > _clock(opens) else day + timedelta(days=1)
            end = datetime.combine(end_day, _clock(closes), tzinfo=zone)
            if start <= local < end:
                return Availability(online=True)
            if start > local:
                upcoming.append(start)
    return Availability(online=False, back_at=min(upcoming) if upcoming else None)


def _clock_text(moment: datetime) -> str:
    """'2:30 pm', '12 pm'."""
    hour = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    return f"{hour}:{moment.minute:02d} {suffix}" if moment.minute else f"{hour} {suffix}"


def _when(back_at: datetime, now: datetime, language: str) -> str:
    local_now = now.astimezone(back_at.tzinfo)
    days = (back_at.date() - local_now.date()).days
    clock = _clock_text(back_at)
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
        "online": " They're online now and will reply here {reply}.",
        "offline": " They're offline right now and back {when}; they'll reply here {reply}.",
        "unknown": " They'll reply here {reply}.",
        "soon": "shortly",
        "later": "as soon as they can",
    },
    "banglish": {
        "lead": "Apnar message amader team ke pathiye diyechi.",
        "online": " Team ekhon online ache, {reply} ekhanei reply dibe.",
        "offline": " Team ekhon offline, {when} abar online hobe; {reply} ekhanei reply dibe.",
        "unknown": " Team {reply} ekhanei reply dibe.",
        "soon": "kichukkhoner moddhe",
        "later": "jototara shombhob druto",
    },
    "bengali": {
        "lead": "আপনার বার্তা আমাদের টিমের কাছে পাঠিয়েছি।",
        "online": " টিম এখন অনলাইনে আছে, {reply} এখানেই উত্তর দেবে।",
        "offline": " টিম এখন অফলাইনে, {when} আবার অনলাইনে আসবে; {reply} এখানেই উত্তর দেবে।",
        "unknown": " টিম {reply} এখানেই উত্তর দেবে।",
        "soon": "কিছুক্ষণের মধ্যে",
        "later": "যত দ্রুত সম্ভব",
    },
}


def acknowledgement(settings: TenantChatSettings, language: str, now: datetime) -> str:
    """The one message a customer gets when the conversation is handed to the team, in their
    language: whether the team is online now or when it's back, and the business's own reply
    time (follow_up_promise, e.g. "within one working day") if it has set one."""
    text = ACKNOWLEDGEMENTS.get(language, ACKNOWLEDGEMENTS["english"])
    availability = team_availability(settings, now)
    promise = settings.follow_up_promise.strip()
    if availability.online:
        return text["lead"] + text["online"].format(reply=promise or text["soon"])
    reply = promise or text["later"]
    if availability.online is False and availability.back_at is not None:
        when = _when(availability.back_at, now, language)
        return text["lead"] + text["offline"].format(when=when, reply=reply)
    return text["lead"] + text["unknown"].format(reply=reply)


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
