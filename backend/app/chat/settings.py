"""Per-tenant chat settings, stored in tenants.settings (JSON) and edited by the business."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    @field_validator("allowed_origins")
    @classmethod
    def _normalize_origins(cls, origins: list[str]) -> list[str]:
        return [origin.strip().rstrip("/").lower() for origin in origins if origin.strip()]

    @classmethod
    def from_tenant(cls, tenant_name: str, raw: dict[str, Any] | None) -> "TenantChatSettings":
        settings = cls.model_validate(raw or {})
        if not settings.business_name:
            settings.business_name = tenant_name
        return settings
