"""Conversations and messages for chat.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02

Both tables are tenant-owned: forced RLS with the tenant_isolation policy and explicit grants
to bap_app. messages references conversations through (tenant_id, conversation_id), so a
message can never point at another tenant's conversation (foreign-key checks bypass RLS).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bap_app"
CURRENT_TENANT = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        primary_key=True,
    )


def _tenant() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _protect(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
    op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "conversations",
        _id(),
        _tenant(),
        sa.Column("channel", sa.String(16), nullable=False, server_default="web"),
        sa.Column("visitor_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint("channel IN ('web')", name="conversations_channel_check"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="conversations_status_check"),
        sa.UniqueConstraint("tenant_id", "id", name="conversations_tenant_id_id_key"),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index(
        "ix_conversations_tenant_updated",
        "conversations",
        ["tenant_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "messages",
        _id(),
        _tenant(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "citations", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "timings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("retrieval", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="CASCADE",
            name="messages_conversation_fkey",
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered', 'no_answer', 'smalltalk', 'error')",
            name="messages_outcome_check",
        ),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )

    _protect("conversations", "SELECT, INSERT, UPDATE")
    # Messages are an append-only record.
    _protect("messages", "SELECT, INSERT")


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
