"""Document ingestion: document processing columns and the chunks table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

chunks is tenant-owned: forced RLS with the tenant_isolation policy, explicit grants to bap_app,
and a composite foreign key (tenant_id, document_id) so a chunk can only reference a document of
its own tenant (foreign-key checks bypass RLS, so the policy alone would not ensure that).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bap_app"
CURRENT_TENANT = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
EMBEDDING_DIMENSIONS = 768


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_uri", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "documents",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_check_constraint(
        "documents_status_check",
        "documents",
        "status IN ('pending', 'processing', 'ready', 'failed')",
    )
    op.create_unique_constraint("documents_tenant_id_id_key", "documents", ["tenant_id", "id"])
    op.create_index(
        "uq_documents_tenant_content_hash",
        "documents",
        ["tenant_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("source_type <> 'url' AND content_hash IS NOT NULL"),
    )
    op.create_index(
        "uq_documents_tenant_url",
        "documents",
        ["tenant_id", "source_uri"],
        unique=True,
        postgresql_where=sa.text("source_type = 'url'"),
    )

    op.create_table(
        "chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, "
                "coalesce(metadata->>'section', '') || ' ' || content)",
                persisted=True,
            ),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="chunks_document_fkey",
        ),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="chunks_document_id_chunk_index_key"
        ),
    )
    op.create_index("ix_chunks_tenant_id", "chunks", ["tenant_id"])
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")

    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON chunks "
        f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
    # Chunks are replaced wholesale (delete + insert), never updated in place.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON chunks TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_index("uq_documents_tenant_url", table_name="documents")
    op.drop_index("uq_documents_tenant_content_hash", table_name="documents")
    op.drop_constraint("documents_tenant_id_id_key", "documents", type_="unique")
    op.drop_constraint("documents_status_check", "documents", type_="check")
    for column in ("updated_at", "chunk_count", "error", "content_hash", "source_uri"):
        op.drop_column("documents", column)
