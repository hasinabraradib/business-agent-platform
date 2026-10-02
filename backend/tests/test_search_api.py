"""POST /v1/search: shape, validation, auth and tenant isolation through the API."""

import pytest
from httpx import AsyncClient

from app.retrieval import get_retriever
from app.retrieval.rerank import FakeReranker
from tests.conftest import bearer
from tests.retrieval_helpers import add_document, make_retriever


@pytest.fixture(autouse=True)
def retriever(app, app_engine):
    instance = make_retriever(app_engine, reranker=FakeReranker())
    app.dependency_overrides[get_retriever] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_retriever, None)


@pytest.fixture
async def twins(make_tenant, owner_engine):
    a = await make_tenant("api-twin-a")
    b = await make_tenant("api-twin-b")
    content = [
        ("Kacchi biryani costs 480 taka.", {"row": 1, "source": "menu.csv"}),
        ("Borhani is a spiced yoghurt drink.", {"row": 2, "source": "menu.csv"}),
    ]
    ids_a = await add_document(owner_engine, a.id, "Menu", content)
    ids_b = await add_document(owner_engine, b.id, "Menu", content)
    return a, b, {str(i) for i in ids_a}, {str(i) for i in ids_b}


async def search(client: AsyncClient, key: str, **body):
    return await client.post("/v1/search", json=body, headers=bearer(key))


async def test_search_returns_chunks_scores_confidence_and_timings(client, twins) -> None:
    a, _, ids_a, _ = twins
    response = await search(client, a.admin_key, query="How much is kacchi biryani?", top_k=2)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "hybrid"
    assert body["query"] == "How much is kacchi biryani?"
    top = body["results"][0]
    assert top["rank"] == 1
    assert top["content"] == "Kacchi biryani costs 480 taka."
    assert top["document_title"] == "Menu"
    assert top["metadata"] == {"row": 1, "source": "menu.csv"}
    assert set(top["scores"]) == {"vector", "keyword", "fused", "rerank"}
    assert top["scores"]["vector"] is not None and top["scores"]["fused"] is not None
    assert top["scores"]["rerank"] is None
    assert {r["chunk_id"] for r in body["results"]} <= ids_a
    assert body["confidence"]["has_relevant_context"] is True
    assert body["confidence"]["top_vector_similarity"] >= body["confidence"]["threshold"]
    assert body["embedding_model"] == "fake-hashing-768-v2"
    assert body["reranker"] is None
    assert {"embed", "vector", "keyword", "fuse", "hydrate", "total"} <= set(body["timings_ms"])


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid", "hybrid_rerank"])
async def test_every_mode_is_available_and_isolated(client, twins, mode) -> None:
    a, b, ids_a, ids_b = twins
    for tenant, own in ((a, ids_a), (b, ids_b)):
        response = await search(
            client, tenant.admin_key, query="kacchi borhani", mode=mode, top_k=10
        )
        assert response.status_code == 200
        body = response.json()
        assert {r["chunk_id"] for r in body["results"]} == own
    if mode == "hybrid_rerank":
        assert body["reranker"] == {"name": "fake", "applied": True, "error": None}
        assert all(r["scores"]["rerank"] is not None for r in body["results"])
        assert "rerank" in body["timings_ms"]


async def test_off_topic_search_reports_no_relevant_context(client, twins) -> None:
    a, *_ = twins
    body = (await search(client, a.admin_key, query="lattice quantum chromodynamics")).json()
    assert body["confidence"]["has_relevant_context"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "x" * 1001},
        {"query": "ok", "mode": "semantic"},
        {"query": "ok", "top_k": 0},
        {"query": "ok", "top_k": 51},
        {},
    ],
)
async def test_invalid_search_requests_are_rejected(client, twins, body) -> None:
    a, *_ = twins
    response = await client.post("/v1/search", json=body, headers=bearer(a.admin_key))
    assert response.status_code == 422


async def test_search_requires_an_admin_key(client, twins) -> None:
    a, *_ = twins
    assert (await client.post("/v1/search", json={"query": "q"})).status_code == 401
    widget = await search(client, a.widget_key, query="q")
    assert widget.status_code == 403
