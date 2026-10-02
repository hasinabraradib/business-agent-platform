import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url

from alembic import command
from app.config import Settings, get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Point the app at the test database before anything reads settings or builds an engine.
_settings = Settings()
TEST_DATABASE_URL = _settings.test_database_url
if make_url(TEST_DATABASE_URL).database == make_url(_settings.database_url).database:
    raise RuntimeError("TEST_DATABASE_URL must point at a different database than DATABASE_URL")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
get_settings.cache_clear()


async def _create_database_if_missing(url: str) -> None:
    target = make_url(url)
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


@pytest.fixture(scope="session", autouse=True)
def test_database() -> str:
    """Create the test database if needed and migrate it to head.

    Sync on purpose: Alembic's async env.py starts its own event loop.
    """
    asyncio.run(_create_database_if_missing(TEST_DATABASE_URL))
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


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
