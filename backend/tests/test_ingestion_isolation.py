"""Tenant isolation for documents and chunks: through the API, the worker and the database."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.embeddings import FakeEmbeddingProvider
from app.ingestion.pipeline import IngestDeps, process_document
from tests.conftest import bearer


@pytest.fixture
async def ingested(client: AsyncClient, make_tenant, job_queue, storage, app_engine):
    """Tenants A and B, each with one ready document (chunks + stored file)."""
    deps = IngestDeps(
        sessionmaker=async_sessionmaker(app_engine, expire_on_commit=False),
        embedder=FakeEmbeddingProvider(),
        storage=storage,
    )
    a = await make_tenant("tenant-a")
    b = await make_tenant("tenant-b")
    docs = {}
    for tenant, body in ((a, b"## A\n\nA's secret recipe."), (b, b"## B\n\nB's supplier list.")):
        response = await client.post(
            "/v1/documents", headers=bearer(tenant.admin_key), files={"file": ("k.md", body)}
        )
        docs[tenant.slug] = response.json()["id"]
    for tenant_id, document_id in job_queue.jobs:
        assert await process_document(deps, tenant_id, document_id) == "ready"
    job_queue.jobs.clear()
    return a, b, docs["tenant-a"], docs["tenant-b"], deps


async def _state(owner_engine: AsyncEngine, document_id: str) -> tuple:
    async with owner_engine.connect() as conn:
        status = await conn.scalar(
            text("SELECT status FROM documents WHERE id = :d"), {"d": document_id}
        )
        chunk_ids = (
            await conn.scalars(
                text("SELECT id FROM chunks WHERE document_id = :d ORDER BY id"),
                {"d": document_id},
            )
        ).all()
    return status, chunk_ids


async def test_tenant_cannot_read_reingest_or_delete_another_tenants_document(
    client: AsyncClient, ingested, job_queue, storage, owner_engine
) -> None:
    a, b, _, doc_b, _ = ingested
    before = await _state(owner_engine, doc_b)
    headers = bearer(a.admin_key)

    assert (await client.get(f"/v1/documents/{doc_b}", headers=headers)).status_code == 404
    assert (
        await client.post(f"/v1/documents/{doc_b}/reingest", headers=headers)
    ).status_code == 404
    assert (await client.delete(f"/v1/documents/{doc_b}", headers=headers)).status_code == 404

    assert await _state(owner_engine, doc_b) == before
    assert before[0] == "ready" and before[1]
    assert storage.path_for(b.id, uuid.UUID(doc_b), "markdown").exists()
    assert job_queue.jobs == []
    listed = (await client.get("/v1/documents", headers=headers)).json()
    assert doc_b not in {d["id"] for d in listed}


async def test_worker_bound_to_one_tenant_cannot_touch_anothers_document(
    ingested, owner_engine
) -> None:
    a, _, _, doc_b, deps = ingested
    before = await _state(owner_engine, doc_b)
    # A job carrying tenant A with B's document id (forged or buggy) finds nothing under RLS.
    assert await process_document(deps, a.id, uuid.UUID(doc_b)) == "missing"
    assert await _state(owner_engine, doc_b) == before


async def test_app_role_with_tenant_a_cannot_see_or_delete_b_chunks(
    ingested, app_engine: AsyncEngine, owner_engine
) -> None:
    a, _, doc_a, doc_b, _ = ingested
    before = await _state(owner_engine, doc_b)
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.id)}
        )
        visible = (await conn.scalars(text("SELECT DISTINCT document_id FROM chunks"))).all()
        assert [str(d) for d in visible] == [doc_a]
        deleted = await conn.execute(
            text("DELETE FROM chunks WHERE document_id = :d"), {"d": doc_b}
        )
        assert deleted.rowcount == 0
    async with app_engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM chunks")) == 0  # no tenant set
    assert await _state(owner_engine, doc_b) == before
