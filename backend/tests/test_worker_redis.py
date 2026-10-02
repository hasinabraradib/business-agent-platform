"""The real queue: API enqueues to Redis with arq; an arq worker (burst mode) processes it."""

import pytest
from arq import Worker
from arq.connections import RedisSettings, create_pool
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.embeddings import FakeEmbeddingProvider
from app.ingestion.pipeline import IngestDeps
from app.ingestion.queue import get_job_queue
from app.worker import WorkerSettings, startup
from tests.conftest import TEST_REDIS_URL, bearer


@pytest.fixture
async def redis_settings():
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    pool = await create_pool(settings)
    await pool.flushdb()
    yield settings
    await pool.flushdb()
    await pool.aclose()
    await get_job_queue().aclose()


async def test_upload_is_processed_by_arq_worker_via_redis(
    app, client: AsyncClient, make_tenant, storage, app_engine: AsyncEngine, redis_settings
) -> None:
    app.dependency_overrides.pop(get_job_queue)  # use the real arq queue on test Redis db 15
    tenant = await make_tenant("queue-test")
    response = await client.post(
        "/v1/documents",
        headers=bearer(tenant.admin_key),
        files={"file": ("faq.md", b"## Parking\n\nFree parking behind the restaurant.")},
    )
    assert response.status_code == 202
    document_id = response.json()["id"]

    deps = IngestDeps(
        sessionmaker=async_sessionmaker(app_engine, expire_on_commit=False),
        embedder=FakeEmbeddingProvider(),
        storage=storage,
    )
    worker = Worker(
        functions=WorkerSettings.functions,
        redis_settings=redis_settings,
        on_startup=startup,
        ctx={"deps": deps},
        burst=True,
        poll_delay=0.05,
        handle_signals=False,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()
    assert (worker.jobs_complete, worker.jobs_failed) == (1, 0)

    detail = (
        await client.get(f"/v1/documents/{document_id}", headers=bearer(tenant.admin_key))
    ).json()
    assert (detail["status"], detail["chunk_count"]) == ("ready", 1)
