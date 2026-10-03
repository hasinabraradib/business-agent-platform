import asyncio
import os
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from alembic import command
from app.config import Settings, get_settings
from app.db_roles import ensure_app_role
from app.models import Document
from app.security import new_api_key
from app.tenants import create_tenant

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
# Redis: same server, a dedicated logical database that tests may flush.
TEST_REDIS_URL = _settings.redis_url.rsplit("/", 1)[0] + "/15"
os.environ["REDIS_URL"] = TEST_REDIS_URL
get_settings.cache_clear()

# Files written outside the per-test storage fixture (e.g. by CLI subprocesses) go here.
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="bap-test-uploads-")

# Tests never call a real AI API, even if .env has a key.
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["GEMINI_API_KEY"] = ""
os.environ["CHAT_PROVIDER"] = "fake"
os.environ["RERANKER"] = "noop"

# ...or the network at all: only loopback hosts resolve (Postgres and Redis run locally).
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_real_getaddrinfo = socket.getaddrinfo


def _loopback_only_getaddrinfo(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else host
    if name not in _LOOPBACK_HOSTS:
        raise OSError(f"Network access is disabled in tests (tried to resolve {name!r})")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _loopback_only_getaddrinfo


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


@dataclass(frozen=True)
class TenantFixture:
    id: uuid.UUID
    slug: str
    admin_key: str
    widget_key: str


@pytest.fixture
def make_tenant(owner_engine: AsyncEngine) -> Callable[..., Awaitable[TenantFixture]]:
    """Create a tenant (as the owner role) with an admin key, a widget key and documents."""

    async def _make(slug: str, documents: tuple[str, ...] = ()) -> TenantFixture:
        async with AsyncSession(owner_engine, expire_on_commit=False) as session:
            tenant, admin = await create_tenant(session, name=slug.title(), slug=slug)
            widget = new_api_key(tenant.id, "widget", label="Website widget")
            session.add(widget.record)
            session.add_all(
                Document(tenant_id=tenant.id, title=title, source_type="upload")
                for title in documents
            )
            await session.commit()
        return TenantFixture(tenant.id, slug, admin.full_key, widget.full_key)

    return _make


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class RecordingQueue:
    """Stands in for Redis: records jobs so tests can run the worker step explicitly."""

    def __init__(self) -> None:
        self.jobs: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.webhooks: list[tuple[uuid.UUID, uuid.UUID, int]] = []

    async def enqueue_ingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self.jobs.append((tenant_id, document_id))

    async def enqueue_webhook(
        self, tenant_id: uuid.UUID, delivery_id: uuid.UUID, defer_seconds: int = 0
    ) -> None:
        self.webhooks.append((tenant_id, delivery_id, defer_seconds))


@pytest.fixture(autouse=True)
def job_queue(app) -> Iterator[RecordingQueue]:
    """Autouse: no test enqueues to a real Redis unless it removes this override itself."""
    from app.ingestion.queue import get_job_queue

    queue = RecordingQueue()
    app.dependency_overrides[get_job_queue] = lambda: queue
    yield queue
    app.dependency_overrides.pop(get_job_queue, None)


@pytest.fixture(autouse=True)
def storage(app, tmp_path):
    """Autouse: uploaded files go to a per-test temporary directory."""
    from app.ingestion.storage import FileStorage, get_storage

    file_storage = FileStorage(tmp_path / "uploads")
    app.dependency_overrides[get_storage] = lambda: file_storage
    yield file_storage
    app.dependency_overrides.pop(get_storage, None)
