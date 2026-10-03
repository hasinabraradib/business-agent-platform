import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.db import Base

ApiKeyKind = Literal["admin", "widget"]
DocumentStatus = Literal["pending", "processing", "ready", "failed"]
DOCUMENT_STATUSES: tuple[str, ...] = ("pending", "processing", "ready", "failed")
MessageRole = Literal["user", "assistant"]
# no_answer feeds the knowledge-gaps report; error marks a failed generation.
MessageOutcome = Literal["answered", "action", "no_answer", "smalltalk", "error"]
# Fixed by the chunks.embedding column; every embedding provider must return this many dims.
EMBEDDING_DIMENSIONS = 768


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
    # Fetch server-generated values (updated_at) with RETURNING on UPDATE too, so responses
    # never trigger a lazy load outside the async context.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="documents_status_check",
        ),
        # Target of chunks' composite foreign key: a chunk can only point at its own tenant's
        # document, even though foreign-key checks themselves bypass RLS.
        UniqueConstraint("tenant_id", "id", name="documents_tenant_id_id_key"),
        # Idempotent ingestion: identical uploaded content, or the same URL, once per tenant.
        Index(
            "uq_documents_tenant_content_hash",
            "tenant_id",
            "content_hash",
            unique=True,
            postgresql_where=text("source_type <> 'url' AND content_hash IS NOT NULL"),
        ),
        Index(
            "uq_documents_tenant_url",
            "tenant_id",
            "source_uri",
            unique=True,
            postgresql_where=text("source_type = 'url'"),
        ),
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    # pdf | markdown | text | csv | url
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Original filename for uploads, the URL for web pages.
    source_uri: Mapped[str | None] = mapped_column(Text)
    # sha256 of the uploaded bytes (or, for URLs, of the last fetched body).
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    error: Mapped[str | None] = mapped_column(Text)
    # CSV catalogues (menus, product lists): how columns map to catalog_items fields, or {} to
    # detect them. NULL: not a catalogue.
    catalog_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Chunk(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="chunks_document_fkey",
        ),
        UniqueConstraint("document_id", "chunk_index", name="chunks_document_id_chunk_index_key"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # The original chunk text, unmodified (the embedded text adds a title/section prefix).
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # page, section, row, source. "metadata" is reserved on SQLAlchemy models, hence `meta`.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Which model produced `embedding`; vectors from different models are not comparable.
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    # 'simple' configuration: content is English and Bengali, and Postgres has no Bengali
    # stemmer. Section headings are included so keyword search can match them.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, "
            "coalesce(metadata->>'section', '') || ' ' || content)",
            persisted=True,
        ),
    )


class Conversation(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "conversations"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        CheckConstraint("channel IN ('web')", name="conversations_channel_check"),
        CheckConstraint("status IN ('open', 'closed')", name="conversations_status_check"),
        UniqueConstraint("tenant_id", "id", name="conversations_tenant_id_id_key"),
        Index("ix_conversations_tenant_updated", "tenant_id", text("updated_at DESC")),
    )

    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="web", server_default="web"
    )
    # Chosen by the website widget (e.g. a random id kept in the browser); not authenticated.
    visitor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Message(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "messages"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="CASCADE",
            name="messages_conversation_fkey",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "in_reply_to"],
            ["messages.tenant_id", "messages.id"],
            name="messages_in_reply_to_fkey",
        ),
        UniqueConstraint("tenant_id", "id", name="messages_tenant_id_id_key"),
        # Idempotent retries: one customer message per client-generated id.
        Index(
            "uq_messages_tenant_client_message_id",
            "tenant_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('answered', 'action', 'no_answer', 'smalltalk', 'error')",
            name="messages_outcome_check",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # [{marker, chunk_id, document_id, document_title, metadata, snippet}]
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    outcome: Mapped[str | None] = mapped_column(String(16))  # assistant messages only
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    timings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # {mode, query, rewritten, chunk_ids, top_similarity, has_relevant_context, ...}
    retrieval: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # Customer messages: the widget's id for the message, so a retry is not stored twice.
    client_message_id: Mapped[str | None] = mapped_column(String(64))
    # Assistant messages: the customer message this replies to.
    in_reply_to: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


def _conversation_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "conversation_id"],
        ["conversations.tenant_id", "conversations.id"],
        ondelete="SET NULL (conversation_id)",
        name=name,
    )


class CatalogItem(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    """One row of a catalogue CSV (a dish, a product), for structured questions."""

    __tablename__ = "catalog_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="catalog_items_document_fkey",
        ),
        UniqueConstraint("document_id", "row", name="catalog_items_document_row_key"),
        Index("ix_catalog_items_tenant_category", "tenant_id", "category"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    row: Mapped[int] = mapped_column(Integer, nullable=False)
    # The chunk holding the same row, so results can be cited like search results.
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    alt_name: Mapped[str | None] = mapped_column(Text)  # e.g. the Bengali name
    category: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BDT")
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class PendingAction(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    """A write the assistant proposed and read back; executed only after the customer confirms
    in a later turn."""

    __tablename__ = "pending_actions"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        _conversation_fk("pending_actions_conversation_fkey"),
        CheckConstraint("status IN ('pending', 'done')", name="pending_actions_status_check"),
        Index("ix_pending_actions_lookup", "conversation_id", "tool", "args_hash"),
    )

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_in: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Reservation(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "reservations"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        _conversation_fk("reservations_conversation_fkey"),
        UniqueConstraint("tenant_id", "reference", name="reservations_tenant_reference_key"),
        UniqueConstraint("pending_action_id", name="reservations_pending_action_key"),
        CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'completed', 'no_show')",
            name="reservations_status_check",
        ),
        CheckConstraint("party_size > 0", name="reservations_party_size_check"),
        Index("ix_reservations_tenant_starts_at", "tenant_id", "starts_at"),
    )

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pending_action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reference: Mapped[str] = mapped_column(String(16), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    local_time: Mapped[time] = mapped_column(Time, nullable=False)
    party_size: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="confirmed", server_default="confirmed"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Order(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "order_number", name="orders_tenant_number_key"),
        CheckConstraint(
            "status IN ('processing', 'shipped', 'delivered', 'cancelled')",
            name="orders_status_check",
        ),
    )

    order_number: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BDT")
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    courier: Mapped[str | None] = mapped_column(Text)


class Lead(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "leads"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        _conversation_fk("leads_conversation_fkey"),
        UniqueConstraint("pending_action_id", name="leads_pending_action_key"),
        CheckConstraint("status IN ('new', 'contacted', 'closed')", name="leads_status_check"),
    )

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pending_action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str] = mapped_column(Text, nullable=False)
    interest: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )


class ToolCallRecord(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    """Every tool call, for replay in the dashboard."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        _conversation_fk("tool_calls_conversation_fkey"),
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["messages.tenant_id", "messages.id"],
            ondelete="CASCADE",
            name="tool_calls_message_fkey",
        ),
        CheckConstraint(
            "status IN ('ok', 'invalid_arguments', 'not_allowed', 'refused', "
            "'needs_confirmation', 'rate_limited', 'error')",
            name="tool_calls_status_check",
        ),
        Index("ix_tool_calls_message", "message_id"),
    )

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Numeric(10, 1), nullable=False)


class WebhookEndpoint(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "webhook_endpoints"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (UniqueConstraint("tenant_id", name="webhook_endpoints_tenant_key"),)

    url: Mapped[str] = mapped_column(Text, nullable=False)
    # HMAC signing secret. Needed in plain form to sign; readable only within the tenant (RLS).
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WebhookDelivery(UUIDPrimaryKey, CreatedAt, TenantOwned, Base):
    __tablename__ = "webhook_deliveries"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'retrying', 'delivered', 'failed')",
            name="webhook_deliveries_status_check",
        ),
        Index("ix_webhook_deliveries_tenant_created", "tenant_id", "created_at"),
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # [{at, status_code, error, duration_ms}]
    attempt_log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
