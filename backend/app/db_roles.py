"""Provisioning for the application's database role.

The API must not connect as a superuser or as the table owner: both bypass Row-Level Security.
It connects as APP_DB_ROLE instead, which has only the grants the migrations give it.
"""

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

APP_DB_ROLE = "bap_app"


async def ensure_app_role(owner_conn: AsyncConnection, app_database_url: str) -> None:
    """Create or update the application role from the credentials in app_database_url.

    Idempotent. Roles are cluster-wide, so this also covers the test database.
    """
    url = make_url(app_database_url)
    if url.username != APP_DB_ROLE:
        raise ValueError(f"DATABASE_URL must connect as {APP_DB_ROLE!r}, not {url.username!r}")
    if not url.password:
        raise ValueError("DATABASE_URL must include a password for the application role")

    exists = await owner_conn.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_DB_ROLE}
    )
    verb = "ALTER" if exists else "CREATE"
    # Let Postgres quote the identifier and password literal; DDL cannot take bind parameters.
    statement = await owner_conn.scalar(
        text(
            "SELECT format('%s ROLE %I WITH LOGIN PASSWORD %L "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT', "
            "CAST(:verb AS text), CAST(:role AS text), CAST(:password AS text))"
        ),
        {"verb": verb, "role": APP_DB_ROLE, "password": url.password},
    )
    await owner_conn.execute(text(statement))
