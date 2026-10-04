"""Dashboard support: document size, staff read markers, closed knowledge gaps.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-04

Columns on existing tenant-owned tables only (their RLS policies already cover them):
- documents.size_bytes: the uploaded or fetched size, for the knowledge list.
- conversations.staff_read_at: when staff last opened the conversation (unread markers).
- messages.gap_closed_at: a no_answer reply whose question was answered from the knowledge-gaps
  screen, so it leaves the gaps list.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("size_bytes", sa.Integer()))
    op.add_column("conversations", sa.Column("staff_read_at", sa.DateTime(timezone=True)))
    op.add_column("messages", sa.Column("gap_closed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_messages_tenant_open_gaps",
        "messages",
        ["tenant_id", "created_at"],
        postgresql_where=sa.text("outcome = 'no_answer' AND gap_closed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_tenant_open_gaps", "messages")
    op.drop_column("messages", "gap_closed_at")
    op.drop_column("conversations", "staff_read_at")
    op.drop_column("documents", "size_bytes")
