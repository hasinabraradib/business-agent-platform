"""Retrieval: keyword, vector, hybrid, rerank, confidence, cache, filtered-index fix, isolation."""

import asyncio
import json
import random
import re

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.retrieval.cache import RedisQueryEmbeddingCache
from app.retrieval.rerank import (
    FakeReranker,
    LLMReranker,
    RerankCandidate,
    Reranker,
    RerankError,
    parse_scores,
)
from app.retrieval.search_sql import VECTOR_SQL, compound_tokens
from app.tenancy import tenant_db
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.retrieval_helpers import FAKE, CountingEmbedder, add_document, make_retriever

FILLER = [
    "We are open every day from noon until eleven at night.",
    "Reservations can be made by phone for groups of any size.",
    "Parking is available in front of the building after six.",
    "We accept cash, cards and mobile payments at the counter.",
    "Children are welcome and high chairs are available on request.",
]


@pytest.fixture
async def shop(make_tenant, owner_engine):
    """A tenant whose corpus exercises keyword matching."""
    tenant = await make_tenant("kw-shop")
    ids = {}
    ids["sar001"], ids["sar002"], ids["kan001"] = await add_document(
        owner_engine,
        tenant.id,
        "Catalogue",
        [
            "sku: JL-SAR-001\nname: Dhakai Jamdani Saree",
            "sku: JL-SAR-002\nname: Half-silk Jamdani Saree",
            "sku: JL-KAN-001\nname: Nakshi Kantha Throw",
        ],
    )
    ids["borhani"], ids["biryani"], ids["combo"] = await add_document(
        owner_engine,
        tenant.id,
        "Menu",
        [
            "Borhani is a spiced yoghurt drink with mint.",
            "Our biryani is cooked with mutton and potato.",
            "The biryani and borhani combo costs 550 taka.",
        ],
    )
    ids["doi"], ids["firni"] = await add_document(
        owner_engine,
        tenant.id,
        "মিষ্টি",
        ["মিষ্টি দই মাটির পাত্রে পরিবেশন করা হয়।", "ফিরনি এলাচ দিয়ে তৈরি।"],
    )
    await add_document(owner_engine, tenant.id, "About", FILLER)
    return tenant, ids


async def _ids(retriever, tenant, query, mode, top_k=5):
    result = await retriever.retrieve(tenant.id, query, mode, top_k)
    return [c.chunk_id for c in result.chunks], result


# --- keyword ---------------------------------------------------------------------------------


async def test_keyword_one_strong_term_hits(app_engine, shop) -> None:
    tenant, ids = shop
    retriever = make_retriever(app_engine)
    ranked, result = await _ids(retriever, tenant, "Do you have borhani?", "keyword")
    assert ranked[0] in {ids["borhani"], ids["combo"]}
    assert set(ranked) <= {ids["borhani"], ids["combo"]}  # only chunks containing the term
    assert all(c.keyword_score > 0 for c in result.chunks)


async def test_keyword_more_matching_terms_rank_higher(app_engine, shop) -> None:
    tenant, ids = shop
    query = "what is the price of the biryani borhani combo"  # "price" appears nowhere
    ranked, result = await _ids(make_retriever(app_engine), tenant, query, "keyword")
    assert ranked[0] == ids["combo"]
    assert {ids["borhani"], ids["biryani"]} <= set(ranked[1:])
    scores = [c.keyword_score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)


async def test_keyword_bengali_term_matches_exactly(app_engine, shop) -> None:
    tenant, ids = shop
    retriever = make_retriever(app_engine)
    ranked, _ = await _ids(retriever, tenant, "ফিরনি", "keyword")
    assert ranked == [ids["firni"]]
    ranked, _ = await _ids(retriever, tenant, "মিষ্টি দই আছে?", "keyword")
    assert ranked[0] == ids["doi"]


async def test_keyword_product_code_matches_exactly(app_engine, shop) -> None:
    tenant, ids = shop
    retriever = make_retriever(app_engine)
    ranked, result = await _ids(retriever, tenant, "Is JL-SAR-001 in stock?", "keyword")
    assert ranked[0] == ids["sar001"]
    by_id = {c.chunk_id: c.keyword_score for c in result.chunks}
    # The exact code clearly outranks its sibling code and the other "-001" code.
    assert by_id[ids["sar001"]] > 1.5 * by_id[ids["sar002"]]
    assert by_id[ids["sar001"]] > 1.5 * by_id[ids["kan001"]]


def test_compound_tokens_are_detected() -> None:
    found = compound_tokens("Is JL-SAR-001 or ab_12 or v2.5 in stock?")
    assert found == ["JL-SAR-001", "ab_12", "v2.5"]
    assert compound_tokens("plain words only") == []


async def test_keyword_query_of_only_question_words_still_searches(app_engine, shop) -> None:
    tenant, _ = shop
    ranked, _ = await _ids(make_retriever(app_engine), tenant, "what is the", "keyword")
    assert ranked  # stop words are only dropped when the query has other terms


# --- hybrid ----------------------------------------------------------------------------------


LONG_DESCRIPTION = (
    "Handwoven cotton runner with indigo stripes, dyed in small batches by a family "
    "workshop near the river, finished with knotted tassels at both ends, gentle on "
    "polished wood, washable in cold water, folded in recycled paper for gifting, and "
    "accompanied by a card describing the weaver, the village and the natural dyes used."
)


@pytest.fixture
async def hybrid_corpus(make_tenant, owner_engine):
    tenant = await make_tenant("hybrid")
    ids = {}
    (ids["code"],) = await add_document(
        owner_engine, tenant.id, "Runner", [f"Item ZX-4471. {LONG_DESCRIPTION}"]
    )
    ids["decoys"] = await add_document(
        owner_engine, tenant.id, "Codes", ["Item ZX-4470.", "Item ZX-4479.", "Item ZX-4417."]
    )
    (ids["shipping"],) = await add_document(
        owner_engine, tenant.id, "Delivery", ["Shipping takes three working days inside Dhaka."]
    )
    await add_document(owner_engine, tenant.id, "About", FILLER)
    return tenant, ids


async def test_hybrid_finds_exact_code_that_vector_alone_misses(app_engine, hybrid_corpus) -> None:
    tenant, ids = hybrid_corpus
    retriever = make_retriever(app_engine)
    vector, _ = await _ids(retriever, tenant, "ZX-4471", "vector", top_k=3)
    keyword, _ = await _ids(retriever, tenant, "ZX-4471", "keyword", top_k=3)
    hybrid, _ = await _ids(retriever, tenant, "ZX-4471", "hybrid", top_k=3)
    assert ids["code"] not in vector  # short look-alike codes crowd it out
    assert keyword[0] == ids["code"]
    assert ids["code"] in hybrid


async def test_hybrid_finds_paraphrase_that_keyword_alone_misses(app_engine, hybrid_corpus) -> None:
    tenant, ids = hybrid_corpus
    retriever = make_retriever(app_engine)
    query = "how long does a shipment take"  # no word in common with "Shipping takes..."
    vector, _ = await _ids(retriever, tenant, query, "vector", top_k=3)
    keyword, _ = await _ids(retriever, tenant, query, "keyword", top_k=3)
    hybrid, result = await _ids(retriever, tenant, query, "hybrid", top_k=3)
    assert ids["shipping"] not in keyword
    assert vector[0] == ids["shipping"]
    assert ids["shipping"] in hybrid
    shipping = next(c for c in result.chunks if c.chunk_id == ids["shipping"])
    assert shipping.keyword_score is None and shipping.fused_score > 0


async def test_rrf_constant_is_configurable(app_engine, hybrid_corpus) -> None:
    tenant, _ = hybrid_corpus
    _, k60 = await _ids(make_retriever(app_engine, rrf_k=60), tenant, "ZX-4471", "hybrid")
    _, k1 = await _ids(make_retriever(app_engine, rrf_k=1), tenant, "ZX-4471", "hybrid")
    assert max(c.fused_score for c in k60.chunks) <= 2 / 61 + 1e-9
    assert max(c.fused_score for c in k1.chunks) > 2 / 61


# --- the filtered HNSW index problem ---------------------------------------------------------


def _random_vector(rng: random.Random) -> str:
    v = [rng.gauss(0, 1) for _ in range(768)]
    norm = sum(x * x for x in v) ** 0.5
    return "[" + ",".join(f"{x / norm:.5f}" for x in v) + "]"


@pytest.fixture
async def small_tenant_among_thousands(make_tenant, owner_engine):
    big = await make_tenant("big-tenant")
    small = await make_tenant("small-tenant")
    rng = random.Random(42)
    async with owner_engine.begin() as conn:
        docs = {}
        for tenant in (big, small):
            docs[tenant.id] = await conn.scalar(
                text(
                    "INSERT INTO documents (tenant_id, title, source_type, status) "
                    "VALUES (:t, 'Doc', 'text', 'ready') RETURNING id"
                ),
                {"t": tenant.id},
            )
        rows = [(big.id, i) for i in range(2000)] + [(small.id, i) for i in range(8)]
        await conn.execute(
            text(
                "INSERT INTO chunks (tenant_id, document_id, chunk_index, content, embedding, "
                "embedding_model) VALUES (:t, :d, :i, 'chunk', CAST(:e AS vector), :model)"
            ),
            [
                {"t": t, "d": docs[t], "i": i, "e": _random_vector(rng), "model": FAKE.model_name}
                for t, i in rows
            ],
        )
        await conn.execute(text("ANALYZE chunks"))
    return big, small


@pytest.fixture
async def hnsw_forced_engine():
    """With sorting disabled, the planner must use the HNSW index to satisfy ORDER BY distance.

    (With fresh statistics it would otherwise pick an exact scan for an 8-row tenant and the
    problem would not show; on large tables with mid-sized tenants it picks HNSW by itself.)
    """
    engine = create_async_engine(
        TEST_DATABASE_URL, connect_args={"server_settings": {"enable_sort": "off"}}
    )
    yield engine
    await engine.dispose()


async def test_small_tenant_gets_top_k_despite_filtered_hnsw_scan(
    small_tenant_among_thousands, hnsw_forced_engine
) -> None:
    _big, small = small_tenant_among_thousands
    query_vector = await FAKE.embed_query("anything")

    # The vector query really runs on the HNSW index under this engine.
    async with tenant_db(make_retriever(hnsw_forced_engine).sessionmaker, small.id) as db:
        plan = await db.execute_sql(
            text("EXPLAIN " + str(VECTOR_SQL)).bindparams(*VECTOR_SQL._bindparams.values()),
            {"embedding": query_vector, "model": FAKE.model_name, "limit": 5},
        )
        assert "ix_chunks_embedding_hnsw" in "\n".join(row[0] for row in plan)

    async def count(**config) -> list:
        retriever = make_retriever(hnsw_forced_engine, **config)
        result = await retriever.retrieve(small.id, "anything", "vector", top_k=5)
        return [c.chunk_id for c in result.chunks]

    # The problem: a plain filtered index scan returns (almost) nothing for the small tenant.
    assert len(await count(iterative_scan="off", exact_fallback=False)) < 5
    # Fix 1: pgvector's iterative index scan.
    assert len(await count(iterative_scan="relaxed_order", exact_fallback=False)) == 5
    # Fix 2 (safety net): exact scan over the tenant's rows when the index comes up short.
    assert len(await count(iterative_scan="off", exact_fallback=True)) == 5
    # The defaults use both.
    found = await count()
    assert len(found) == 5

    async with tenant_db(make_retriever(hnsw_forced_engine).sessionmaker, small.id) as db:
        owned = await db.execute_sql(
            text("SELECT count(*) FROM chunks WHERE id = ANY(:ids) AND tenant_id = :tenant_id"),
            {"ids": found},
        )
        assert owned.scalar_one() == 5


# --- reranking -------------------------------------------------------------------------------


class ScriptedReranker(Reranker):
    name = "scripted"

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def rerank(self, query, candidates):
        if self.behaviour == "reverse":
            return [i / len(candidates) for i in range(len(candidates))]  # last becomes first
        if self.behaviour == "slow":
            await asyncio.sleep(5)
        if self.behaviour == "wrong_length":
            return [1.0]
        raise RerankError("boom")


async def test_fake_reranker_reorders_by_its_scores(app_engine, shop) -> None:
    tenant, _ = shop
    query = "spiced yoghurt drink with mint"
    reranked, result = await _ids(
        make_retriever(app_engine, reranker=FakeReranker()), tenant, query, "hybrid_rerank", top_k=5
    )
    assert result.rerank_applied and result.rerank_error is None
    assert result.reranker == "fake"
    scores = [c.rerank_score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)
    assert result.chunks[0].content == "Borhani is a spiced yoghurt drink with mint."
    assert set(reranked) <= set(
        (await _ids(make_retriever(app_engine), tenant, query, "hybrid", top_k=15))[0]
    )


async def test_scripted_reranker_order_is_applied(app_engine, shop) -> None:
    tenant, _ = shop
    query = "biryani borhani combo"
    pool, _ = await _ids(make_retriever(app_engine), tenant, query, "hybrid", top_k=15)
    reranked, _ = await _ids(
        make_retriever(app_engine, reranker=ScriptedReranker("reverse")),
        tenant,
        query,
        "hybrid_rerank",
        top_k=3,
    )
    assert reranked == list(reversed(pool))[:3]


@pytest.mark.parametrize("behaviour", ["error", "wrong_length"])
async def test_reranker_failures_fall_back_to_fused_order(app_engine, shop, behaviour) -> None:
    tenant, _ = shop
    query = "biryani borhani combo"
    hybrid, _ = await _ids(make_retriever(app_engine), tenant, query, "hybrid", top_k=5)
    reranked, result = await _ids(
        make_retriever(app_engine, reranker=ScriptedReranker(behaviour)),
        tenant,
        query,
        "hybrid_rerank",
        top_k=5,
    )
    assert reranked == hybrid
    assert not result.rerank_applied and result.rerank_error
    assert all(c.rerank_score is None for c in result.chunks)


async def test_reranker_timeout_falls_back_to_fused_order(app_engine, shop) -> None:
    tenant, _ = shop
    query = "biryani borhani combo"
    hybrid, _ = await _ids(make_retriever(app_engine), tenant, query, "hybrid", top_k=5)
    reranked, result = await _ids(
        make_retriever(app_engine, reranker=ScriptedReranker("slow"), rerank_timeout_seconds=0.1),
        tenant,
        query,
        "hybrid_rerank",
        top_k=5,
    )
    assert reranked == hybrid
    assert result.rerank_error == "timed out after 0.1s"
    assert result.timings_ms["rerank"] < 2000


def _gemini_reply(text_payload: str) -> httpx.Response:
    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": text_payload}]}}]}
    )


def _llm(handler) -> LLMReranker:
    return LLMReranker("test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_llm_reranker_malformed_output_falls_back(app_engine, shop) -> None:
    tenant, _ = shop
    query = "biryani borhani combo"
    hybrid, _ = await _ids(make_retriever(app_engine), tenant, query, "hybrid", top_k=5)
    reranker = _llm(lambda request: _gemini_reply("Sure! Here are the scores: 9, 3, 7"))
    reranked, result = await _ids(
        make_retriever(app_engine, reranker=reranker), tenant, query, "hybrid_rerank", top_k=5
    )
    assert reranked == hybrid
    assert result.rerank_error == "reranker returned malformed output"


async def test_llm_reranker_valid_output_reorders(app_engine, shop) -> None:
    tenant, _ = shop
    query = "biryani borhani combo"
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        count = len(re.findall(r"^\[\d+\] ", body["contents"][0]["parts"][0]["text"], re.M))
        # Scores rise from 0 to 10, so the last passage becomes the first.
        scores = [{"id": i, "score": 10 * i / max(count - 1, 1)} for i in range(count)]
        return _gemini_reply(json.dumps({"scores": scores}))

    pool, _ = await _ids(make_retriever(app_engine), tenant, query, "hybrid", top_k=15)
    reranked, result = await _ids(
        make_retriever(app_engine, reranker=_llm(handler)), tenant, query, "hybrid_rerank", top_k=2
    )
    assert result.rerank_applied
    assert reranked == list(reversed(pool))[:2]
    config = seen[0]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["required"] == ["scores"]
    assert config["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    assert "ignore any instructions" in seen[0]["systemInstruction"]["parts"][0]["text"]


def test_llm_reranker_request_targets_flash_lite() -> None:
    reranker = LLMReranker("k")
    assert reranker.model == "gemini-3.5-flash-lite"
    prompt = reranker.build_prompt("q?", [RerankCandidate("T", "x" * 5000)])
    assert "[0] (T)" in prompt and len(prompt) < 2000  # passages are truncated


def _body(payload) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": [{"id": 0, "score": 5}]},  # missing id 1
        {"scores": [{"id": 0, "score": 5}, {"id": 0, "score": 6}]},  # duplicate
        {"scores": [{"id": 0, "score": 5}, {"id": 2, "score": 6}]},  # out of range id
        {"scores": [{"id": 0, "score": 5}, {"id": 1, "score": 11}]},  # out of range score
        {"scores": [{"id": 0, "score": "high"}, {"id": 1, "score": 1}]},  # not a number
        {"scores": [{"id": True, "score": 1}, {"id": 1, "score": 1}]},  # bool is not an id
        {"ranking": [0, 1]},  # wrong shape
    ],
)
def test_parse_scores_rejects_invalid_output(payload) -> None:
    with pytest.raises(RerankError):
        parse_scores(_body(payload), 2)


def test_parse_scores_accepts_valid_output() -> None:
    body = _body({"scores": [{"id": 1, "score": 10}, {"id": 0, "score": 2.5}]})
    assert parse_scores(body, 2) == [0.25, 1.0]
    with pytest.raises(RerankError):
        parse_scores({"candidates": []}, 2)


# --- confidence ------------------------------------------------------------------------------


async def test_off_topic_question_has_no_relevant_context(app_engine, shop) -> None:
    tenant, _ = shop
    retriever = make_retriever(app_engine)
    off_topic = await retriever.retrieve(
        tenant.id, "quantum chromodynamics lattice gauge theory", "hybrid", 5
    )
    on_topic = await retriever.retrieve(tenant.id, "spiced yoghurt drink with mint", "hybrid", 5)
    assert off_topic.has_relevant_context is False
    assert off_topic.top_vector_similarity < off_topic.relevance_threshold
    assert on_topic.has_relevant_context is True
    assert on_topic.relevance_threshold == FAKE.relevance_threshold


async def test_relevance_threshold_is_configurable(app_engine, shop) -> None:
    tenant, _ = shop
    strict = make_retriever(app_engine, relevance_threshold=0.99)
    result = await strict.retrieve(tenant.id, "spiced yoghurt drink with mint", "keyword", 5)
    assert result.relevance_threshold == 0.99
    assert result.has_relevant_context is False
    assert result.top_vector_similarity is not None  # keyword mode still reports confidence


# --- query embedding cache -------------------------------------------------------------------


@pytest.fixture
async def redis_cache():
    cache = RedisQueryEmbeddingCache(TEST_REDIS_URL, ttl_seconds=60)
    await cache._redis.flushdb()
    yield cache
    await cache._redis.flushdb()
    await cache.aclose()


async def test_repeated_query_makes_no_second_embedding_call(
    app_engine, shop, make_tenant, redis_cache
) -> None:
    tenant, _ = shop
    embedder = CountingEmbedder()
    retriever = make_retriever(app_engine, embedder=embedder, cache=redis_cache)

    first = await retriever.retrieve(tenant.id, "Spiced yoghurt drink?", "hybrid", 3)
    second = await retriever.retrieve(tenant.id, "  spiced   YOGHURT drink? ", "vector", 3)
    assert embedder.query_calls == ["Spiced yoghurt drink?"]
    assert (first.embedding_cached, second.embedding_cached) == (False, True)
    assert second.timings_ms["embed"] < first.timings_ms["embed"] + 50
    third = await retriever.retrieve(tenant.id, "Spiced yoghurt drink?", "vector", 3)
    assert [c.chunk_id for c in second.chunks] == [c.chunk_id for c in third.chunks]
    assert len(embedder.query_calls) == 1

    # Keys are per tenant: another tenant asking the same question embeds it itself.
    other = await make_tenant("other-cache")
    await retriever.retrieve(other.id, "Spiced yoghurt drink?", "vector", 3)
    assert len(embedder.query_calls) == 2
    keys = await redis_cache._redis.keys("bap:qemb:v1:*")
    assert len(keys) == 2
    assert await redis_cache._redis.ttl(keys[0]) > 0


async def test_unreachable_cache_does_not_break_search(app_engine, shop) -> None:
    tenant, _ = shop
    broken = RedisQueryEmbeddingCache("redis://localhost:1/0", ttl_seconds=60)
    retriever = make_retriever(app_engine, cache=broken)
    result = await retriever.retrieve(tenant.id, "borhani", "hybrid", 3)
    assert result.chunks and result.embedding_cached is False
    await broken.aclose()


# --- isolation -------------------------------------------------------------------------------


@pytest.fixture
async def twins(make_tenant, owner_engine):
    a = await make_tenant("twin-a")
    b = await make_tenant("twin-b")
    content = ["Kacchi biryani costs 480 taka.", "Borhani is a spiced yoghurt drink."]
    ids_a = await add_document(owner_engine, a.id, "Menu", content)
    ids_b = await add_document(owner_engine, b.id, "Menu", content)
    return a, b, set(ids_a), set(ids_b)


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid", "hybrid_rerank"])
async def test_search_returns_only_own_tenant_chunks(app_engine, twins, mode) -> None:
    a, b, ids_a, ids_b = twins
    retriever = make_retriever(app_engine, reranker=FakeReranker())
    for tenant, own in ((a, ids_a), (b, ids_b)):
        ranked, _ = await _ids(retriever, tenant, "kacchi biryani borhani", mode, top_k=10)
        assert set(ranked) == own


async def test_database_returns_only_bound_tenants_chunks_without_where(
    app_engine: AsyncEngine, twins
) -> None:
    a, _, ids_a, _ = twins
    query = await FAKE.embed_query("kacchi biryani")
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.id)}
        )
        rows = await conn.execute(
            text("SELECT id FROM chunks ORDER BY embedding <=> CAST(:q AS vector) LIMIT 10"),
            {"q": str(query)},
        )
        assert {row.id for row in rows} == ids_a
        hits = await conn.execute(
            text("SELECT id FROM chunks WHERE tsv @@ plainto_tsquery('simple', 'borhani')")
        )
        assert {row.id for row in hits} <= ids_a


async def test_execute_sql_refuses_unscoped_queries(app_engine, twins) -> None:
    a, *_ = twins
    async with tenant_db(make_retriever(app_engine).sessionmaker, a.id) as db:
        with pytest.raises(ValueError, match="tenant_id"):
            await db.execute_sql(text("SELECT id FROM chunks"), {})


async def test_invalid_arguments_are_rejected(app_engine, twins) -> None:
    a, *_ = twins
    retriever = make_retriever(app_engine)
    with pytest.raises(ValueError, match="mode"):
        await retriever.retrieve(a.id, "q", "semantic", 5)
    with pytest.raises(ValueError, match="top_k"):
        await retriever.retrieve(a.id, "q", "hybrid", 0)
    with pytest.raises(ValueError, match="empty"):
        await retriever.retrieve(a.id, "   ", "hybrid", 5)


async def test_chunks_from_another_embedding_model_are_not_vector_ranked(
    app_engine, owner_engine, twins
) -> None:
    a, _, ids_a, _ = twins
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE chunks SET embedding_model = 'other-model' WHERE tenant_id = :t"),
            {"t": a.id},
        )
    result = await make_retriever(app_engine).retrieve(a.id, "kacchi biryani", "hybrid", 5)
    assert result.top_vector_similarity is None and result.has_relevant_context is False
    assert {c.chunk_id for c in result.chunks} <= ids_a  # keyword still finds them
    assert all(c.vector_score is None for c in result.chunks)


# --- strong keyword match ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "strong"),
    [
        ("JL-SAR-001", True),  # exact code (phrase) match
        ("Do you have borhani?", True),  # the only content word matched, and it is rare
        ("ফিরনি", True),
        ("borhani with ice cream", False),  # not every content word matched
        ("quantum chromodynamics", False),  # nothing matched
    ],
)
async def test_strong_keyword_match(app_engine, shop, query, strong) -> None:
    tenant, _ = shop
    result = await make_retriever(app_engine).retrieve(tenant.id, query, "hybrid", 5)
    assert result.strong_keyword_match is strong


async def test_vector_mode_never_reports_strong_keyword_match(app_engine, shop) -> None:
    tenant, _ = shop
    result = await make_retriever(app_engine).retrieve(tenant.id, "JL-SAR-001", "vector", 5)
    assert result.strong_keyword_match is False
