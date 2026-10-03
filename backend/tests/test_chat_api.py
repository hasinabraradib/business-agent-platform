"""POST /v1/chat end to end with the fake chat model, plus the conversation read endpoints."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.chat.deps import get_chat_service, get_rate_limiter
from app.chat.ratelimit import RateLimiter
from app.chat.service import ChatConfig, ChatService
from app.chat.tools import SearchKnowledgeTool, ToolRegistry
from app.config import get_settings
from app.llm import Candidate, ChatChain, FakeChatProvider, Scripted
from app.retrieval.service import Retriever
from tests.chat_helpers import (
    FALLBACK,
    ORIGIN,
    add_cafe_corpus,
    chat,
    configure_tenant,
    parse_sse,
    searched,
    smart_responder,
)
from tests.conftest import TEST_REDIS_URL, bearer
from tests.retrieval_helpers import make_retriever


class SpyRetriever(Retriever):
    def __init__(self, base: Retriever) -> None:
        super().__init__(base.sessionmaker, base.embedder, base.cache, base.reranker, base.config)
        self.queries: list[str] = []

    async def retrieve(self, tenant_id, query, mode="hybrid", top_k=5):
        self.queries.append(query)
        return await super().retrieve(tenant_id, query, mode, top_k)


@pytest.fixture
def provider() -> FakeChatProvider:
    return FakeChatProvider(smart_responder)


@pytest.fixture
def retriever(app_engine) -> SpyRetriever:
    return SpyRetriever(make_retriever(app_engine))


NOW = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)  # Saturday 7:30 PM in Dhaka


def make_service(app_engine, chain: ChatChain, retriever: Retriever, **config) -> ChatService:
    return ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        chain,
        ToolRegistry([SearchKnowledgeTool(retriever)]),
        ChatConfig(**{"idle_timeout_seconds": 0.5, "first_token_timeout_seconds": 0.5, **config}),
        clock=lambda: NOW,
    )


@pytest.fixture
def service(app, app_engine, provider, retriever) -> ChatService:
    instance = make_service(app_engine, ChatChain([Candidate(provider, "fake-chat")]), retriever)
    app.dependency_overrides[get_chat_service] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_chat_service, None)


def use(app, instance: ChatService) -> None:
    app.dependency_overrides[get_chat_service] = lambda: instance


@pytest.fixture(autouse=True)
async def limiter(app):
    instance = RateLimiter(TEST_REDIS_URL)
    await instance._redis.flushdb()
    app.dependency_overrides[get_rate_limiter] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_rate_limiter, None)
    await instance._redis.flushdb()
    await instance.aclose()


@pytest.fixture
async def cafe(make_tenant, owner_engine, service):
    tenant = await make_tenant("cafe")
    await configure_tenant(owner_engine, tenant.id)
    chunk_ids = await add_cafe_corpus(owner_engine, tenant.id)
    return tenant, {str(i) for i in chunk_ids}


# --- streaming and response shape --------------------------------------------------------------


async def test_stream_events_arrive_in_order(client: AsyncClient, cafe, owner_engine) -> None:
    tenant, chunk_ids = cafe
    response = await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?", stream=True)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[-2:] == ["citations", "done"]
    assert set(names[:-2]) == {"token"} and len(names) > 4

    streamed = "".join(data["text"] for name, data in events if name == "token")
    assert streamed == "dish: Kacchi Biryani [1]"
    citations = events[-2][1]["citations"]
    assert [c["marker"] for c in citations] == [1]
    assert citations[0]["chunk_id"] in chunk_ids
    assert citations[0]["document_title"] == "Menu"
    assert citations[0]["snippet"].startswith("dish: Kacchi Biryani")
    done = events[-1][1]
    assert done["outcome"] == "answered"
    assert done["usage"]["prompt_tokens"] > 0 and done["usage"]["completion_tokens"] > 0
    async with owner_engine.connect() as conn:
        stored = await conn.scalar(
            text("SELECT content FROM messages WHERE id = :id"), {"id": done["message_id"]}
        )
    assert stored == streamed


async def test_non_streaming_response_shape(client: AsyncClient, cafe) -> None:
    tenant, _ = cafe
    response = await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "conversation_id", "message_id", "reply", "outcome", "citations", "usage", "timings",
        "retrieval", "model", "replayed", "error",
    }  # fmt: skip
    assert body["reply"] == "dish: Kacchi Biryani [1]"
    assert body["outcome"] == "answered"
    assert body["error"] is None
    assert body["model"] == "fake:fake-chat" and body["replayed"] is False
    assert body["retrieval"]["searches"][0]["query"] == "How much is the Kacchi Biryani?"


async def test_conversation_and_messages_are_persisted(client, cafe, owner_engine) -> None:
    tenant, chunk_ids = cafe
    body = (await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?")).json()
    async with owner_engine.connect() as conn:
        conversation = (
            await conn.execute(
                text("SELECT * FROM conversations WHERE id = :id"), {"id": body["conversation_id"]}
            )
        ).one()
        messages = (
            await conn.execute(
                text("SELECT * FROM messages WHERE conversation_id = :c ORDER BY created_at"),
                {"c": body["conversation_id"]},
            )
        ).all()
    assert (conversation.tenant_id, conversation.channel, conversation.visitor_id) == (
        tenant.id,
        "web",
        "visitor-1",
    )
    assert conversation.status == "ai"
    assert [(m.role, m.outcome) for m in messages] == [("user", None), ("assistant", "answered")]
    assistant = messages[1]
    assert assistant.model == "fake:fake-chat"
    assert assistant.in_reply_to == messages[0].id
    assert assistant.prompt_tokens > 0 and assistant.completion_tokens > 0
    assert {"first_token", "model_1", "model_2", "total"} <= set(assistant.timings)
    [search] = assistant.retrieval["searches"]
    assert search["query"] == "How much is the Kacchi Biryani?"
    assert set(search["chunk_ids"]) <= chunk_ids and search["chunk_ids"]
    assert search["top_similarity"] > 0 and search["relevant"] is True
    assert assistant.citations[0]["marker"] == 1
    assert assistant.error is None


# --- when to search ----------------------------------------------------------------------------


def search_count(retriever) -> int:
    return len(retriever.queries)


async def test_greetings_thanks_and_follow_ups_make_no_search(client, cafe, retriever) -> None:
    tenant, _ = cafe
    hi = (await chat(client, tenant.admin_key, "Hi")).json()
    assert search_count(retriever) == 0 and hi["outcome"] == "smalltalk"

    question = (
        await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?",
                   conversation_id=hi["conversation_id"])
    ).json()  # fmt: skip
    assert search_count(retriever) == 1 and question["outcome"] == "answered"

    follow_up = (
        await chat(client, tenant.admin_key, "and is it spicy?",
                   conversation_id=hi["conversation_id"])
    ).json()  # fmt: skip
    assert search_count(retriever) == 1  # answered from the earlier source, no new search
    assert follow_up["outcome"] == "answered"
    assert follow_up["reply"] == "It's medium spicy [1]."
    assert follow_up["citations"][0]["snippet"].startswith("dish: Kacchi Biryani")
    assert follow_up["retrieval"]["searches"] == []
    assert follow_up["retrieval"]["earlier_sources"]

    thanks = (
        await chat(client, tenant.admin_key, "thanks bhai", conversation_id=hi["conversation_id"])
    ).json()
    assert search_count(retriever) == 1 and thanks["outcome"] == "smalltalk"


async def test_the_model_writes_its_own_search_query(client, cafe, provider, retriever) -> None:
    tenant, _ = cafe

    def responder(request, model):
        if searched(request):
            return smart_responder(request, model)
        return Scripted(tool_calls=[("search_knowledge", {"query": "Kacchi Biryani price"})])

    provider.responder = responder
    body = (await chat(client, tenant.admin_key, "kacchi koto?")).json()
    assert retriever.queries == ["Kacchi Biryani price"]
    assert body["retrieval"]["searches"][0]["query"] == "Kacchi Biryani price"
    assert body["outcome"] == "answered"


async def test_at_most_two_searches_per_message(client, cafe, provider, retriever) -> None:
    tenant, _ = cafe

    def responder(request, model):
        if request.tools:  # keeps searching while it is allowed to
            return Scripted(
                tool_calls=[("search_knowledge", {"query": f"q{len(request.messages)}"})]
            )
        return "[[answered]]\nBorhani is BDT 90 [2]."

    provider.responder = responder
    body = (await chat(client, tenant.admin_key, "tell me everything")).json()
    assert len(retriever.queries) == 2
    assert len(body["retrieval"]["searches"]) == 2
    requests = provider.requests()
    assert [bool(r.tools) for r in requests] == [True, True, False]  # no tool after the cap
    assert body["outcome"] in ("answered", "no_answer")


async def test_a_second_tool_call_in_one_step_beyond_the_cap_is_refused(
    client, cafe, provider, retriever
) -> None:
    tenant, _ = cafe

    def responder(request, model):
        if not searched(request):
            return Scripted(
                tool_calls=[("search_knowledge", {"query": q}) for q in ("a", "b", "c")]
            )
        return "[[no_answer]]\nSorry."

    provider.responder = responder
    await chat(client, tenant.admin_key, "three at once")
    assert len(retriever.queries) == 2
    tool_texts = [m.text for m in provider.requests()[-1].messages if m.role == "tool"]
    assert "Search limit reached" in tool_texts[-1]


async def test_failed_search_is_no_answer_with_the_contact(client, cafe) -> None:
    tenant, _ = cafe
    body = (await chat(client, tenant.admin_key, "quantum chromodynamics lattice")).json()
    assert body["outcome"] == "no_answer"
    assert FALLBACK in body["reply"]
    assert body["retrieval"]["searches"][0]["relevant"] is False


async def test_contact_is_not_appended_twice_when_the_model_spaced_it_oddly(
    client, cafe, provider
) -> None:
    # gpt-oss writes narrow no-break spaces and non-breaking hyphens ("01700\u2011000000");
    # an exact substring check missed them and appended the contact a second time.
    tenant, _ = cafe
    odd = FALLBACK.replace(" ", "\u202f").replace("-", "\u2011")
    assert odd != FALLBACK
    provider.responder = lambda request, model: f"[[no_answer]]\nSorry, we can't do that. {odd}"
    body = (await chat(client, tenant.admin_key, "Can you deliver to the moon?")).json()
    assert body["outcome"] == "no_answer"
    assert "Contact:" not in body["reply"]


async def test_business_facts_without_a_citation_are_not_answered(client, cafe, provider) -> None:
    tenant, _ = cafe

    def searched_but_uncited(request, model):
        if searched(request):
            return "[[answered]]\nThe Kacchi Biryani costs 480 taka."
        return Scripted(tool_calls=[("search_knowledge", {"query": "kacchi"})])

    provider.responder = searched_but_uncited
    body = (await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?")).json()
    assert body["outcome"] == "no_answer"
    assert body["retrieval"]["checks"]["model_tag"] == "answered"

    provider.responder = lambda request, model: "[[answered]]\nWe close at 11 pm tonight."
    unsearched = (await chat(client, tenant.admin_key, "When do you close?")).json()
    assert unsearched["outcome"] == "no_answer"  # a claim with nothing to verify it


async def test_strong_keyword_match_counts_as_a_relevant_search(
    client, app, app_engine, cafe, provider
) -> None:
    tenant, _ = cafe
    strict = make_service(
        app_engine,
        ChatChain([Candidate(provider, "fake-chat")]),
        SpyRetriever(make_retriever(app_engine, relevance_threshold=0.999)),
    )
    use(app, strict)
    body = (await chat(client, tenant.admin_key, "JL-SAR-001")).json()
    search = body["retrieval"]["searches"][0]
    assert search["strong_keyword_match"] is True and search["relevant"] is True
    assert body["outcome"] == "answered" and body["reply"] == "sku: JL-SAR-001 [1]"


async def test_the_offline_model_works_through_the_real_prompt(
    client, app, app_engine, cafe, retriever
) -> None:
    """The default fake model (offline demo) must see the customer's words, not the tags."""
    tenant, _ = cafe
    offline = FakeChatProvider()  # the default offline responder
    use(app, make_service(app_engine, ChatChain([Candidate(offline, "fake-chat")]), retriever))
    greeting = (await chat(client, tenant.admin_key, "Hi")).json()
    assert greeting["outcome"] == "smalltalk" and retriever.queries == []
    answer = (await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?")).json()
    assert retriever.queries == ["How much is the Kacchi Biryani?"]
    assert answer["outcome"] == "answered"
    assert answer["reply"] == "From what we have: dish: Kacchi Biryani [1]"


# --- time awareness ----------------------------------------------------------------------------


async def test_current_local_time_in_the_tenants_timezone_is_in_the_prompt(
    client, cafe, provider, owner_engine
) -> None:
    tenant, _ = cafe
    await chat(client, tenant.admin_key, "Hi")
    assert "Saturday, 3 October 2026, 1:30 PM (UTC)" in provider.requests()[-1].system
    await configure_tenant(owner_engine, tenant.id, timezone="Asia/Dhaka")
    await chat(client, tenant.admin_key, "Hi", visitor_id="v-2")
    assert "Saturday, 3 October 2026, 7:30 PM (Asia/Dhaka)" in provider.requests()[-1].system


# --- latency: fast failover --------------------------------------------------------------------


async def test_slow_primary_fails_over_fast_and_the_answering_model_is_stored(
    client, app, app_engine, cafe, retriever
) -> None:
    tenant, _ = cafe
    slow = FakeChatProvider(lambda r, m: Scripted("late", first_delay=2), name="primary")
    fallback = FakeChatProvider(smart_responder, name="fallback")
    chain = ChatChain([Candidate(slow, "m1"), Candidate(fallback, "m2")], first_event_timeout=0.1)
    use(app, make_service(app_engine, chain, retriever, first_token_timeout_seconds=5))
    response = await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?", stream=True)
    events = parse_sse(response.text)
    done = events[-1][1]
    assert events[-1][0] == "done" and done["outcome"] == "answered"
    assert done["timings"]["first_token"] < 1500
    detail = (
        await client.get(
            f"/v1/conversations/{done['conversation_id']}", headers=bearer(tenant.admin_key)
        )
    ).json()
    assert detail["messages"][-1]["model"] == "fallback:m2"
    # Within the cool-down the slow primary is not tried again.
    calls_before = len(slow.calls)
    await chat(client, tenant.admin_key, "Hi", conversation_id=done["conversation_id"])
    assert len(slow.calls) == calls_before


async def test_unavailable_primary_fails_over_immediately(
    client, app, app_engine, cafe, retriever
) -> None:
    tenant, _ = cafe
    down = FakeChatProvider(lambda r, m: Scripted(unavailable=True), name="primary")
    up = FakeChatProvider(smart_responder, name="fallback")
    use(app, make_service(app_engine, ChatChain([Candidate(down, "m1"), Candidate(up, "m2")]),
                          retriever))  # fmt: skip
    body = (await chat(client, tenant.admin_key, "Hi")).json()
    assert body["model"] == "fallback:m2" and body["outcome"] == "smalltalk"


async def test_when_every_model_fails_the_last_one_tried_is_stored(
    client, app, app_engine, cafe, retriever
) -> None:
    tenant, _ = cafe
    a = FakeChatProvider(lambda r, m: Scripted(unavailable=True), name="a")
    b = FakeChatProvider(lambda r, m: Scripted("x" * 10, fail_after_chunks=0), name="b")
    use(app, make_service(app_engine, ChatChain([Candidate(a, "m1"), Candidate(b, "m2")]),
                          retriever))  # fmt: skip
    body = (await chat(client, tenant.admin_key, "Hi")).json()
    detail = (
        await client.get(
            f"/v1/conversations/{body['conversation_id']}", headers=bearer(tenant.admin_key)
        )
    ).json()
    assert detail["messages"][-1]["model"] == "b:m2"
    assert detail["messages"][-1]["outcome"] == "error"


# --- idempotent retries ------------------------------------------------------------------------


async def count_user_messages(owner_engine, client_id: str) -> int:
    async with owner_engine.connect() as conn:
        return await conn.scalar(
            text("SELECT count(*) FROM messages WHERE client_message_id = :c"), {"c": client_id}
        )


async def test_retry_with_the_same_client_message_id_stores_one_user_message(
    client, cafe, provider, owner_engine
) -> None:
    tenant, _ = cafe
    client_id = "cmsg-0001-abcdef"
    provider.responder = lambda r, m: Scripted("x", fail_after_chunks=0)
    failed = await chat(client, tenant.admin_key, "Kacchi price?", client_message_id=client_id)
    assert failed.status_code == 502
    conversation_id = failed.json()["conversation_id"]

    provider.responder = smart_responder
    retried = (
        await chat(client, tenant.admin_key, "Kacchi price?", client_message_id=client_id,
                   conversation_id=conversation_id)
    ).json()  # fmt: skip
    assert retried["outcome"] == "answered" and retried["replayed"] is False
    assert await count_user_messages(owner_engine, client_id) == 1

    again = (
        await chat(client, tenant.admin_key, "Kacchi price?", client_message_id=client_id,
                   conversation_id=conversation_id, stream=True)
    )  # fmt: skip
    events = parse_sse(again.text)
    assert [name for name, _ in events] == ["token", "citations", "done"]
    assert events[-1][1]["message_id"] == retried["message_id"] and events[-1][1]["replayed"]
    assert events[0][1]["text"] == retried["reply"]
    assert await count_user_messages(owner_engine, client_id) == 1
    detail = (
        await client.get(f"/v1/conversations/{conversation_id}", headers=bearer(tenant.admin_key))
    ).json()
    roles = [(m["role"], m["outcome"]) for m in detail["messages"]]
    assert roles == [("user", None), ("assistant", "error"), ("assistant", "answered")]
    user_id = detail["messages"][0]["id"]
    assert all(m["in_reply_to"] == user_id for m in detail["messages"][1:])


async def test_retry_of_a_first_message_reuses_its_conversation(client, cafe, provider) -> None:
    tenant, _ = cafe
    client_id = "cmsg-first-000001"
    provider.responder = lambda r, m: Scripted("x", fail_after_chunks=0)
    first = await chat(client, tenant.admin_key, "Hi", client_message_id=client_id)
    provider.responder = smart_responder
    retry = (await chat(client, tenant.admin_key, "Hi", client_message_id=client_id)).json()
    assert retry["conversation_id"] == first.json()["conversation_id"]


async def test_another_visitor_cannot_reuse_a_client_message_id(client, cafe) -> None:
    tenant, _ = cafe
    client_id = "cmsg-visitor-0001"
    await chat(client, tenant.admin_key, "Hi", client_message_id=client_id)
    response = await chat(
        client, tenant.admin_key, "Hi", client_message_id=client_id, visitor_id="someone-else"
    )
    assert response.status_code == 404


async def test_stored_errors_are_bounded_not_truncated_early(
    client, app, app_engine, cafe, retriever
) -> None:
    tenant, _ = cafe
    from app.llm import ChatError

    class LongError(FakeChatProvider):
        async def stream(self, request, *, model):
            raise ChatError("x" * 5000, model="b:model-b")
            yield  # pragma: no cover

    use(app, make_service(app_engine, ChatChain([Candidate(LongError(), "m")]), retriever))
    body = (await chat(client, tenant.admin_key, "Hi")).json()
    detail = (
        await client.get(
            f"/v1/conversations/{body['conversation_id']}", headers=bearer(tenant.admin_key)
        )
    ).json()
    stored = detail["messages"][-1]
    assert stored["model"] == "b:model-b"
    assert 1500 < len(stored["error"]) <= 2000


# --- citations and outcomes --------------------------------------------------------------------


async def test_invalid_citation_markers_are_dropped(client, cafe, provider) -> None:
    tenant, _ = cafe
    provider.chunk_size = 1  # stream character by character: markers arrive split
    provider.responder = lambda request, model: (
        "[[answered]]\nKacchi costs 480 taka [1]. Spice is medium [99] and [1, 42]."
        if searched(request)
        else Scripted(tool_calls=[("search_knowledge", {"query": "kacchi price"})])
    )
    response = await chat(client, tenant.admin_key, "Kacchi price?", stream=True)
    events = parse_sse(response.text)
    streamed = "".join(d["text"] for n, d in events if n == "token")
    assert streamed == "Kacchi costs 480 taka [1]. Spice is medium and [1]."
    assert [c["marker"] for c in events[-2][1]["citations"]] == [1]
    detail = (
        await client.get(
            f"/v1/conversations/{events[-1][1]['conversation_id']}",
            headers=bearer(tenant.admin_key),
        )
    ).json()
    assistant = detail["messages"][-1]
    assert assistant["content"] == streamed
    assert assistant["retrieval"]["checks"]["dropped_markers"] == ["[99]", "[1, 42]"]


async def test_outcomes_answered_no_answer_and_smalltalk(client, cafe, owner_engine) -> None:
    tenant, _ = cafe
    answered = (await chat(client, tenant.admin_key, "How much is the Kacchi Biryani?")).json()
    off_topic = (await chat(client, tenant.admin_key, "quantum chromodynamics lattice")).json()
    greeting = (await chat(client, tenant.admin_key, "Hi")).json()

    assert (answered["outcome"], off_topic["outcome"], greeting["outcome"]) == (
        "answered",
        "no_answer",
        "smalltalk",
    )
    assert off_topic["retrieval"]["searches"][0]["relevant"] is False
    assert off_topic["retrieval"]["searches"][0]["markers"] == []  # nothing the model could cite
    assert FALLBACK in off_topic["reply"]  # the contact is always offered on no_answer
    assert greeting["reply"] == "Hello! How can I help you today?"
    assert greeting["citations"] == []
    assert greeting["retrieval"]["searches"] == []
    async with owner_engine.connect() as conn:
        stored = (
            await conn.scalars(
                text("SELECT outcome FROM messages WHERE role = 'assistant' ORDER BY created_at")
            )
        ).all()
    assert stored == ["answered", "no_answer", "smalltalk"]


# --- public-endpoint protections ---------------------------------------------------------------


async def test_widget_key_works_on_chat_from_an_allowed_origin(client, cafe) -> None:
    tenant, _ = cafe
    response = await chat(client, tenant.widget_key, "Hi", origin=ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["vary"] == "Origin"
    # ...but is still refused on admin routes.
    for path in ("/v1/conversations", "/v1/documents", "/v1/tenant"):
        refused = await client.get(path, headers={**bearer(tenant.widget_key), "Origin": ORIGIN})
        assert refused.status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", None, "https://cafe.example.evil.com"])
async def test_widget_key_from_other_origins_is_refused(client, cafe, origin) -> None:
    tenant, _ = cafe
    response = await chat(client, tenant.widget_key, "Hi", origin=origin)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


async def test_admin_key_needs_no_origin(client, cafe) -> None:
    tenant, _ = cafe
    assert (await chat(client, tenant.admin_key, "Hi")).status_code == 200


async def test_preflight_is_answered(client, cafe) -> None:
    response = await client.options(
        "/v1/chat",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 204
    assert "authorization" in response.headers["access-control-allow-headers"]


async def test_per_visitor_rate_limit(client, cafe, monkeypatch) -> None:
    tenant, _ = cafe
    monkeypatch.setattr(get_settings(), "chat_rate_per_visitor_per_minute", 2)
    for _ in range(2):
        assert (await chat(client, tenant.widget_key, "Hi", origin=ORIGIN)).status_code == 200
    limited = await chat(client, tenant.widget_key, "Hi", origin=ORIGIN)
    assert limited.status_code == 429
    assert "too quickly" in limited.json()["detail"]
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    assert limited.headers["access-control-allow-origin"] == ORIGIN  # the widget can read it
    other_visitor = await chat(client, tenant.widget_key, "Hi", origin=ORIGIN, visitor_id="v-2")
    assert other_visitor.status_code == 200


async def test_per_key_rate_limit(client, cafe, monkeypatch) -> None:
    tenant, _ = cafe
    monkeypatch.setattr(get_settings(), "chat_rate_per_key_per_minute", 1)
    assert (await chat(client, tenant.widget_key, "Hi", origin=ORIGIN)).status_code == 200
    limited = await chat(client, tenant.widget_key, "Hi", origin=ORIGIN, visitor_id="v-9")
    assert limited.status_code == 429
    assert "from this website" in limited.json()["detail"]


async def test_daily_message_cap(client, cafe, owner_engine) -> None:
    tenant, _ = cafe
    await configure_tenant(owner_engine, tenant.id, daily_message_cap=2)
    for visitor in ("a", "b"):
        assert (await chat(client, tenant.admin_key, "Hi", visitor_id=visitor)).status_code == 200
    capped = await chat(client, tenant.admin_key, "Hi", visitor_id="c")
    assert capped.status_code == 429
    assert "today's message limit" in capped.json()["detail"]
    assert int(capped.headers["retry-after"]) <= 24 * 3600


@pytest.mark.parametrize(
    "body",
    [
        {"message": "x" * 2001},
        {"message": ""},
        {"message": "hi", "visitor_id": ""},
        {"message": "hi", "visitor_id": "has spaces"},
    ],
)
async def test_invalid_chat_requests_are_rejected(client, cafe, body) -> None:
    tenant, _ = cafe
    response = await client.post(
        "/v1/chat",
        json={"visitor_id": "v", "stream": False, **body},
        headers=bearer(tenant.admin_key),
    )
    assert response.status_code == 422


async def test_chat_requires_a_key(client, cafe) -> None:
    response = await client.post("/v1/chat", json={"visitor_id": "v", "message": "hi"})
    assert response.status_code == 401


# --- failures ----------------------------------------------------------------------------------


async def test_mid_stream_model_failure_sends_error_event_and_stores_error(
    client, cafe, provider, owner_engine
) -> None:
    tenant, _ = cafe
    provider.responder = lambda request, model: Scripted(
        "[[answered]]\nKacchi costs 480 taka [1].", fail_after_chunks=3
    )
    response = await chat(client, tenant.widget_key, "Kacchi price?", origin=ORIGIN, stream=True)
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[-1] == "error" and "done" not in names and "citations" not in names
    assert "token" in names  # it failed after streaming had started
    error = events[-1][1]
    assert FALLBACK in error["message"] and "Sorry" in error["message"]
    async with owner_engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT content, outcome, error FROM messages WHERE id = :id"),
                {"id": error["message_id"]},
            )
        ).one()
    assert stored.outcome == "error"
    assert stored.content == error["message"]
    detail = (
        await client.get(
            f"/v1/conversations/{error['conversation_id']}", headers=bearer(tenant.admin_key)
        )
    ).json()
    assert detail["messages"][-1]["model"] == "fake:fake-chat"  # the model that failed
    assert "model error: fake model failure" in stored.error
    assert "characters had been streamed" in stored.error


async def test_model_timeout_sends_error_event(client, cafe, provider) -> None:
    tenant, _ = cafe
    provider.responder = lambda request, model: Scripted("slow answer", delay=2)
    events = parse_sse((await chat(client, tenant.admin_key, "Hi", stream=True)).text)
    assert [name for name, _ in events] == ["error"]
    detail = (
        await client.get(
            f"/v1/conversations/{events[0][1]['conversation_id']}", headers=bearer(tenant.admin_key)
        )
    ).json()
    assert detail["messages"][-1]["error"] == "model timed out"


async def test_non_streaming_failure_returns_502_with_apology(client, cafe, provider) -> None:
    tenant, _ = cafe
    provider.responder = lambda request, model: Scripted("x", fail_after_chunks=0)
    response = await chat(client, tenant.admin_key, "দাম কত?")
    assert response.status_code == 502
    body = response.json()
    assert body["outcome"] == "error" and FALLBACK in body["reply"]
    assert "দুঃখিত" in body["reply"]  # Bengali-script customers get a Bengali apology


async def test_failed_turns_are_left_out_of_later_history(client, cafe, provider) -> None:
    tenant, _ = cafe
    provider.responder = lambda request, model: Scripted("x", fail_after_chunks=0)
    failed = (await chat(client, tenant.admin_key, "first question")).json()
    provider.responder = smart_responder
    await chat(client, tenant.admin_key, "Hi", conversation_id=failed["conversation_id"])
    prompt = "\n".join(m.text for m in provider.requests()[-1].messages)
    assert "first question" in prompt
    assert "Sorry, I'm having trouble" not in prompt


# --- conversations and isolation ---------------------------------------------------------------


async def test_conversation_endpoints_list_newest_first_with_messages(client, cafe) -> None:
    tenant, _ = cafe
    first = (await chat(client, tenant.admin_key, "Hi", visitor_id="older")).json()
    second = (await chat(client, tenant.admin_key, "Kacchi price?", visitor_id="newer")).json()
    await chat(
        client,
        tenant.admin_key,
        "Borhani?",
        visitor_id="newer",
        conversation_id=second["conversation_id"],
    )

    listed = (await client.get("/v1/conversations", headers=bearer(tenant.admin_key))).json()
    assert [c["id"] for c in listed] == [second["conversation_id"], first["conversation_id"]]
    assert [c["message_count"] for c in listed] == [4, 2]

    detail = (
        await client.get(
            f"/v1/conversations/{second['conversation_id']}", headers=bearer(tenant.admin_key)
        )
    ).json()
    assert [(m["role"], m["outcome"]) for m in detail["messages"]] == [
        ("user", None),
        ("assistant", "answered"),
        ("user", None),
        ("assistant", "answered"),
    ]
    assert detail["messages"][1]["citations"][0]["marker"] == 1


async def test_visitor_cannot_continue_another_visitors_conversation(client, cafe) -> None:
    tenant, _ = cafe
    mine = (await chat(client, tenant.widget_key, "Hi", origin=ORIGIN, visitor_id="me")).json()
    response = await chat(
        client,
        tenant.widget_key,
        "Hi",
        origin=ORIGIN,
        visitor_id="someone-else",
        conversation_id=mine["conversation_id"],
    )
    assert response.status_code == 404


@pytest.fixture
async def twins(make_tenant, owner_engine, service):
    a = await make_tenant("chat-a")
    b = await make_tenant("chat-b")
    for tenant in (a, b):
        await configure_tenant(owner_engine, tenant.id)
    ids_a = {str(i) for i in await add_cafe_corpus(owner_engine, a.id)}
    ids_b = {str(i) for i in await add_cafe_corpus(owner_engine, b.id)}
    return a, b, ids_a, ids_b


async def test_tenant_cannot_read_or_continue_another_tenants_conversation(client, twins) -> None:
    a, b, _, _ = twins
    b_reply = (await chat(client, b.admin_key, "Hi", visitor_id="v")).json()
    b_conversation = b_reply["conversation_id"]
    read = await client.get(f"/v1/conversations/{b_conversation}", headers=bearer(a.admin_key))
    assert read.status_code == 404
    listed = (await client.get("/v1/conversations", headers=bearer(a.admin_key))).json()
    assert b_conversation not in {c["id"] for c in listed}
    cont = await chat(client, a.admin_key, "Hi", visitor_id="v", conversation_id=b_conversation)
    assert cont.status_code == 404


async def test_other_tenants_chunks_never_appear_in_answers(client, twins, provider) -> None:
    a, _, ids_a, ids_b = twins
    for question in ("How much is the Kacchi Biryani?", "JL-SAR-001", "Is there parking?"):
        body = (await chat(client, a.admin_key, question)).json()
        cited = {c["chunk_id"] for c in body["citations"]}
        assert cited and cited <= ids_a and not cited & ids_b
        assert set(body["retrieval"]["searches"][0]["chunk_ids"]) <= ids_a
    prompts = " ".join(m.text for r in provider.requests() for m in r.messages)
    assert "dish: Kacchi Biryani" in prompts  # A's own copy of the content


async def test_database_keeps_conversations_per_tenant(
    client, twins, app_engine: AsyncEngine
) -> None:
    a, b, _, _ = twins
    await chat(client, a.admin_key, "Hi")
    await chat(client, b.admin_key, "Hi")
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.id)}
        )
        tenants = (await conn.scalars(text("SELECT DISTINCT tenant_id FROM messages"))).all()
        assert tenants == [a.id]
        assert await conn.scalar(text("SELECT count(*) FROM conversations")) == 1
