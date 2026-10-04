"""Per-tenant chat settings, stored in tenants.settings (JSON) and edited by the business."""

import logging
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

OpeningHours = dict[str, list[tuple[str, str]]]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class TenantChatSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assistant_name: str = Field(default="Assistant", max_length=60)
    business_name: str = Field(default="", max_length=120)  # empty: the tenant's name
    tone: str = Field(default="friendly, warm and concise", max_length=200)
    # How customers reach a person, offered whenever the assistant cannot answer.
    fallback_contact: str = Field(default="", max_length=200)
    # Websites allowed to use widget keys (exact origins, e.g. "https://example.com").
    allowed_origins: list[str] = Field(default_factory=list)
    daily_message_cap: int | None = Field(default=None, ge=1)

    # Widget appearance and first impression.
    greeting: str = Field(default="Hi! How can I help you today?", max_length=300)
    # One accent colour per tenant (#RRGGBB only: it ends up in CSS). Platform default: lime.
    accent_color: str = Field(default="#C5EE4F", pattern=r"^#[0-9A-Fa-f]{6}$")
    suggested_questions: list[str] = Field(default_factory=list, max_length=4)
    # IANA timezone of the business, so the assistant knows the local date and time.
    timezone: str = Field(default="UTC", max_length=64)
    # Extra instructions from the business (appended to the system prompt as guidance).
    instructions: str = Field(default="", max_length=1000)

    # Tools the assistant may use for this tenant (the model is only offered these).
    enabled_tools: list[str] = Field(default_factory=lambda: ["search_knowledge"])
    currency: str = Field(default="BDT", pattern=r"^[A-Z]{3}$")
    # Weekly opening hours for reservations: {"mon": [["12:00", "23:00"]], ...}; a closing time
    # at or before the opening time means after midnight. Missing day: closed.
    opening_hours: OpeningHours = Field(default_factory=dict)
    max_online_party_size: int = Field(default=8, ge=1, le=100)
    # What the assistant may say about follow-up timing for leads; empty: promise no time.
    # Also the reply time in the handoff acknowledgement when the team is offline.
    follow_up_promise: str = Field(default="", max_length=200)
    # Optional own wording for the handoff acknowledgement (any language; placeholders {when}
    # = when the team is back, {reply_time} = follow_up_promise). Empty: built-in messages.
    handoff_online_message: str = Field(default="", max_length=300)
    # Where handoff alerts are emailed (needs the platform's SMTP settings). Empty: no email.
    staff_alert_email: str = Field(
        default="", max_length=200, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    handoff_offline_message: str = Field(default="", max_length=300)

    @field_validator("opening_hours")
    @classmethod
    def _valid_hours(
        cls, hours: dict[str, list[tuple[str, str]]]
    ) -> dict[str, list[tuple[str, str]]]:
        for day, ranges in hours.items():
            if day not in WEEKDAYS:
                raise ValueError(f"unknown weekday {day!r}; use {', '.join(WEEKDAYS)}")
            for opens, closes in ranges:
                for value in (opens, closes):
                    if not HHMM.fullmatch(value):
                        raise ValueError(f"times must be HH:MM, got {value!r}")
        return hours

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("suggested_questions")
    @classmethod
    def _short_questions(cls, questions: list[str]) -> list[str]:
        cleaned = [q.strip() for q in questions if q.strip()]
        if any(len(q) > 120 for q in cleaned):
            raise ValueError("suggested questions must be at most 120 characters")
        return cleaned

    @field_validator("allowed_origins")
    @classmethod
    def _normalize_origins(cls, origins: list[str]) -> list[str]:
        return [origin.strip().rstrip("/").lower() for origin in origins if origin.strip()]

    @classmethod
    def from_tenant(cls, tenant_name: str, raw: dict[str, Any] | None) -> "TenantChatSettings":
        """Parse stored settings; an invalid field falls back to its default (and is logged)
        rather than breaking chat and the widget for the whole tenant."""
        data = dict(raw or {})
        try:
            settings = cls.model_validate(data)
        except ValidationError as exc:
            invalid = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            logger.warning("Ignoring invalid tenant settings: %s", sorted(invalid))
            settings = cls.model_validate({k: v for k, v in data.items() if k not in invalid})
        if not settings.business_name:
            settings.business_name = tenant_name
        return settings
