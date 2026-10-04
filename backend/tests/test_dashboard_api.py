"""Endpoints added for the dashboard: inbox search and unread markers, analytics, knowledge gaps,
traces, settings, webhook test events, Telegram status. Each with a cross-tenant probe."""

import pytest
from sqlalchemy import text

from app.chat.deps import get_chat_service
from tests.chat_helpers import add_cafe_corpus, configure_tenant, smart_responder
from tests.conftest import bearer


@pytest.fixture
def provider():
    from app.llm import FakeChatProvider

    return FakeChatProvider(smart_responder)


@pytest.fixture
def service(app, app_engine, provider, job_queue):
    from datetime import UTC, datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.chat.service import ChatService
    from app.chat.tools import SearchKnowledgeTool, ToolRegistry
    from app.llm import Candidate, ChatChain
    from tests.retrieval_helpers import make_retriever

    instance = ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        ChatChain([Candidate(provider, "fake-chat")]),
        ToolRegistry([SearchKnowledgeTool(make_retriever(app_engine))]),
        clock=lambda: datetime(2026, 10, 3, 13, 30, tzinfo=UTC),
        queue=job_queue,
    )
    app.dependency_overrides[get_chat_service] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_chat_service, None)


@pytest.fixture
async def cafe(make_tenant, owner_engine, service):
    tenant = await make_tenant("dash-cafe")
    await configure_tenant(owner_engine, tenant.id)
    await add_cafe_corpus(owner_engine, tenant.id)
    return tenant


@pytest.fixture
async def other(make_tenant, owner_engine, service):
    tenant = await make_tenant("dash-other")
    await configure_tenant(owner_engine, tenant.id, business_name="Other Shop")
    return tenant


async def chat(client, tenant, message, visitor="v-1", conversation=None) -> dict:
    body = {"visitor_id": visitor, "message": message, "stream": False}
    if conversation:
        body["conversation_id"] = conversation
    response = await client.post("/v1/chat", json=body, headers=bearer(tenant.admin_key))
    assert response.status_code == 200, response.text
    return response.json()


# --- inbox -------------------------------------------------------------------------------------


async def test_inbox_search_preview_and_unread(client, cafe, other) -> None:
    first = await chat(client, cafe, "How much is the Kacchi Biryani?", visitor="v-kacchi")
    await chat(client, cafe, "hello", visitor="v-hello")
    await chat(client, other, "Kacchi please", visitor="v-elsewhere")

    listed = (await client.get("/v1/conversations", headers=bearer(cafe.admin_key))).json()
    assert len(listed) == 2
    kacchi = next(c for c in listed if c["id"] == first["conversation_id"])
    assert kacchi["unread"] is True
    assert kacchi["last_message_role"] == "assistant"
    assert kacchi["last_message_preview"] == "dish: Kacchi Biryani [1]"

    found = await client.get(
        "/v1/conversations", params={"q": "kacchi"}, headers=bearer(cafe.admin_key)
    )
    assert [c["id"] for c in found.json()] == [first["conversation_id"]]  # not the other shop's

    read = await client.post(
        f"/v1/conversations/{first['conversation_id']}/read", headers=bearer(cafe.admin_key)
    )
    assert read.status_code == 200 and read.json()["staff_read_at"]
    listed = (await client.get("/v1/conversations", headers=bearer(cafe.admin_key))).json()
    assert next(c for c in listed if c["id"] == first["conversation_id"])["unread"] is False

    await chat(client, cafe, "thanks", visitor="v-kacchi", conversation=first["conversation_id"])
    listed = (await client.get("/v1/conversations", headers=bearer(cafe.admin_key))).json()
    assert next(c for c in listed if c["id"] == first["conversation_id"])["unread"] is True


async def test_search_escapes_wildcards(client, cafe) -> None:
    await chat(client, cafe, "hello", visitor="v-a")
    found = await client.get("/v1/conversations", params={"q": "%"}, headers=bearer(cafe.admin_key))
    assert found.json() == []  # "%" is a literal, not "match everything"


async def test_mark_read_is_tenant_scoped(client, cafe, other, owner_engine) -> None:
    mine = await chat(client, cafe, "hello")
    probe = await client.post(
        f"/v1/conversations/{mine['conversation_id']}/read", headers=bearer(other.admin_key)
    )
    assert probe.status_code == 404
    async with owner_engine.connect() as conn:
        read_at = await conn.scalar(
            text("SELECT staff_read_at FROM conversations WHERE id = :c"),
            {"c": mine["conversation_id"]},
        )
    assert read_at is None
    widget = await client.post(
        f"/v1/conversations/{mine['conversation_id']}/read", headers=bearer(cafe.widget_key)
    )
    assert widget.status_code == 403


# --- overview ----------------------------------------------------------------------------------


async def test_overview_counts_only_the_tenants_own_data(client, cafe, other) -> None:
    await chat(client, cafe, "How much is the Kacchi Biryani?", visitor="v-1")
    await chat(client, cafe, "quantum chromodynamics lattice", visitor="v-2")  # a gap
    await chat(client, other, "hello", visitor="v-3")
    body = (await client.get("/v1/analytics/overview", headers=bearer(cafe.admin_key))).json()
    assert body["conversations_today"] == 2 and body["conversations_week"] == 2
    assert body["outcomes"]["answered"] == 1 and body["outcomes"]["no_answer"] == 1
    assert body["handoff_rate"] == 0
    assert body["median_first_token_ms"] is not None
    assert body["tokens"]["prompt"] > 0 and body["estimated_cost_usd"] == 0  # fake model: unpriced
    assert [g["question"] for g in body["recent_gaps"]] == ["quantum chromodynamics lattice"]
    assert sum(d["conversations"] for d in body["daily"]) == 2
    theirs = (await client.get("/v1/analytics/overview", headers=bearer(other.admin_key))).json()
    assert theirs["conversations_week"] == 1 and theirs["recent_gaps"] == []
    widget = await client.get("/v1/analytics/overview", headers=bearer(cafe.widget_key))
    assert widget.status_code == 403


def test_cost_estimate_uses_published_prices() -> None:
    from app.analytics import estimate_cost

    cost = estimate_cost({"openai_compat:openai/gpt-oss-120b": (1_000_000, 100_000),
                          "fake:fake-chat": (5_000, 50)})  # fmt: skip
    assert cost == 0.15 + 0.06


# --- knowledge gaps ----------------------------------------------------------------------------


def test_gap_grouping_by_similarity() -> None:
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.analytics import Gap, group_gaps

    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    conv = uuid.uuid4()
    gaps = [
        Gap("How much is delivery to Gulshan?", now - timedelta(hours=3), uuid.uuid4(), conv),
        Gap("how much does delivery to gulshan cost", now - timedelta(hours=1), uuid.uuid4(), conv),
        Gap("Do you have pizza?", now - timedelta(hours=2), uuid.uuid4(), uuid.uuid4()),
        Gap("পিজ্জা আছে?", now, uuid.uuid4(), uuid.uuid4()),
    ]
    groups = group_gaps(gaps)
    assert [(g.question, g.count) for g in groups] == [
        ("পিজ্জা আছে?", 1),  # newest first; different words, its own group
        ("how much does delivery to gulshan cost", 2),
        ("Do you have pizza?", 1),
    ]
    assert groups[1].examples == ["how much does delivery to gulshan cost",
                                  "How much is delivery to Gulshan?"]  # fmt: skip
    assert groups[1].conversation_ids == [conv]


async def test_answering_a_gap_creates_knowledge_and_closes_it(
    client, cafe, other, job_queue
) -> None:
    await chat(client, cafe, "quantum chromodynamics lattice", visitor="v-1")
    await chat(client, cafe, "quantum chromodynamics lattices?", visitor="v-2")
    await chat(client, other, "quantum chromodynamics lattice", visitor="v-3")
    gaps = (await client.get("/v1/knowledge-gaps", headers=bearer(cafe.admin_key))).json()
    assert len(gaps) == 1 and gaps[0]["count"] == 2
    their_gaps = (await client.get("/v1/knowledge-gaps", headers=bearer(other.admin_key))).json()

    # The other tenant's message ids are ignored: it can only close its own gaps.
    response = await client.post(
        "/v1/knowledge-gaps/answer",
        json={"question": "Do you teach physics?", "answer": "No, we are a restaurant.",
              "message_ids": gaps[0]["message_ids"] + their_gaps[0]["message_ids"]},
        headers=bearer(cafe.admin_key),
    )  # fmt: skip
    assert response.status_code == 201, response.text
    assert response.json()["closed"] == 2
    assert response.json()["document"]["source_type"] == "markdown"
    assert job_queue.jobs[-1][0] == cafe.id
    assert (await client.get("/v1/knowledge-gaps", headers=bearer(cafe.admin_key))).json() == []
    still = (await client.get("/v1/knowledge-gaps", headers=bearer(other.admin_key))).json()
    assert still[0]["count"] == 1  # untouched

    probe = await client.post(
        "/v1/knowledge-gaps/answer",
        json={"question": "x?", "answer": "y", "message_ids": still[0]["message_ids"]},
        headers=bearer(cafe.admin_key),
    )
    assert probe.status_code == 422  # answer too short; and nothing of theirs was touched


# --- traces ------------------------------------------------------------------------------------


async def test_trace_shows_search_scores_tools_and_model(client, cafe, other) -> None:
    reply = await chat(client, cafe, "How much is the Kacchi Biryani?")
    trace = await client.get(
        f"/v1/messages/{reply['message_id']}/trace", headers=bearer(cafe.admin_key)
    )
    assert trace.status_code == 200
    body = trace.json()
    assert body["question"] == "How much is the Kacchi Biryani?"
    assert body["model"] == "fake:fake-chat" and body["fallback"] is False
    search = body["searches"][0]
    assert search["query"] == "How much is the Kacchi Biryani?" and search["mode"] == "hybrid"
    assert search["decision"] == "passed" and search["threshold"] is not None
    assert search["results"][0]["document_title"] == "Menu"
    assert search["results"][0]["vector_score"] is not None
    assert body["tools"][0]["tool"] == "search_knowledge"
    assert body["timings_ms"]["first_token"] >= 0 and body["tokens"]["prompt"] > 0
    probe = await client.get(
        f"/v1/messages/{reply['message_id']}/trace", headers=bearer(other.admin_key)
    )
    assert probe.status_code == 404


async def test_trace_records_a_failover(app, app_engine, client, cafe, job_queue) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.chat.service import ChatService
    from app.chat.tools import ToolRegistry
    from app.llm import Candidate, ChatChain, FakeChatProvider, Scripted

    down = FakeChatProvider(lambda r, m: Scripted(unavailable=True), name="primary")
    up = FakeChatProvider(lambda r, m: "[[smalltalk]]\nHello!", name="fallback")
    service = ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        ChatChain([Candidate(down, "m1"), Candidate(up, "m2")]), ToolRegistry([]),
    )  # fmt: skip
    app.dependency_overrides[get_chat_service] = lambda: service
    reply = await chat(client, cafe, "hi")
    body = (
        await client.get(
            f"/v1/messages/{reply['message_id']}/trace", headers=bearer(cafe.admin_key)
        )
    ).json()
    assert body["fallback"] is True and body["model"] == "fallback:m2"
    assert body["failovers"][0]["model"] == "primary:m1"


async def test_trace_is_only_for_assistant_messages(client, cafe, owner_engine) -> None:
    reply = await chat(client, cafe, "hello")
    async with owner_engine.connect() as conn:
        question = await conn.scalar(
            text("SELECT id FROM messages WHERE conversation_id = :c AND role = 'user'"),
            {"c": reply["conversation_id"]},
        )
    response = await client.get(f"/v1/messages/{question}/trace", headers=bearer(cafe.admin_key))
    assert response.status_code == 404


# --- settings ----------------------------------------------------------------------------------


async def test_settings_update_validates_and_merges(client, cafe, other) -> None:
    response = await client.patch(
        "/v1/tenant/settings",
        json={"follow_up_promise": "within a day", "opening_hours": {"mon": [["10:00", "18:00"]]},
              "allowed_origins": ["https://Shop.Example/"]},
        headers=bearer(cafe.admin_key),
    )  # fmt: skip
    assert response.status_code == 200, response.text
    settings = response.json()["settings"]
    assert settings["follow_up_promise"] == "within a day"
    assert settings["allowed_origins"] == ["https://shop.example"]
    assert settings["business_name"] == "Cafe Test"  # untouched fields are kept
    bad = await client.patch(
        "/v1/tenant/settings",
        json={"opening_hours": {"funday": [["25:00", "18:00"]]}, "accent_color": "red"},
        headers=bearer(cafe.admin_key),
    )
    assert bad.status_code == 422
    fields = {p["field"] for p in bad.json()["detail"]}
    assert {"opening_hours", "accent_color"} <= fields
    unknown = await client.patch(
        "/v1/tenant/settings", json={"is_admin": True}, headers=bearer(cafe.admin_key)
    )
    assert unknown.status_code == 422 and "is_admin" in unknown.text
    theirs = (await client.get("/v1/tenant", headers=bearer(other.admin_key))).json()
    assert "follow_up_promise" not in theirs["settings"]
    widget = await client.patch(
        "/v1/tenant/settings", json={"tone": "x"}, headers=bearer(cafe.widget_key)
    )
    assert widget.status_code == 403


# --- webhook test events -----------------------------------------------------------------------


async def test_webhook_test_event(client, cafe, other, owner_engine, job_queue) -> None:
    none = await client.post("/v1/webhooks/test", headers=bearer(cafe.admin_key))
    assert none.status_code == 409
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO webhook_endpoints (tenant_id, url, secret, enabled) "
                 "VALUES (:t, 'https://hooks.example/bap', 'whsec_test', true)"),
            {"t": cafe.id},
        )  # fmt: skip
    sent = await client.post("/v1/webhooks/test", headers=bearer(cafe.admin_key))
    assert sent.status_code == 202
    assert job_queue.webhooks[-1][:2] == (cafe.id, uuid_of(sent.json()["delivery_id"]))
    deliveries = await client.get("/v1/webhooks/deliveries", headers=bearer(cafe.admin_key))
    assert deliveries.json()[0]["event_type"] == "webhook.test"
    theirs = await client.post("/v1/webhooks/test", headers=bearer(other.admin_key))
    assert theirs.status_code == 409  # the other tenant has no endpoint, and can't use ours


def uuid_of(value: str):
    import uuid

    return uuid.UUID(value)


# --- Telegram status ---------------------------------------------------------------------------


async def test_telegram_status(client, cafe, other, monkeypatch) -> None:
    from tests.test_telegram import BOT_TOKEN, FakeTelegram

    fake = FakeTelegram()
    from app.channels import telegram

    monkeypatch.setattr(telegram, "CLIENT_FACTORY", fake.factory)
    empty = await client.get("/v1/channels/telegram/status", headers=bearer(cafe.admin_key))
    assert empty.json()["configured"] is False
    await client.put("/v1/channels/telegram", json={"bot_token": BOT_TOKEN, "staff_chat_id": -5},
                     headers=bearer(cafe.admin_key))  # fmt: skip
    status = await client.get("/v1/channels/telegram/status", headers=bearer(cafe.admin_key))
    body = status.json()
    assert body["configured"] and body["bot_ok"] and body["bot_username"] == "cafe_bot"
    assert body["staff_chat_id"] == -5 and body["webhook_registered"] is False
    assert BOT_TOKEN not in status.text
    theirs = await client.get("/v1/channels/telegram/status", headers=bearer(other.admin_key))
    assert theirs.json()["configured"] is False
