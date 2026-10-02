import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from app.config import Settings, get_settings
from app.db_roles import ensure_app_role

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _with_database(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


# Point the app and the CLI at the test database before anything reads settings.
_settings = Settings()
if _settings.test_database_name == make_url(_settings.database_url).database:
    raise RuntimeError("TEST_DATABASE_NAME must differ from the database in DATABASE_URL")
TEST_DATABASE_URL = _with_database(_settings.database_url, _settings.test_database_name)
TEST_OWNER_DATABASE_URL = _with_database(_settings.owner_database_url, _settings.test_database_name)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["OWNER_DATABASE_URL"] = TEST_OWNER_DATABASE_URL
get_settings.cache_clear()


async def _prepare_database() -> None:
    target = make_url(TEST_OWNER_DATABASE_URL)
    admin_dsn = target.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target.database}"')
    finally:
        await conn.close()

    engine = create_async_engine(TEST_OWNER_DATABASE_URL)
    try:
        async with engine.begin() as owner_conn:
            await ensure_app_role(owner_conn, TEST_DATABASE_URL)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def test_database() -> str:
    """Create the test database and app role if needed, and migrate to head as the owner.

    Sync on purpose: Alembic's async env.py starts its own event loop.
    """
    asyncio.run(_prepare_database())
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_OWNER_DATABASE_URL)
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
async def owner_engine(test_database: str) -> AsyncIterator[AsyncEngine]:
    """Owner-role engine: bypasses RLS. For arranging test data and inspecting raw rows only."""
    engine = create_async_engine(TEST_OWNER_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def app_engine(test_database: str) -> AsyncIterator[AsyncEngine]:
    """Application-role engine: what the API uses, subject to RLS."""
    engine = create_async_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(owner_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with owner_engine.begin() as conn:
        await conn.execute(text("TRUNCATE tenants CASCADE"))


@pytest.fixture(scope="session")
async def app(test_database: str):
    from app.db import get_engine
    from app.main import create_app

    yield create_app()
    await get_engine().dispose()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
