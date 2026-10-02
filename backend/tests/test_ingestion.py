"""Ingestion end to end through the API, with the worker step run in-process (fake embeddings)."""

import asyncio
import json
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.embeddings import (
    EmbeddingError,
    EmbeddingInput,
    FakeEmbeddingProvider,
    GeminiEmbeddingProvider,
)
from app.ingestion.fetch import FetchedPage, FetchError
from app.ingestion.parsers import NO_EXTRACTABLE_TEXT
from app.ingestion.pipeline import INTERNAL_ERROR, IngestDeps, process_document
from app.routers.documents import get_url_resolver
from tests.conftest import bearer
from tests.pdf_factory import scanned_pdf, text_pdf

MARKDOWN = b"""# Rosa's Kitchen

## Opening hours

We are open every day from 11am to 11pm. On Fridays we close from 1pm to 2pm.

## Reservations

Book a table by phone. Groups of more than eight need a deposit.
"""

PAGES = {
    "https://kb.example/hours": FetchedPage(
        "https://kb.example/hours",
        "text/html; charset=utf-8",
        b"<title>Hours | Rosa</title><main><h1>Hours</h1><p>Open daily.</p></main>",
    )
}


async def public_resolver(host: str, port: int) -> list[str]:
    if host.endswith(".example"):
        return ["93.184.216.34"]
    raise OSError("unknown host")


async def fake_fetch(url: str) -> FetchedPage:
    if url not in PAGES:
        raise FetchError("the page returned HTTP 404")
    return PAGES[url]


@pytest.fixture
def deps(app_engine: AsyncEngine, storage) -> IngestDeps:
    # The application role, exactly like the real worker.
    return IngestDeps(
        sessionmaker=async_sessionmaker(app_engine, expire_on_commit=False),
        embedder=FakeEmbeddingProvider(),
        storage=storage,
        fetch=fake_fetch,
    )


@pytest.fixture
async def run_worker(job_queue, deps):
    async def _run(embedder=None) -> list[str]:
        if embedder is not None:
            deps.embedder = embedder
        jobs, job_queue.jobs[:] = list(job_queue.jobs), []
        return [await process_document(deps, t, d) for t, d in jobs]

    return _run


@pytest.fixture
async def tenant(make_tenant):
    return await make_tenant("rosas")


@pytest.fixture(autouse=True)
def _resolver(app):
    app.dependency_overrides[get_url_resolver] = lambda: public_resolver


async def upload(client: AsyncClient, key: str, name: str, data: bytes, **form) -> httpx.Response:
    return await client.post(
        "/v1/documents", headers=bearer(key), files={"file": (name, data)}, data=form
    )


async def get_doc(client: AsyncClient, key: str, document_id: str) -> dict:
    response = await client.get(f"/v1/documents/{document_id}", headers=bearer(key))
    assert response.status_code == 200
    return response.json()


async def add_url(client: AsyncClient, key: str, url: str) -> httpx.Response:
    return await client.post("/v1/documents/url", json={"url": url}, headers=bearer(key))


async def chunk_rows(owner_engine: AsyncEngine, document_id: str) -> list:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT id, tenant_id, chunk_index, content, metadata, embedding_model "
                    "FROM chunks WHERE document_id = :d ORDER BY chunk_index"
                ),
                {"d": document_id},
            )
        ).all()


async def test_upload_returns_202_pending_then_worker_makes_it_ready(
    client: AsyncClient, tenant, job_queue, run_worker, storage, owner_engine
) -> None:
    response = await upload(client, tenant.admin_key, "rosas-faq.md", MARKDOWN)
    assert response.status_code == 202
    document = response.json()
    assert document["status"] == "pending"
    assert document["title"] == "rosas-faq"
    assert document["source_type"] == "markdown"
    assert document["source_uri"] == "rosas-faq.md"
    assert job_queue.jobs == [(tenant.id, uuid.UUID(document["id"]))]
    assert storage.path_for(tenant.id, uuid.UUID(document["id"]), "markdown").read_bytes() == (
        MARKDOWN
    )

    assert await run_worker() == ["ready"]

    detail = await get_doc(client, tenant.admin_key, document["id"])
    assert detail["status"] == "ready"
    assert detail["error"] is None
    assert detail["chunk_count"] == 2
    rows = await chunk_rows(owner_engine, document["id"])
    assert [r.metadata["section"] for r in rows] == [
        "Rosa's Kitchen > Opening hours",
        "Rosa's Kitchen > Reservations",
    ]
    assert rows[0].content.startswith("We are open every day")
    assert {r.tenant_id for r in rows} == {tenant.id}
    assert {r.embedding_model for r in rows} == {"fake-hashing-768"}


async def test_duplicate_upload_returns_existing_document(
    client: AsyncClient, tenant, job_queue, owner_engine
) -> None:
    first = await upload(client, tenant.admin_key, "faq.md", MARKDOWN)
    second = await upload(client, tenant.admin_key, "renamed-copy.md", MARKDOWN, title="Copy")
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(job_queue.jobs) == 1
    async with owner_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM documents")) == 1


async def test_same_content_for_different_tenants_is_not_deduplicated(
    client: AsyncClient, tenant, make_tenant
) -> None:
    other = await make_tenant("other")
    a = await upload(client, tenant.admin_key, "faq.md", MARKDOWN)
    b = await upload(client, other.admin_key, "faq.md", MARKDOWN)
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["id"] != b.json()["id"]


async def test_reingest_replaces_chunks(
    client: AsyncClient, tenant, job_queue, run_worker, owner_engine
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    await run_worker()
    before = await chunk_rows(owner_engine, doc_id)

    response = await client.post(
        f"/v1/documents/{doc_id}/reingest", headers=bearer(tenant.admin_key)
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    # Until the worker finishes, the old chunks remain readable.
    assert [r.id for r in await chunk_rows(owner_engine, doc_id)] == [r.id for r in before]

    assert await run_worker() == ["ready"]
    after = await chunk_rows(owner_engine, doc_id)
    assert [r.content for r in after] == [r.content for r in before]
    assert not {r.id for r in after} & {r.id for r in before}
    async with owner_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM chunks")) == len(before)


class NaNEmbeddings(FakeEmbeddingProvider):
    """Passes the pipeline's own checks only if they are missing: NaN vectors."""

    async def embed_documents(self, documents):
        return [[float("nan")] * self.dimensions for _ in documents]


class BrokenInsertEmbeddings(FakeEmbeddingProvider):
    """Valid-looking vectors that the database rejects (wrong dimension sneaks past)."""

    dimensions = 3

    async def embed_documents(self, documents):
        return [[0.1, 0.2, 0.3] for _ in documents]


async def test_failed_chunk_swap_keeps_previous_chunks(
    client: AsyncClient, tenant, run_worker, owner_engine
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    await run_worker()
    before = [r.id for r in await chunk_rows(owner_engine, doc_id)]

    await client.post(f"/v1/documents/{doc_id}/reingest", headers=bearer(tenant.admin_key))
    # 3-dim vectors fail on INSERT into vector(768), after the old chunks were deleted in the
    # same transaction: the whole swap must roll back.
    assert await run_worker(BrokenInsertEmbeddings()) == ["failed"]

    assert [r.id for r in await chunk_rows(owner_engine, doc_id)] == before
    detail = await get_doc(client, tenant.admin_key, doc_id)
    assert (detail["status"], detail["error"], detail["chunk_count"]) == (
        "failed",
        INTERNAL_ERROR,
        len(before),
    )


async def test_invalid_vectors_are_rejected_before_storing(
    client: AsyncClient, tenant, run_worker
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    assert await run_worker(NaNEmbeddings()) == ["failed"]
    detail = await get_doc(client, tenant.admin_key, doc_id)
    assert detail["error"] == "embedding provider returned an invalid vector"


async def test_delete_removes_document_chunks_and_file(
    client: AsyncClient, tenant, run_worker, storage, owner_engine
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    await run_worker()
    path = storage.path_for(tenant.id, uuid.UUID(doc_id), "markdown")
    assert path.exists()
    assert await chunk_rows(owner_engine, doc_id)

    response = await client.delete(f"/v1/documents/{doc_id}", headers=bearer(tenant.admin_key))
    assert response.status_code == 204
    assert not path.exists()
    assert await chunk_rows(owner_engine, doc_id) == []
    missing = await client.get(f"/v1/documents/{doc_id}", headers=bearer(tenant.admin_key))
    assert missing.status_code == 404


async def test_worker_skips_document_deleted_before_it_ran(
    client: AsyncClient, tenant, run_worker
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    await client.delete(f"/v1/documents/{doc_id}", headers=bearer(tenant.admin_key))
    assert await run_worker() == ["missing"]


async def test_csv_upload_gets_one_chunk_per_row(
    client: AsyncClient, tenant, run_worker, owner_engine
) -> None:
    csv_data = "dish,dish_bn,price_bdt\nKacchi Biryani,কাচ্চি বিরিয়ানি,450\nBorhani,বোরহানি,80\n"
    doc_id = (await upload(client, tenant.admin_key, "menu.csv", csv_data.encode())).json()["id"]
    await run_worker()
    rows = await chunk_rows(owner_engine, doc_id)
    assert [(r.metadata["row"], r.content) for r in rows] == [
        (1, "dish: Kacchi Biryani\ndish_bn: কাচ্চি বিরিয়ানি\nprice_bdt: 450"),
        (2, "dish: Borhani\ndish_bn: বোরহানি\nprice_bdt: 80"),
    ]
    assert {r.metadata["source"] for r in rows} == {"menu.csv"}


async def test_bengali_survives_the_full_pipeline_unchanged(
    client: AsyncClient, tenant, run_worker, owner_engine
) -> None:
    bengali = "আমরা প্রতিদিন সকাল ১১টা থেকে রাত ১১টা পর্যন্ত খোলা থাকি। মেনুতে কাচ্চি বিরিয়ানি আছে।"
    md = f"## সময়সূচি\n\n{bengali}\n".encode()
    doc_id = (await upload(client, tenant.admin_key, "bn.md", md)).json()["id"]
    await run_worker()
    rows = await chunk_rows(owner_engine, doc_id)
    assert [r.content for r in rows] == [bengali]
    async with owner_engine.connect() as conn:
        hit = await conn.scalar(
            text("SELECT count(*) FROM chunks WHERE tsv @@ plainto_tsquery('simple', 'বিরিয়ানি')")
        )
    assert hit == 1


async def test_text_pdf_is_ingested_with_page_numbers(
    client: AsyncClient, tenant, run_worker, owner_engine
) -> None:
    pdf = text_pdf([["Delivery inside Dhaka takes two days."], ["Outside Dhaka takes five days."]])
    doc_id = (await upload(client, tenant.admin_key, "shipping.pdf", pdf)).json()["id"]
    assert await run_worker() == ["ready"]
    rows = await chunk_rows(owner_engine, doc_id)
    assert rows[0].metadata == {"page": 1, "page_end": 2, "source": "shipping.pdf"}


async def test_scanned_pdf_fails_with_no_ocr_error(client: AsyncClient, tenant, run_worker) -> None:
    doc_id = (await upload(client, tenant.admin_key, "scan.pdf", scanned_pdf())).json()["id"]
    assert await run_worker() == ["failed"]
    detail = await get_doc(client, tenant.admin_key, doc_id)
    assert detail["status"] == "failed"
    assert detail["error"] == NO_EXTRACTABLE_TEXT == "no extractable text; OCR is not supported yet"
    assert detail["chunk_count"] == 0


@pytest.mark.parametrize(
    ("name", "data", "status", "detail"),
    [
        ("report.docx", b"PK\x03\x04", 415, "Unsupported file type"),
        ("image.png", b"\x89PNG", 415, "Unsupported file type"),
        ("noext", b"hello", 415, "Unsupported file type"),
        ("fake.pdf", b"not a pdf at all", 415, "not a valid PDF"),
        ("latin1.csv", "café,1".encode("latin-1"), 415, "UTF-8"),
        ("empty.txt", b"  \n", 400, "empty"),
    ],
)
async def test_bad_uploads_are_rejected(
    client: AsyncClient, tenant, job_queue, name, data, status, detail
) -> None:
    response = await upload(client, tenant.admin_key, name, data)
    assert response.status_code == status
    assert detail in response.json()["detail"]
    assert job_queue.jobs == []


async def test_uploads_over_the_size_limit_are_rejected(
    client: AsyncClient, tenant, job_queue, monkeypatch
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1024)
    response = await upload(client, tenant.admin_key, "big.txt", b"a" * 1025)
    assert response.status_code == 413
    assert "limit" in response.json()["detail"]
    ok = await upload(client, tenant.admin_key, "fits.txt", b"a" * 1024)
    assert ok.status_code == 202


async def test_url_document_is_fetched_by_worker(
    client: AsyncClient, tenant, job_queue, run_worker, owner_engine
) -> None:
    response = await add_url(client, tenant.admin_key, "https://kb.example/hours#today")
    assert response.status_code == 202
    doc = response.json()
    assert (doc["source_type"], doc["source_uri"], doc["title"]) == (
        "url",
        "https://kb.example/hours",
        "https://kb.example/hours",
    )
    assert await run_worker() == ["ready"]
    detail = await get_doc(client, tenant.admin_key, doc["id"])
    assert detail["title"] == "Hours | Rosa"
    assert detail["content_hash"] is not None
    rows = await chunk_rows(owner_engine, doc["id"])
    assert [(r.metadata["section"], r.content) for r in rows] == [("Hours", "Open daily.")]

    again = await add_url(client, tenant.admin_key, "https://kb.example/hours")
    assert again.status_code == 200
    assert again.json()["id"] == doc["id"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest",
        "ftp://kb.example/x",
        "http://10.1.1.1/",
    ],
)
async def test_unsafe_urls_are_rejected_by_the_api(
    client: AsyncClient, tenant, job_queue, url: str
) -> None:
    response = await add_url(client, tenant.admin_key, url)
    assert response.status_code == 400
    assert job_queue.jobs == []


async def test_unreachable_url_fails_with_message(client: AsyncClient, tenant, run_worker) -> None:
    doc = (await add_url(client, tenant.admin_key, "https://kb.example/missing")).json()
    assert await run_worker() == ["failed"]
    detail = await get_doc(client, tenant.admin_key, doc["id"])
    assert detail["error"] == "the page returned HTTP 404"


class FailingEmbeddings(FakeEmbeddingProvider):
    async def embed_documents(self, documents):
        raise EmbeddingError("Gemini embedding request failed (403): API key not valid")


async def test_embedding_error_marks_document_failed(
    client: AsyncClient, tenant, run_worker, owner_engine
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    assert await run_worker(FailingEmbeddings()) == ["failed"]
    detail = await get_doc(client, tenant.admin_key, doc_id)
    assert detail["status"] == "failed"
    assert detail["error"] == "Gemini embedding request failed (403): API key not valid"
    assert await chunk_rows(owner_engine, doc_id) == []


class ExplodingEmbeddings(FakeEmbeddingProvider):
    async def embed_documents(self, documents):
        raise RuntimeError("secret internal detail")


async def test_unexpected_errors_fail_with_a_generic_message(
    client: AsyncClient, tenant, run_worker
) -> None:
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    assert await run_worker(ExplodingEmbeddings()) == ["failed"]
    detail = await get_doc(client, tenant.admin_key, doc_id)
    assert detail["error"] == INTERNAL_ERROR
    assert "secret" not in detail["error"]


async def test_rate_limited_embedding_is_retried_and_succeeds(
    client: AsyncClient, tenant, run_worker
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "Resource exhausted"}})
        count = len(json.loads(request.content)["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [0.5] * 768}] * count})

    async def no_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    gemini = GeminiEmbeddingProvider(
        "test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=no_sleep
    )
    doc_id = (await upload(client, tenant.admin_key, "faq.md", MARKDOWN)).json()["id"]
    assert await run_worker(gemini) == ["ready"]
    assert len(calls) == 2
    detail = await get_doc(client, tenant.admin_key, doc_id)
    assert (detail["status"], detail["chunk_count"]) == ("ready", 2)


async def test_pipeline_embeds_with_title_and_section_prefix(
    client: AsyncClient, tenant, run_worker
) -> None:
    captured: list[EmbeddingInput] = []

    class Capture(FakeEmbeddingProvider):
        async def embed_documents(self, documents):
            captured.extend(documents)
            return await super().embed_documents(documents)

    await upload(client, tenant.admin_key, "faq.md", MARKDOWN, title="Rosa's FAQ")
    assert await run_worker(Capture()) == ["ready"]
    assert [c.title for c in captured] == ["Rosa's FAQ", "Rosa's FAQ"]
    assert captured[0].text.startswith("Rosa's Kitchen > Opening hours\n\nWe are open")
