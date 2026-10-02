from collections.abc import AsyncIterator

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import get_engine, get_session

# Nothing listens on port 1, so connecting fails fast.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://user:pass@127.0.0.1:1/nowhere"


async def _unreachable_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(UNREACHABLE_DATABASE_URL, connect_args={"timeout": 2})
    try:
        async with AsyncSession(engine) as session:
            yield session
    finally:
        await engine.dispose()


async def test_live_returns_200(client: AsyncClient) -> None:
    response = await client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_live_does_not_touch_database(app, client: AsyncClient) -> None:
    app.dependency_overrides[get_session] = _unreachable_session
    response = await client.get("/live")
    assert response.status_code == 200


async def test_health_returns_200_with_database(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "up"}


async def test_health_returns_503_when_database_down(app, client: AsyncClient) -> None:
    app.dependency_overrides[get_session] = _unreachable_session
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "down"}


async def test_migrations_enable_pgvector(test_database: str) -> None:
    async with get_engine().connect() as conn:
        result = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
        assert result.scalar() == 1
    assert get_engine().url.database == make_url(test_database).database
