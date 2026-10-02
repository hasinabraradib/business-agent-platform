import uuid
from datetime import datetime
from typing import Any, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
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
MessageOutcome = Literal["answered", "no_answer", "smalltalk", "error"]
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
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="CASCADE",
            name="messages_conversation_fkey",
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered', 'no_answer', 'smalltalk', 'error')",
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
