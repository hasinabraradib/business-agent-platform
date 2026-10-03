import datetime as dt
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from app.chat.tools import ToolArgs, TurnContext, WriteOutcome, WriteTool
from app.models import PendingAction, Reservation
from app.tenancy import TenantDB

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_NOTICE = timedelta(minutes=30)
MAX_AHEAD = timedelta(days=60)
LAST_SEATING_BEFORE_CLOSE = timedelta(minutes=30)
REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[^\d+]", "", value)
    if not 7 <= len(digits.lstrip("+")) <= 15:
        raise ValueError("phone number must have 7 to 15 digits")
    return digits


class ReservationArgs(ToolArgs):
    date: dt.date = Field(description="Reservation date, YYYY-MM-DD (resolve 'tomorrow' yourself)")
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="24-hour HH:MM, e.g. 20:00")
    party_size: int = Field(ge=1, le=500, description="Number of people")
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=7, max_length=32)
    notes: str = Field(default="", max_length=300, description="Optional requests")

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return normalize_phone(value)


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def within_opening_hours(settings, local: datetime) -> bool:
    """Is a reservation starting at this local time allowed (open, and not too near closing)?"""
    start = local.hour * 60 + local.minute
    last_seating = int(LAST_SEATING_BEFORE_CLOSE.total_seconds() // 60)
    today = WEEKDAY_KEYS[local.weekday()]
    yesterday = WEEKDAY_KEYS[(local.weekday() - 1) % 7]
    for opens, closes in settings.opening_hours.get(today, []):
        o, c = _minutes(opens), _minutes(closes)
        if c <= o:  # closes after midnight: the evening part belongs to today
            if start >= o:
                return True
        elif o <= start <= c - last_seating:
            return True
    for opens, closes in settings.opening_hours.get(yesterday, []):
        o, c = _minutes(opens), _minutes(closes)
        if c <= o and start <= c - last_seating:  # after midnight, from yesterday's opening
            return True
    return False


def describe_hours(settings) -> str:
    parts = []
    for key in WEEKDAY_KEYS:
        ranges = settings.opening_hours.get(key)
        text = ", ".join(f"{o}-{c}" for o, c in ranges) if ranges else "closed"
        parts.append(f"{key.title()} {text}")
    return "; ".join(parts)


class CreateReservationTool(WriteTool):
    name = "create_reservation"
    args_model = ReservationArgs

    def description(self, settings) -> str:
        return (
            f"Book a table at {settings.business_name}. Needs date, time, party size, name and "
            "phone. The first call only checks the details and returns them for the customer to "
            "confirm; call again with the same details after the customer confirms."
        )

    def guidance(self, settings) -> str:
        return (
            "- Reservations: collect date, time, party size, name and phone conversationally "
            "(ask only for what is missing), then call create_reservation. It answers NOT DONE "
            "YET with the details: read them back and ask the customer to confirm. When they "
            "confirm, call it again with exactly the same details. Never say a table is booked "
            "unless the tool says it is booked. If it refuses (closed, past, too large a party), "
            "explain briefly and offer the contact."
        )

    def identity(self, args: ReservationArgs) -> dict:
        return {
            "date": args.date.isoformat(),
            "time": args.time,
            "party_size": args.party_size,
            "name": args.name.casefold(),
            "phone": re.sub(r"\D", "", args.phone),
        }

    def _starts_at(self, context: TurnContext, args: ReservationArgs) -> datetime:
        hours, minutes = (int(x) for x in args.time.split(":"))
        return datetime.combine(
            args.date, dt.time(hours, minutes), ZoneInfo(context.settings.timezone)
        )

    async def check(self, context: TurnContext, args: ReservationArgs) -> str | None:
        settings = context.settings
        contact = settings.fallback_contact
        if args.party_size > settings.max_online_party_size:
            return (
                f"Not booked: online bookings are limited to {settings.max_online_party_size} "
                f"people. Do not book. Tell the customer a group this size needs to talk to the "
                f"team, and give the contact: {contact}"
            )
        if not settings.opening_hours:
            return f"Not booked: online booking is not set up. Give the contact: {contact}"
        starts_at = self._starts_at(context, args)
        now = context.now.astimezone(starts_at.tzinfo)
        if starts_at < now + MIN_NOTICE:
            return (
                f"Not booked: {args.date.isoformat()} {args.time} is in the past or too soon "
                f"(local time now: {now:%Y-%m-%d %H:%M}). Ask for a later time."
            )
        if starts_at > now + MAX_AHEAD:
            return "Not booked: bookings can be made up to 60 days ahead. Ask for an earlier date."
        if not within_opening_hours(settings, starts_at):
            return (
                f"Not booked: {starts_at:%A} {args.time} is outside opening hours (last booking "
                f"30 minutes before closing). Opening hours: {describe_hours(settings)}. Suggest "
                "a time when we are open."
            )
        return None

    def read_back(self, context: TurnContext, args: ReservationArgs) -> str:
        starts_at = self._starts_at(context, args)
        clock = starts_at.strftime("%I:%M %p").lstrip("0")
        text = (
            f"{starts_at:%A} {starts_at.day} {starts_at:%B %Y} at {clock}, table for "
            f"{args.party_size}, name {args.name}, phone {args.phone}"
        )
        return f"{text}, notes: {args.notes}" if args.notes else text

    async def execute(
        self, context: TurnContext, db: TenantDB, args: ReservationArgs, pending: PendingAction
    ) -> WriteOutcome:
        starts_at = self._starts_at(context, args)
        reference = "R-" + "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)
        )
        reservation = Reservation(
            conversation_id=context.conversation_id,
            pending_action_id=pending.id,
            reference=reference,
            starts_at=starts_at,
            local_date=args.date,
            local_time=starts_at.time(),
            party_size=args.party_size,
            name=args.name,
            phone=args.phone,
            notes=args.notes,
        )
        db.add(reservation)
        await db.flush()
        details = self.read_back(context, args)
        return WriteOutcome(
            summary=f"BOOKED. Reference {reference}: {details}.",
            result_id=reservation.id,
            event_type="reservation.created",
            event_data={
                "id": str(reservation.id),
                "reference": reference,
                "starts_at": starts_at.isoformat(),
                "date": args.date.isoformat(),
                "time": args.time,
                "party_size": args.party_size,
                "name": args.name,
                "phone": args.phone,
                "notes": args.notes,
                "conversation_id": str(context.conversation_id),
            },
        )

    def done_message(self, outcome: WriteOutcome) -> str:
        return f"{outcome.summary} Tell the customer it is booked and give the reference."
