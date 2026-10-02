"""Per-tenant chat settings, stored in tenants.settings (JSON) and edited by the business."""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


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
