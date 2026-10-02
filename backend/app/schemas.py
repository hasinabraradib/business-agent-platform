import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import ApiKeyKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TenantOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]


class ApiKeyCreate(BaseModel):
    kind: ApiKeyKind
    label: str = Field(default="", max_length=200)


class ApiKeyOut(ORMModel):
    """Key metadata. Never includes the key or its hash."""

    id: uuid.UUID
    kind: str
    prefix: str
    label: str
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full API key. Shown only in this response; store it now.")


class DocumentOut(ORMModel):
    id: uuid.UUID
    title: str
    source_type: str
    status: str
    created_at: datetime
