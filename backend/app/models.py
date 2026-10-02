import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db import Base

ApiKeyKind = Literal["admin", "widget"]


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantOwned:
    """Mixin for tables whose rows belong to one tenant.

    Every TenantOwned table must also have an RLS policy in its migration (see AGENTS.md), and is
    queried only through app.tenancy.TenantDB.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class Tenant(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class ApiKey(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "api_keys"
    __table_args__ = (CheckConstraint("kind IN ('admin', 'widget')", name="api_keys_kind_check"),)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "documents"

    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
