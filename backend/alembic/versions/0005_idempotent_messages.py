"""Idempotent chat messages: client message ids and reply links.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("client_message_id", sa.String(64), nullable=True))
    op.add_column(
        "messages", sa.Column("in_reply_to", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_unique_constraint("messages_tenant_id_id_key", "messages", ["tenant_id", "id"])
    op.create_foreign_key(
        "messages_in_reply_to_fkey",
        "messages",
        "messages",
        ["tenant_id", "in_reply_to"],
        ["tenant_id", "id"],
    )
    op.create_index(
        "uq_messages_tenant_client_message_id",
        "messages",
        ["tenant_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_messages_tenant_client_message_id", table_name="messages")
    op.drop_constraint("messages_in_reply_to_fkey", "messages", type_="foreignkey")
    op.drop_constraint("messages_tenant_id_id_key", "messages", type_="unique")
    op.drop_column("messages", "in_reply_to")
    op.drop_column("messages", "client_message_id")
