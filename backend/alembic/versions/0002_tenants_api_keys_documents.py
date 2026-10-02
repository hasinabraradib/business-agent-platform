"""Tenants, API keys and documents, with Row-Level Security.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02

Every tenant-owned table gets ENABLE + FORCE ROW LEVEL SECURITY and a policy comparing
tenant_id to the per-transaction setting app.current_tenant. The application role (bap_app) is
granted only what the API needs. API-key lookup happens before a tenant is known, so it goes
through a SECURITY DEFINER function rather than a broad policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bap_app"
# NULLIF: once set_config(..., true) has been used in a session, the unset value is '' not NULL.
CURRENT_TENANT = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"


def _enable_rls(table: str, column: str = "tenant_id") -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({column} = {CURRENT_TENANT}) WITH CHECK ({column} = {CURRENT_TENANT})"
    )


def upgrade() -> None:
    # The role is normally created with a password by `python -m app.cli ensure-app-role`;
    # create it here (without login) only so grants never fail on a fresh cluster.
    op.execute(
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN "
        f"CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS; "
        f"END IF; END $$"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")

    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False, unique=True),
        sa.Column(
            "settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "api_keys",
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
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("prefix", sa.String(8), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('admin', 'widget')", name="api_keys_kind_check"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "documents",
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
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])

    # A tenant can see only its own tenants row; api_keys and documents are tenant-owned.
    _enable_rls("tenants", column="id")
    _enable_rls("api_keys")
    _enable_rls("documents")

    # Tenants are created by the platform CLI (owner role), never by the API.
    op.execute(f"GRANT SELECT, UPDATE ON tenants TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON api_keys TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO {APP_ROLE}")

    # Resolves an API key hash to its tenant before any tenant context exists. Runs as the owner
    # (which bypasses RLS), returns only active keys, and exposes nothing beyond these columns.
    op.execute(
        """
        CREATE FUNCTION resolve_api_key(p_key_hash text)
        RETURNS TABLE (api_key_id uuid, tenant_id uuid, kind text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT k.id, k.tenant_id, k.kind::text
            FROM api_keys AS k
            WHERE k.key_hash = p_key_hash AND k.revoked_at IS NULL
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION resolve_api_key(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_api_key(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_api_key(text)")
    op.drop_table("documents")
    op.drop_table("api_keys")
    op.drop_table("tenants")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    # The role itself is cluster-wide (shared with other databases) and is left in place.
