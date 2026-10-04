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
