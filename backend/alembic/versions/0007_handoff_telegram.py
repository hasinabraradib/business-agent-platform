"""Human handoff and the Telegram channel.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04

- conversations.status becomes ai | waiting_human | human | resolved (open -> ai, closed ->
  resolved), with the handoff reason and time and the customer's name; channel gains telegram.
- messages: role "staff" (a team member) and outcome "handoff".
- telegram_channels (encrypted bot token, hashed webhook secret, staff chat), telegram_updates
  (update ids already accepted) and staff_alerts (alert message -> conversation). All three are
  tenant-owned: forced RLS with the tenant_isolation policy and explicit grants to bap_app.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bap_app"
CURRENT_TENANT = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
UUID = postgresql.UUID(as_uuid=True)


def _id() -> sa.Column:
    return sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column(
        "tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def _created() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _protect(table: str, grants: str) -> None:
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
    op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    op.drop_constraint("conversations_status_check", "conversations", type_="check")
    op.execute("UPDATE conversations SET status = 'ai' WHERE status = 'open'")
    op.execute("UPDATE conversations SET status = 'resolved' WHERE status = 'closed'")
    op.alter_column("conversations", "status", server_default="ai")
    op.create_check_constraint(
        "conversations_status_check",
        "conversations",
        "status IN ('ai', 'waiting_human', 'human', 'resolved')",
    )
    _replace_check("conversations", "conversations_channel_check", "channel IN ('web', 'telegram')")
    op.add_column("conversations", sa.Column("customer_name", sa.String(120)))
    op.add_column("conversations", sa.Column("handoff_reason", sa.Text()))
    op.add_column("conversations", sa.Column("handoff_requested_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_conversations_tenant_status_updated",
        "conversations",
        ["tenant_id", "status", "updated_at"],
    )
    _replace_check("messages", "messages_role_check", "role IN ('user', 'assistant', 'staff')")
    _replace_check(
        "messages",
        "messages_outcome_check",
        "outcome IS NULL OR outcome IN "
        "('answered', 'action', 'no_answer', 'smalltalk', 'handoff', 'error')",
    )

    op.create_table(
        "telegram_channels",
        _id(),
        _tenant(),
        sa.Column("bot_token_encrypted", sa.Text(), nullable=False),
        sa.Column("bot_username", sa.String(64)),
        sa.Column("webhook_secret_hash", sa.String(64)),
        sa.Column("staff_chat_id", sa.BigInteger()),
        _created(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", name="telegram_channels_tenant_key"),
    )
    _protect("telegram_channels", "SELECT, INSERT, UPDATE, DELETE")

    op.create_table(
        "telegram_updates",
        _id(),
        _tenant(),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        _created(),
        sa.UniqueConstraint("tenant_id", "update_id", name="telegram_updates_update_key"),
    )
    _protect("telegram_updates", "SELECT, INSERT")

    op.create_table(
        "staff_alerts",
        _id(),
        _tenant(),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        _created(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="CASCADE",
            name="staff_alerts_conversation_fkey",
        ),
        sa.UniqueConstraint(
            "tenant_id", "chat_id", "telegram_message_id", name="staff_alerts_message_key"
        ),
    )
    _protect("staff_alerts", "SELECT, INSERT")


def downgrade() -> None:
    for table in ("staff_alerts", "telegram_updates", "telegram_channels"):
        op.drop_table(table)
    op.execute("DELETE FROM messages WHERE role = 'staff'")
    op.execute("UPDATE messages SET outcome = 'smalltalk' WHERE outcome = 'handoff'")
    _replace_check(
        "messages",
        "messages_outcome_check",
        "outcome IS NULL OR outcome IN ('answered', 'action', 'no_answer', 'smalltalk', 'error')",
    )
    _replace_check("messages", "messages_role_check", "role IN ('user', 'assistant')")
    op.drop_index("ix_conversations_tenant_status_updated", "conversations")
    for column in ("handoff_requested_at", "handoff_reason", "customer_name"):
        op.drop_column("conversations", column)
    op.execute("DELETE FROM conversations WHERE channel = 'telegram'")
    _replace_check("conversations", "conversations_channel_check", "channel IN ('web')")
    op.drop_constraint("conversations_status_check", "conversations", type_="check")
    op.execute("UPDATE conversations SET status = 'closed' WHERE status = 'resolved'")
    op.execute("UPDATE conversations SET status = 'open' WHERE status <> 'closed'")
    op.alter_column("conversations", "status", server_default="open")
    op.create_check_constraint(
        "conversations_status_check", "conversations", "status IN ('open', 'closed')"
    )
