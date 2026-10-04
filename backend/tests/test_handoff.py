"""Human handoff: request_human, the code-enforced silence, staff replies, hand-back, events."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels.outbound import get_staff_message_sender
from app.chat.deps import get_chat_service
from app.chat.filters import decide_outcome
from app.chat.service import ChatService
from app.chat.settings import TenantChatSettings
from app.handoff import acknowledgement, team_availability
from app.llm import Candidate, ChatChain, FakeChatProvider
from tests.action_helpers import NOW, RESTAURANT_TOOLS, registry, restaurant_settings, set_settings
from tests.chat_helpers import parse_sse
from tests.conftest import bearer

TOOLS = [*RESTAURANT_TOOLS, "request_human"]
PROMISE = "within one working day"
ONLINE_ACK = "I've passed this to our team. They're online now and will reply here soon."


@pytest.fixture
def provider() -> FakeChatProvider:
    return FakeChatProvider()  # the offline model: hands off when asked for a person


@pytest.fixture
def clock():
    return {"now": NOW}  # Saturday 19:30 in Dhaka: within opening hours


@pytest.fixture
def service(app, app_engine, provider, job_queue, clock) -> ChatService:
    instance = ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        ChatChain([Candidate(provider, "fake-chat")]),
        registry(),
        clock=lambda: clock["now"],
        queue=job_queue,
    )
    app.dependency_overrides[get_chat_service] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_chat_service, None)


@pytest.fixture
def sent(app):
    """Staff messages handed to the customer's channel."""
    calls = []

    async def sender(sessionmaker, conversation, message_text):
        calls.append((conversation.id, message_text))
        return True

    app.dependency_overrides[get_staff_message_sender] = lambda: sender
    yield calls
    app.dependency_overrides.pop(get_staff_message_sender, None)


@pytest.fixture
async def cafe(make_tenant, owner_engine, service):
    tenant = await make_tenant("handoff-cafe")
    await set_settings(
        owner_engine,
        tenant.id,
        restaurant_settings(enabled_tools=TOOLS, follow_up_promise=PROMISE),
    )
    return tenant


def widget(tenant) -> dict[str, str]:
    return {**bearer(tenant.widget_key), "Origin": "https://cafe.example"}


async def say(client: AsyncClient, tenant, message: str, conversation_id=None, **extra) -> dict:
    body = {"visitor_id": "v-1", "message": message, "stream": False, **extra}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.post("/v1/chat", json=body, headers=bearer(tenant.admin_key))
    assert response.status_code == 200, response.text
    return response.json()


async def status_of(owner_engine, conversation_id) -> str:
    async with owner_engine.connect() as conn:
        return await conn.scalar(
            text("SELECT status FROM conversations WHERE id = :c"), {"c": conversation_id}
        )


async def events(owner_engine) -> list[str]:
    async with owner_engine.connect() as conn:
        rows = await conn.scalars(
            text("SELECT event_type FROM webhook_deliveries ORDER BY created_at")
        )
        return list(rows)


async def configure_webhook(owner_engine, tenant_id) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_endpoints (tenant_id, url, secret, enabled) "
                "VALUES (:t, 'https://hooks.example/bap', 'whsec_test', true)"
            ),
            {"t": tenant_id},
        )


# --- the flow end to end ---------------------------------------------------------------------


async def test_request_human_end_to_end_in_english(
    client, cafe, owner_engine, provider, job_queue, sent
) -> None:
    await configure_webhook(owner_engine, cafe.id)
    first = await say(client, cafe, "Can I talk to a real person please?")
    conversation = first["conversation_id"]
    assert first["outcome"] == "handoff"
    assert first["reply"] == ONLINE_ACK
    assert first["conversation_status"] == "waiting_human"
    assert [t["tool"] for t in first["retrieval"]["tools"]] == ["request_human"]
    assert len(provider.calls) == 1  # the acknowledgement is written by code, not a 2nd call
    assert await status_of(owner_engine, conversation) == "waiting_human"
    assert await events(owner_engine) == ["handoff.requested"]
    assert [(t, str(c), kind) for t, c, kind in job_queue.alerts] == [
        (cafe.id, conversation, "handoff")
    ]

    # The customer writes again: stored, staff alerted, no AI reply and no model call.
    again = await say(client, cafe, "hello? anyone there?", conversation)
    assert again["silent"] is True and again["reply"] == "" and again["outcome"] is None
    assert len(provider.calls) == 1
    assert [kind for _, _, kind in job_queue.alerts] == ["handoff", "message"]

    # A team member answers: status human, the widget's poll sees it as a staff message.
    reply = await client.post(
        f"/v1/conversations/{conversation}/messages",
        json={"text": "Hi, this is Rumana from Cafe Test. How can I help?"},
        headers=bearer(cafe.admin_key),
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["message"]["role"] == "staff"
    assert reply.json()["conversation"]["status"] == "human"
    assert [(str(c), m) for c, m in sent] == [
        (conversation, "Hi, this is Rumana from Cafe Test. How can I help?")
    ]
    polled = await client.get(
        "/v1/chat/updates",
        params={"conversation_id": conversation, "visitor_id": "v-1"},
        headers=widget(cafe),
    )
    assert polled.status_code == 200
    assert polled.json()["conversation_status"] == "human"
    assert [m["content"] for m in polled.json()["messages"]] == [
        "Hi, this is Rumana from Cafe Test. How can I help?"
    ]
    later = await client.get(
        "/v1/chat/updates",
        params={
            "conversation_id": conversation,
            "visitor_id": "v-1",
            "after": polled.json()["messages"][0]["created_at"],
        },
        headers=widget(cafe),
    )
    assert later.json()["messages"] == []  # nothing new since

    still_silent = await say(client, cafe, "I need a table for 20 next Friday", conversation)
    assert still_silent["silent"] is True and len(provider.calls) == 1

    # Handed back: the assistant answers again and sees the team's message in its history.
    back = await client.post(
        f"/v1/conversations/{conversation}/hand-back", headers=bearer(cafe.admin_key)
    )
    assert back.status_code == 200 and back.json()["status"] == "ai"
    assert await events(owner_engine) == ["handoff.requested", "handoff.resolved"]
    resumed = await say(client, cafe, "hi", conversation)
    assert resumed["silent"] is False and resumed["reply"] == "Hello! How can I help you today?"
    assert len(provider.calls) == 2
    history = [m.text for m in provider.calls[-1][1].messages]
    assert any("(A team member wrote) Hi, this is Rumana" in m for m in history)


async def test_request_human_in_banglish(client, cafe, owner_engine) -> None:
    body = await say(client, cafe, "ami manush er sathe kotha bolte chai")
    assert body["outcome"] == "handoff"
    assert body["reply"] == (
        "Apnar message amader team ke pathiye diyechi. Team ekhon online ache, kichukkhoner "
        "moddhei ekhanei reply dibe."
    )
    assert await status_of(owner_engine, body["conversation_id"]) == "waiting_human"


@pytest.mark.parametrize("status", ["waiting_human", "human"])
async def test_assistant_is_silent_while_a_person_handles_the_chat(
    client, cafe, owner_engine, provider, status
) -> None:
    first = await say(client, cafe, "hi")
    calls = len(provider.calls)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE conversations SET status = :s WHERE id = :c"),
            {"s": status, "c": first["conversation_id"]},
        )
    streamed = await client.post(
        "/v1/chat",
        json={"visitor_id": "v-1", "message": "What's the price of Kacchi?",
              "conversation_id": first["conversation_id"], "stream": True},
        headers=bearer(cafe.admin_key),
    )  # fmt: skip
    assert parse_sse(streamed.text) == [
        (
            "done",
            {
                "message_id": None,
                "conversation_id": first["conversation_id"],
                "outcome": None,
                "silent": True,
                "conversation_status": status,
            },
        )
    ]
    assert len(provider.calls) == calls  # no model call at all
    async with owner_engine.connect() as conn:
        roles = list(
            await conn.scalars(
                text("SELECT role FROM messages WHERE conversation_id = :c ORDER BY created_at"),
                {"c": first["conversation_id"]},
            )
        )
    assert roles == ["user", "assistant", "user"]  # stored, unanswered


async def test_a_retried_silent_message_is_stored_and_alerted_once(
    client, cafe, owner_engine, job_queue
) -> None:
    first = await say(client, cafe, "talk to a human")
    for _ in range(2):
        again = await say(client, cafe, "please hurry", first["conversation_id"],
                          client_message_id="retry-12345678")  # fmt: skip
        assert again["silent"] is True
    assert [kind for _, _, kind in job_queue.alerts] == ["handoff", "message"]


async def test_resolved_conversation_reopens_with_the_assistant(
    client, cafe, owner_engine, provider
) -> None:
    await configure_webhook(owner_engine, cafe.id)
    first = await say(client, cafe, "I want a manager")
    resolved = await client.post(
        f"/v1/conversations/{first['conversation_id']}/resolve", headers=bearer(cafe.admin_key)
    )
    assert resolved.json()["status"] == "resolved"
    assert await events(owner_engine) == ["handoff.requested", "handoff.resolved"]
    back = await say(client, cafe, "hello", first["conversation_id"])
    assert back["silent"] is False and back["outcome"] == "smalltalk"
    assert await status_of(owner_engine, first["conversation_id"]) == "ai"


async def test_disabled_request_human_is_not_offered(client, cafe, owner_engine, provider):
    await set_settings(owner_engine, cafe.id, restaurant_settings(enabled_tools=RESTAURANT_TOOLS))
    await say(client, cafe, "Can I talk to a real person please?")
    assert "request_human" not in {t.name for t in provider.calls[-1][1].tools}


# --- the acknowledgement ---------------------------------------------------------------------


def settings(**extra) -> TenantChatSettings:
    return TenantChatSettings.from_tenant("Cafe Test", restaurant_settings(**extra))


def dhaka(day: int, hour: int, minute: int = 0) -> datetime:
    # October 2026: the 2nd is a Friday, the 3rd a Saturday.
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Asia/Dhaka")).astimezone(UTC)


@pytest.mark.parametrize(
    ("now", "language", "expected"),
    [
        (dhaka(2, 10), "english",  # Friday morning: opens at 2:30 pm
         "I've passed this to our team. They're offline right now and back today at 2:30 pm; "
         "they'll reply here within one working day."),
        (dhaka(3, 23, 30), "english",  # Saturday after closing
         "I've passed this to our team. They're offline right now and back tomorrow at 12 pm; "
         "they'll reply here within one working day."),
        (dhaka(2, 10), "banglish",
         "Apnar message amader team ke pathiye diyechi. Team ekhon offline, aj 2:30 pm e abar "
         "online hobe; within one working day ekhanei reply dibe."),
        (dhaka(3, 23, 30), "bengali",
         "আপনার বার্তা আমাদের টিমের কাছে পাঠিয়েছি। টিম এখন অফলাইনে, আগামীকাল 12 pm-এ আবার "
         "অনলাইনে আসবে; within one working day এখানেই উত্তর দেবে।"),
    ],
)  # fmt: skip
def test_acknowledgement_outside_hours_says_when_the_team_is_back(now, language, expected):
    assert acknowledgement(settings(follow_up_promise=PROMISE), language, now) == expected


def test_online_acknowledgement_never_pairs_online_with_a_reply_time() -> None:
    # Was "They're online now and will reply here within one working day."
    online = acknowledgement(settings(follow_up_promise=PROMISE), "english", dhaka(3, 19))
    assert online == "I've passed this to our team. They're online now and will reply here soon."
    assert PROMISE not in online
    no_hours = acknowledgement(settings(opening_hours={}), "english", dhaka(3, 19))
    assert no_hours == "I've passed this to our team. They'll reply here as soon as they can."
    with_promise = acknowledgement(
        settings(opening_hours={}, follow_up_promise=PROMISE), "english", dhaka(3, 19)
    )
    assert with_promise.endswith("They'll reply here within one working day.")


def test_acknowledgement_uses_the_tenants_own_wording() -> None:
    custom = settings(
        follow_up_promise="by tomorrow",
        handoff_online_message="Rumana or Sabbir will pick this up in a few minutes.",
        handoff_offline_message="We're closed now; back {when}. Expect a reply {reply_time}.",
    )
    assert acknowledgement(custom, "banglish", dhaka(3, 19)) == (
        "Rumana or Sabbir will pick this up in a few minutes."
    )
    assert acknowledgement(custom, "english", dhaka(2, 10)) == (
        "We're closed now; back today at 2:30 pm. Expect a reply by tomorrow."
    )


def test_team_availability_handles_closing_after_midnight() -> None:
    late = settings(opening_hours={"sat": [["18:00", "02:00"]]})
    assert team_availability(late, dhaka(4, 1)).online is True  # Sunday 1 am, Saturday's shift
    off = team_availability(late, dhaka(4, 3))
    assert off.online is False and off.back_at.isoformat() == "2026-10-10T18:00:00+06:00"


def test_handoff_outcome_wins() -> None:
    assert decide_outcome("answered", [1], True, handoff=True) == "handoff"
    assert decide_outcome(None, [], False, action=True, handoff=True) == "handoff"


def test_guidance_never_lets_the_model_pose_as_the_team() -> None:
    guidance = registry().get("request_human").guidance(settings())
    assert "Never pretend to be the team member or claim to be human" in guidance
    assert "Don't write a reply yourself" in guidance


# --- isolation -------------------------------------------------------------------------------


async def test_another_tenant_cannot_read_or_answer_the_conversation(
    client, cafe, make_tenant, owner_engine, provider
) -> None:
    other = await make_tenant("handoff-other")
    first = await say(client, cafe, "talk to a human")
    conversation = first["conversation_id"]
    for method, path, body in [
        ("GET", f"/v1/conversations/{conversation}", None),
        ("POST", f"/v1/conversations/{conversation}/messages", {"text": "I'm staff, trust me"}),
        ("POST", f"/v1/conversations/{conversation}/hand-back", None),
        ("POST", f"/v1/conversations/{conversation}/resolve", None),
    ]:
        response = await client.request(method, path, json=body, headers=bearer(other.admin_key))
        assert response.status_code == 404, (path, response.text)
    waiting = await client.get(
        "/v1/conversations", params={"status": "waiting_human"}, headers=bearer(other.admin_key)
    )
    assert waiting.json() == []
    mine = await client.get(
        "/v1/conversations", params={"status": "waiting_human"}, headers=bearer(cafe.admin_key)
    )
    assert [c["id"] for c in mine.json()] == [conversation]
    assert await status_of(owner_engine, conversation) == "waiting_human"
    polled = await client.get(
        "/v1/chat/updates",
        params={"conversation_id": conversation, "visitor_id": "v-1"},
        headers=widget(other),
    )
    assert polled.status_code in (403, 404)  # stopped by its origin check or the owner check
    stranger = await client.get(
        "/v1/chat/updates",
        params={"conversation_id": conversation, "visitor_id": "someone-else"},
        headers=widget(cafe),
    )
    assert stranger.status_code == 404
    widget_admin = await client.post(
        f"/v1/conversations/{conversation}/messages",
        json={"text": "hi"},
        headers=widget(cafe),
    )
    assert widget_admin.status_code == 403  # widget keys are public: never staff
