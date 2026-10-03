"""Reservations, order lookup and lead capture: confirmation gate, rules, privacy, limits."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.chat.actions.orders import NOT_FOUND
from app.chat.actions.reservations import within_opening_hours
from app.chat.deps import get_chat_service
from app.chat.ratelimit import RateLimiter
from app.chat.service import ChatService
from app.chat.settings import TenantChatSettings
from app.llm import Candidate, ChatChain, FakeChatProvider, Scripted
from tests.action_helpers import (
    CONTACT,
    NOW,
    SHOP_TOOLS,
    call,
    make_context,
    registry,
    restaurant_settings,
    set_settings,
)
from tests.conftest import TEST_REDIS_URL, bearer

BOOKING = {"date": "2026-10-04", "time": "20:00", "party_size": 4, "name": "Rahim",
           "phone": "01711-000000"}  # fmt: skip
LEAD = {"name": "Sadia", "contact": "01811-222333", "interest": "50 sarees for a wedding"}


def tool_responder(tool: str, arguments: dict):
    """A model that calls `tool` on every customer message, then words the result."""

    def respond(request, model):
        last = request.messages[-1]
        if last.role == "tool":
            if "Already done" in last.text:
                return "[[answered]]\nThat's already done."
            if "BOOKED" in last.text or "SAVED" in last.text:
                return "[[answered]]\nAll done!"
            if "NOT DONE YET" in last.text:
                return "[[smalltalk]]\nShall I go ahead with those details?"
            return "[[no_answer]]\nSorry, I can't do that."
        if any(t.name == tool for t in request.tools):
            return Scripted(tool_calls=[(tool, arguments)])
        return "[[no_answer]]\nThat isn't something I can do here."

    return respond


@pytest.fixture
async def limiter():
    instance = RateLimiter(TEST_REDIS_URL)
    await instance._redis.flushdb()
    yield instance
    await instance._redis.flushdb()
    await instance.aclose()


@pytest.fixture
def provider() -> FakeChatProvider:
    return FakeChatProvider(tool_responder("create_reservation", BOOKING))


@pytest.fixture
def service(app, app_engine, provider, job_queue, limiter) -> ChatService:
    instance = ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        ChatChain([Candidate(provider, "fake-chat")]),
        registry(),
        clock=lambda: NOW,
        limiter=limiter,
        queue=job_queue,
    )
    app.dependency_overrides[get_chat_service] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_chat_service, None)


@pytest.fixture
async def cafe(make_tenant, owner_engine, service):
    tenant = await make_tenant("action-cafe")
    await set_settings(owner_engine, tenant.id, restaurant_settings())
    return tenant


async def say(client: AsyncClient, tenant, message: str, conversation_id=None, **extra) -> dict:
    body = {"visitor_id": "v-1", "message": message, "stream": False, **extra}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.post("/v1/chat", json=body, headers=bearer(tenant.admin_key))
    assert response.status_code == 200, response.text
    return response.json()


async def count(owner_engine, table: str) -> int:
    async with owner_engine.connect() as conn:
        return await conn.scalar(text(f"SELECT count(*) FROM {table}"))


async def configure_webhook(owner_engine, tenant_id) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO webhook_endpoints (tenant_id, url, secret, enabled) "
                 "VALUES (:t, 'https://hooks.example/bap', 'whsec_test', true)"),
            {"t": tenant_id},
        )  # fmt: skip


# --- reservations ------------------------------------------------------------------------------


async def test_booking_happens_only_after_confirmation(client, cafe, owner_engine, job_queue):
    await configure_webhook(owner_engine, cafe.id)
    first = await say(client, cafe, "table for 4 tomorrow 8pm, Rahim 01711-000000")
    assert first["reply"] == "Shall I go ahead with those details?"
    assert first["outcome"] == "smalltalk"
    assert first["retrieval"]["tools"][0]["status"] == "needs_confirmation"
    assert await count(owner_engine, "reservations") == 0

    second = await say(client, cafe, "yes please", first["conversation_id"])
    assert second["outcome"] == "action" and second["reply"] == "All done!"
    async with owner_engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM reservations"))).one()
        statuses = (await conn.scalars(text(
            "SELECT status FROM tool_calls ORDER BY created_at"))).all()  # fmt: skip
        pending = await conn.scalar(text("SELECT status FROM pending_actions"))
        delivery = (
            await conn.execute(text("SELECT event_type, payload FROM webhook_deliveries"))
        ).one()
    assert (row.party_size, row.name, row.phone, row.status) == (
        4,
        "Rahim",
        "01711000000",
        "confirmed",
    )
    assert row.starts_at == datetime(2026, 10, 4, 14, 0, tzinfo=UTC)  # 20:00 in Dhaka (UTC+6)
    assert (
        str(row.local_time) == "20:00:00" and str(row.conversation_id) == first["conversation_id"]
    )
    assert (
        row.reference.startswith("R-")
        and row.reference in second["retrieval"]["actions"][0]["summary"]
    )
    assert statuses == ["needs_confirmation", "ok"] and pending == "done"
    assert delivery.event_type == "reservation.created"
    assert delivery.payload["data"]["reference"] == row.reference
    assert len(job_queue.webhooks) == 1


async def test_repeated_confirmation_creates_one_booking(client, cafe, owner_engine) -> None:
    first = await say(client, cafe, "book it: 4 people tomorrow 8pm, Rahim 01711-000000")
    await say(client, cafe, "yes", first["conversation_id"])
    again = await say(client, cafe, "yes", first["conversation_id"])
    assert again["reply"] == "That's already done."
    assert again["outcome"] == "answered"
    assert await count(owner_engine, "reservations") == 1


async def test_no_booking_without_a_real_confirmation(client, cafe, owner_engine) -> None:
    first = await say(client, cafe, "4 people tomorrow 8pm, Rahim 01711-000000")
    # The model calls the tool again, but the customer did not confirm.
    other = await say(client, cafe, "wait, is there parking?", first["conversation_id"])
    assert other["retrieval"]["tools"][0]["status"] == "needs_confirmation"
    declined = await say(client, cafe, "no, cancel", first["conversation_id"])
    assert declined["retrieval"]["tools"][0]["status"] == "needs_confirmation"
    assert await count(owner_engine, "reservations") == 0


async def test_proposing_and_confirming_in_the_same_turn_does_not_book(
    client, cafe, owner_engine, provider
) -> None:
    def twice(request, model):  # calls the tool again right after the proposal, same turn
        tools_so_far = [m for m in request.messages if m.role == "tool"]
        if len(tools_so_far) < 2:
            return Scripted(tool_calls=[("create_reservation", BOOKING)])
        return "[[smalltalk]]\nShall I book it?"

    provider.responder = twice
    body = await say(client, cafe, "yes, book 4 people tomorrow 8pm, Rahim 01711-000000")
    assert [t["status"] for t in body["retrieval"]["tools"]] == ["needs_confirmation"] * 2
    assert await count(owner_engine, "reservations") == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"time": "03:00"}, "outside opening hours"),
        ({"date": "2026-10-03", "time": "19:00"}, "in the past"),  # 19:00 Dhaka < 19:30 now
        ({"date": "2026-10-09", "time": "13:00"}, "outside opening hours"),  # Friday before 14:30
        ({"party_size": 15}, "limited to 8 people"),
        ({"date": "2027-03-01"}, "up to 60 days ahead"),
    ],
)
async def test_bookings_outside_the_rules_are_refused(client, cafe, owner_engine, provider,
                                                       changes, reason) -> None:  # fmt: skip
    provider.responder = tool_responder("create_reservation", {**BOOKING, **changes})
    body = await say(client, cafe, "book it")
    tool = body["retrieval"]["tools"][0]
    assert tool["status"] == "refused" and reason in tool["summary"]
    assert body["outcome"] == "no_answer" and CONTACT in body["reply"]
    assert await count(owner_engine, "pending_actions") == 0
    assert await count(owner_engine, "reservations") == 0


async def test_times_are_judged_in_the_tenants_timezone(app_engine, cafe) -> None:
    # 19:00 on 3 October is still ahead in UTC (13:30 now) but has passed in Dhaka (19:30 now).
    context = make_context(app_engine, cafe.id, restaurant_settings())
    _, record = await call(context, "create_reservation", {**BOOKING, "date": "2026-10-03",
                                                            "time": "19:00"})  # fmt: skip
    assert record.status == "refused"
    utc = make_context(app_engine, cafe.id, restaurant_settings(timezone="UTC"))
    _, record = await call(utc, "create_reservation", {**BOOKING, "date": "2026-10-03",
                                                       "time": "19:00"})  # fmt: skip
    assert record.status == "needs_confirmation"


def test_opening_hours_including_after_midnight() -> None:
    settings = TenantChatSettings.from_tenant(
        "Bar", {"opening_hours": {"sat": [["18:00", "02:00"]]}, "timezone": "Asia/Dhaka"}
    )

    def at(day, hh, mm=0):
        return datetime(2026, 10, day, hh, mm)

    assert within_opening_hours(settings, at(3, 18))  # Saturday evening
    assert within_opening_hours(settings, at(4, 1, 30))  # Sunday 01:30 is still Saturday night
    assert not within_opening_hours(settings, at(4, 1, 45))  # less than 30 min before closing
    assert not within_opening_hours(settings, at(3, 17, 59))
    assert not within_opening_hours(settings, at(4, 18))  # Sunday: closed


# --- disabled tools and invalid arguments ------------------------------------------------------


async def test_disabled_tools_are_not_offered_and_cannot_run(client, cafe, owner_engine, provider):
    await set_settings(
        owner_engine, cafe.id, restaurant_settings(enabled_tools=["search_knowledge"])
    )
    body = await say(client, cafe, "book a table")
    offered = {t.name for t in provider.requests()[0].tools}
    assert offered == {"search_knowledge"}
    assert body["reply"].startswith("That isn't something I can do here.")

    def sneaky(request, model):  # calls it anyway
        if request.messages[-1].role == "tool":
            return "[[no_answer]]\nSorry."
        return Scripted(tool_calls=[("create_reservation", BOOKING)])

    provider.responder = sneaky
    body = await say(client, cafe, "book a table")
    assert body["retrieval"]["tools"][0]["status"] == "not_allowed"
    assert await count(owner_engine, "pending_actions") == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {**BOOKING, "party_size": "many"},
        {k: v for k, v in BOOKING.items() if k != "phone"},
        {**BOOKING, "time": "8pm"},
        {**BOOKING, "phone": "123"},
        {**BOOKING, "colour": "red"},
    ],
)
async def test_invalid_arguments_go_back_to_the_model(client, cafe, owner_engine, provider,
                                                      arguments) -> None:  # fmt: skip
    provider.responder = tool_responder("create_reservation", arguments)
    body = await say(client, cafe, "book it")
    assert body["retrieval"]["tools"][0]["status"] == "invalid_arguments"
    tool_message = next(m for m in provider.requests()[-1].messages if m.role == "tool")
    assert tool_message.text.startswith("Invalid arguments for create_reservation")
    assert await count(owner_engine, "pending_actions") == 0


async def test_write_tools_are_limited_per_visitor(app_engine, cafe, limiter) -> None:
    statuses = []
    for party in range(1, 9):
        context = make_context(app_engine, cafe.id, restaurant_settings(), limiter=limiter,
                               visitor_id="spammer")  # fmt: skip
        _, record = await call(context, "create_reservation", {**BOOKING, "party_size": party})
        statuses.append(record.status)
    assert statuses[:6] == ["needs_confirmation"] * 6
    assert statuses[6:] == ["rate_limited"] * 2


# --- order lookup ------------------------------------------------------------------------------


@pytest.fixture
async def shop(make_tenant, owner_engine):
    tenant = await make_tenant("action-shop")
    await set_settings(owner_engine, tenant.id, restaurant_settings(enabled_tools=SHOP_TOOLS))
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orders (tenant_id, order_number, status, items, total, currency, "
                 "phone, placed_at, courier) VALUES (:t, 'JL-10232', 'shipped', "
                 "CAST(:items AS jsonb), 12500, 'BDT', '01819-876543', now(), 'Steadfast')"),
            {"t": tenant.id, "items": '[{"name": "Half-silk Jamdani Saree", "qty": 1}]'},
        )  # fmt: skip
    return tenant


async def test_order_lookup_needs_the_right_phone_digits(app_engine, shop) -> None:
    settings = restaurant_settings(enabled_tools=SHOP_TOOLS)
    context = make_context(app_engine, shop.id, settings)
    found, record = await call(context, "lookup_order", {"order_number": "jl-10232",
                                                         "phone_last4": "6543"})  # fmt: skip
    assert "status shipped" in found and "Half-silk Jamdani Saree" in found and "Steadfast" in found
    assert "01819" not in found and record.result_summary == "found (shipped)"
    assert context.lookups == 1

    wrong_digits, _ = await call(context, "lookup_order", {"order_number": "JL-10232",
                                                           "phone_last4": "0000"})  # fmt: skip
    unknown, _ = await call(context, "lookup_order", {"order_number": "JL-99999",
                                                      "phone_last4": "6543"})  # fmt: skip
    assert wrong_digits == unknown  # identical: no way to tell which part was wrong
    assert NOT_FOUND in unknown


async def test_order_lookup_attempts_are_limited(app_engine, shop, limiter) -> None:
    settings = restaurant_settings(enabled_tools=SHOP_TOOLS)
    statuses = []
    for digits in range(7):
        context = make_context(app_engine, shop.id, settings, limiter=limiter, visitor_id="prober")
        guess = {"order_number": "JL-10232", "phone_last4": f"{digits:04d}"}
        _, record = await call(context, "lookup_order", guess)
        statuses.append(record.status)
    assert statuses == ["ok"] * 5 + ["rate_limited"] * 2


# --- leads -------------------------------------------------------------------------------------


async def test_lead_capture_stores_the_lead_and_links_the_conversation(
    client, cafe, owner_engine, provider, job_queue
) -> None:
    await configure_webhook(owner_engine, cafe.id)
    provider.responder = tool_responder("capture_lead", LEAD)
    first = await say(client, cafe, "I want 50 sarees for a wedding, can someone call me?")
    assert await count(owner_engine, "leads") == 0
    second = await say(client, cafe, "haa thik ache", first["conversation_id"])
    assert second["outcome"] == "action"
    async with owner_engine.connect() as conn:
        lead = (await conn.execute(text("SELECT * FROM leads"))).one()
        event = await conn.scalar(text("SELECT event_type FROM webhook_deliveries"))
    assert (lead.name, lead.contact, lead.interest) == ("Sadia", "01811222333", LEAD["interest"])
    assert str(lead.conversation_id) == first["conversation_id"] and lead.status == "new"
    assert event == "lead.created" and len(job_queue.webhooks) == 1


def test_lead_guidance_never_invents_a_follow_up_time() -> None:
    tool = registry().get("capture_lead")
    plain = TenantChatSettings.from_tenant("Cafe", {})
    assert "do not promise any time" in tool.guidance(plain)
    promised = TenantChatSettings.from_tenant(
        "Cafe", {"follow_up_promise": "within one working day"}
    )
    assert "within one working day" in tool.guidance(promised)
