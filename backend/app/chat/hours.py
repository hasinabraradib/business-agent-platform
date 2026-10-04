"""Opening hours from tenant settings: is the business open now, until when, or when it opens.

Used by the system prompt (so "are you open now?" is answered from a computed status, not the
model's arithmetic) and by the handoff acknowledgement (whether the team is online).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.chat.settings import WEEKDAYS, TenantChatSettings

DAYS_AHEAD = 8  # how far to look for the next opening


@dataclass(frozen=True)
class Availability:
    online: bool | None  # None: the business has not set its hours
    back_at: datetime | None = None  # local time it next opens, when closed
    closes_at: datetime | None = None  # local time it closes, when open


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def availability(settings: TenantChatSettings, now: datetime) -> Availability:
    """Within the opening hours (in the business's timezone)? A closing time at or before the
    opening time means after midnight."""
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
                return Availability(online=True, closes_at=end)
            if start > local:
                upcoming.append(start)
    return Availability(online=False, back_at=min(upcoming) if upcoming else None)


def clock_text(moment: datetime) -> str:
    """'2:30 pm', '12 pm'."""
    hour = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    return f"{hour}:{moment.minute:02d} {suffix}" if moment.minute else f"{hour} {suffix}"


def open_status(settings: TenantChatSettings, now: datetime) -> str | None:
    """e.g. 'open now, until 11 pm tonight' or 'closed now; opens today at 2:30 pm'."""
    status = availability(settings, now)
    if status.online is None:
        return None
    local = now.astimezone(ZoneInfo(settings.timezone))
    if status.online and status.closes_at is not None:
        closes = status.closes_at
        if closes.date() != local.date():
            return f"open now, until {clock_text(closes)} (after midnight)"
        return f"open now, until {clock_text(closes)} {'tonight' if closes.hour >= 17 else 'today'}"
    if status.back_at is None:
        return "closed now"
    days = (status.back_at.date() - local.date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else f"on {status.back_at:%A}"
    return f"closed now; opens {day} at {clock_text(status.back_at)}"
