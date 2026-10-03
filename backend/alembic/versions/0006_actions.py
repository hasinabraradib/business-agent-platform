"""Action tools: catalogue items, reservations, orders, leads, pending confirmations, tool-call
records and webhooks.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-03

Every new table is tenant-owned: forced RLS with the tenant_isolation policy and explicit grants
to bap_app. References to conversations/documents/messages are composite (tenant_id, id), so no
row can point at another tenant's data even though foreign-key checks bypass RLS.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bap_app"
CURRENT_TENANT = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def _id() -> sa.Column:
    return sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column(
        "tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def _ts(name: str, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def _conversation_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "conversation_id"],
        ["conversations.tenant_id", "conversations.id"],
        ondelete="SET NULL (conversation_id)",
        name=name,
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


TABLES = (
    "webhook_deliveries", "webhook_endpoints", "tool_calls", "leads", "orders",
    "reservations", "pending_actions", "catalog_items",
)  # fmt: skip


def upgrade() -> None:
    op.add_column("documents", sa.Column("catalog_mapping", JSONB, nullable=True))
    op.drop_constraint("messages_outcome_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_outcome_check",
        "messages",
        "outcome IS NULL OR outcome IN ('answered', 'action', 'no_answer', 'smalltalk', 'error')",
    )

    op.create_table(
        "catalog_items",
        _id(),
        _tenant(),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("row", sa.Integer(), nullable=False),
        sa.Column("chunk_id", UUID, sa.ForeignKey("chunks.id", ondelete="SET NULL")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("alt_name", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("price", sa.Numeric(12, 2)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("in_stock", sa.Boolean()),
        sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="catalog_items_document_fkey",
        ),
        sa.UniqueConstraint("document_id", "row", name="catalog_items_document_row_key"),
    )
    op.create_index("ix_catalog_items_tenant_category", "catalog_items", ["tenant_id", "category"])

    op.create_table(
        "pending_actions",
        _id(),
        _tenant(),
        sa.Column("conversation_id", UUID),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("args_hash", sa.String(64), nullable=False),
        sa.Column("proposed_in", UUID, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_id", UUID),
        sa.Column("result_summary", sa.Text()),
        _ts("created_at"),
        _ts("updated_at"),
        _conversation_fk("pending_actions_conversation_fkey"),
        sa.CheckConstraint("status IN ('pending', 'done')", name="pending_actions_status_check"),
    )
    op.create_index(
        "ix_pending_actions_lookup", "pending_actions", ["conversation_id", "tool", "args_hash"]
    )

    op.create_table(
        "reservations",
        _id(),
        _tenant(),
        sa.Column("conversation_id", UUID),
        sa.Column("pending_action_id", UUID),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("local_time", sa.Time(), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="confirmed"),
        _ts("created_at"),
        _ts("updated_at"),
        _conversation_fk("reservations_conversation_fkey"),
        sa.UniqueConstraint("tenant_id", "reference", name="reservations_tenant_reference_key"),
        sa.UniqueConstraint("pending_action_id", name="reservations_pending_action_key"),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'completed', 'no_show')",
            name="reservations_status_check",
        ),
        sa.CheckConstraint("party_size > 0", name="reservations_party_size_check"),
    )
    op.create_index("ix_reservations_tenant_starts_at", "reservations", ["tenant_id", "starts_at"])

    op.create_table(
        "orders",
        _id(),
        _tenant(),
        sa.Column("order_number", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("items", JSONB, nullable=False),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        _ts("shipped_at", nullable=True),
        _ts("delivered_at", nullable=True),
        sa.Column("courier", sa.Text()),
        _ts("created_at"),
        sa.UniqueConstraint("tenant_id", "order_number", name="orders_tenant_number_key"),
        sa.CheckConstraint(
            "status IN ('processing', 'shipped', 'delivered', 'cancelled')",
            name="orders_status_check",
        ),
    )

    op.create_table(
        "leads",
        _id(),
        _tenant(),
        sa.Column("conversation_id", UUID),
        sa.Column("pending_action_id", UUID),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=False),
        sa.Column("interest", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        _ts("created_at"),
        _conversation_fk("leads_conversation_fkey"),
        sa.UniqueConstraint("pending_action_id", name="leads_pending_action_key"),
        sa.CheckConstraint("status IN ('new', 'contacted', 'closed')", name="leads_status_check"),
    )

    op.create_table(
        "tool_calls",
        _id(),
        _tenant(),
        sa.Column("conversation_id", UUID),
        sa.Column("message_id", UUID, nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Numeric(10, 1), nullable=False),
        _ts("created_at"),
        _conversation_fk("tool_calls_conversation_fkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["messages.tenant_id", "messages.id"],
            ondelete="CASCADE",
            name="tool_calls_message_fkey",
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'invalid_arguments', 'not_allowed', 'refused', "
            "'needs_confirmation', 'rate_limited', 'error')",
            name="tool_calls_status_check",
        ),
    )
    op.create_index("ix_tool_calls_message", "tool_calls", ["message_id"])

    op.create_table(
        "webhook_endpoints",
        _id(),
        _tenant(),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("tenant_id", name="webhook_endpoints_tenant_key"),
    )

    op.create_table(
        "webhook_deliveries",
        _id(),
        _tenant(),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_log", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_status_code", sa.Integer()),
        sa.Column("last_error", sa.Text()),
        _ts("next_attempt_at", nullable=True),
        _ts("delivered_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'retrying', 'delivered', 'failed')",
            name="webhook_deliveries_status_check",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_tenant_created", "webhook_deliveries", ["tenant_id", "created_at"]
    )

    _protect("catalog_items", "SELECT, INSERT, DELETE")  # replaced wholesale on re-ingest
    _protect("pending_actions", "SELECT, INSERT, UPDATE")
    _protect("reservations", "SELECT, INSERT, UPDATE")
    _protect("orders", "SELECT")  # managed by the shop's systems / the CLI, read-only here
    _protect("leads", "SELECT, INSERT, UPDATE")
    _protect("tool_calls", "SELECT, INSERT")  # an append-only record
    _protect("webhook_endpoints", "SELECT, INSERT, UPDATE")
    _protect("webhook_deliveries", "SELECT, INSERT, UPDATE")


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
    op.drop_constraint("messages_outcome_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_outcome_check",
        "messages",
        "outcome IS NULL OR outcome IN ('answered', 'no_answer', 'smalltalk', 'error')",
    )
    op.drop_column("documents", "catalog_mapping")
