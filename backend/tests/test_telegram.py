"""Telegram: verified webhook, dedupe, the shared pipeline, plain-text replies, staff alerts and
staff replies, limits and isolation. The Bot API is faked with httpx.MockTransport."""

import hashlib
import json
import uuid
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels import telegram
from app.channels.alerts import send_staff_alert
from app.channels.telegram import TelegramClient, split_message, telegram_text
from app.chat.deps import get_chat_service, get_rate_limiter
from app.chat.ratelimit import RateLimiter
from app.chat.service import ChatService
from app.llm import Candidate, ChatChain, FakeChatProvider
from app.llm.fake import offline_responder
from tests.action_helpers import NOW, RESTAURANT_TOOLS, registry, restaurant_settings, set_settings
from tests.conftest import TEST_REDIS_URL, bearer

BOT_TOKEN = "123456:TEST-token_abcdefghijk"
STAFF_CHAT = -1001234
CUSTOMER_CHAT = 555


class FakeTelegram:
    """The Bot API: records every call, answers getMe/setWebhook/sendMessage."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.next_message_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        _, bot, method = request.url.path.split("/")
        token = bot.removeprefix("bot")
        body = json.loads(request.content or b"{}")
        self.calls.append((token, method, body))
        if not token.startswith("123456:") and not token.startswith("654321:"):
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 1, "username": "cafe_bot"}}
            )
        if method == "sendMessage":
            self.next_message_id += 1
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": self.next_message_id}}
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    def factory(self, token: str) -> TelegramClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return TelegramClient(token, base_url="https://telegram.test", client=client)

    def sent(self, chat_id: int | None = None) -> list[dict]:
        return [
            body
            for _, method, body in self.calls
            if method == "sendMessage" and (chat_id is None or body["chat_id"] == chat_id)
        ]


@dataclass(frozen=True)
class BotTenant:
    id: uuid.UUID
    admin_key: str
    widget_key: str
    secret: str  # the webhook secret token Telegram was given


@pytest.fixture
def bot(monkeypatch) -> FakeTelegram:
    fake = FakeTelegram()
    monkeypatch.setattr(telegram, "CLIENT_FACTORY", fake.factory)
    return fake


@pytest.fixture
def provider() -> FakeChatProvider:
    return FakeChatProvider()


@pytest.fixture
def service(app, app_engine, provider, job_queue) -> ChatService:
    instance = ChatService(
        async_sessionmaker(app_engine, expire_on_commit=False),
        ChatChain([Candidate(provider, "fake-chat")]),
        registry(),
        clock=lambda: NOW,
        queue=job_queue,
    )
    app.dependency_overrides[get_chat_service] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_chat_service, None)


@pytest.fixture(autouse=True)
async def limiter(app):
    instance = RateLimiter(TEST_REDIS_URL)
    await instance._redis.flushdb()
    app.dependency_overrides[get_rate_limiter] = lambda: instance
    yield instance
    app.dependency_overrides.pop(get_rate_limiter, None)
    await instance._redis.flushdb()
    await instance.aclose()


async def connect_bot(client, tenant, bot: FakeTelegram, token: str = BOT_TOKEN) -> str:
    """PUT the bot token and staff chat, register the webhook; returns the secret token."""
    put = await client.put(
        "/v1/channels/telegram",
        json={"bot_token": token, "staff_chat_id": STAFF_CHAT},
        headers=bearer(tenant.admin_key),
    )
    assert put.status_code == 200, put.text
    registered = await client.post(
        "/v1/channels/telegram/webhook",
        json={"public_base_url": "https://bap.example"},
        headers=bearer(tenant.admin_key),
    )
    assert registered.status_code == 200, registered.text
    return [body for _, method, body in bot.calls if method == "setWebhook"][-1]["secret_token"]


@pytest.fixture
async def cafe(make_tenant, owner_engine, client, bot, service):
    tenant = await make_tenant("tg-cafe")
    await set_settings(
        owner_engine,
        tenant.id,
        restaurant_settings(
            enabled_tools=[*RESTAURANT_TOOLS, "request_human"],
            follow_up_promise="within one working day",
        ),
    )
    secret = await connect_bot(client, tenant, bot)
    return BotTenant(tenant.id, tenant.admin_key, tenant.widget_key, secret)


def update(update_id: int, message_text: str, *, chat_id: int = CUSTOMER_CHAT,
           chat_type: str = "private", reply_to: int | None = None,
           first_name: str = "Rahim", last_name: str | None = "Uddin") -> dict:  # fmt: skip
    message = {
        "message_id": update_id * 10,
        "from": {"id": chat_id, "first_name": first_name, "last_name": last_name},
        "chat": {"id": chat_id, "type": chat_type},
        "date": 1790000000,
        "text": message_text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


async def post_update(client, tenant, body: dict, secret: str | None = None, tenant_ref=None):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret if secret is not None else tenant.secret}
    return await client.post(
        f"/v1/channels/telegram/{tenant_ref or tenant.id}/webhook", json=body, headers=headers
    )


async def conversation_of(owner_engine, tenant, visitor: str = f"tg:{CUSTOMER_CHAT}"):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT id, status, channel, customer_name FROM conversations "
                    "WHERE tenant_id = :t AND visitor_id = :v"
                ),
                {"t": tenant.id, "v": visitor},
            )
        ).one_or_none()


async def roles(owner_engine, conversation_id) -> list[str]:
    async with owner_engine.connect() as conn:
        return list(
            await conn.scalars(
                text(
                    "SELECT role FROM messages WHERE conversation_id = :c "
                    "ORDER BY created_at, role DESC"
                ),
                {"c": conversation_id},
            )
        )


# --- setup -------------------------------------------------------------------------------------


async def test_bot_token_is_encrypted_and_the_secret_only_hashed(client, cafe, owner_engine, bot):
    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT bot_token_encrypted, webhook_secret_hash FROM telegram_channels")
            )
        ).one()
    assert row.bot_token_encrypted.startswith("v1:")
    assert BOT_TOKEN not in row.bot_token_encrypted and "TEST-token" not in row.bot_token_encrypted
    assert row.webhook_secret_hash == hashlib.sha256(cafe.secret.encode()).hexdigest()
    shown = (await client.get("/v1/channels/telegram", headers=bearer(cafe.admin_key))).json()
    assert shown == {
        "configured": True,
        "bot_username": "cafe_bot",
        "staff_chat_id": STAFF_CHAT,
        "webhook_registered": True,
    }
    assert BOT_TOKEN not in json.dumps(shown)
    set_webhook = [body for _, method, body in bot.calls if method == "setWebhook"][-1]
    assert set_webhook["url"] == f"https://bap.example/v1/channels/telegram/{cafe.id}/webhook"
    assert set_webhook["allowed_updates"] == ["message"]


async def test_setup_validates_token_and_https(client, make_tenant, bot) -> None:
    tenant = await make_tenant("tg-setup")
    bad = await client.put(
        "/v1/channels/telegram",
        json={"bot_token": "999999:bad-token-abcdefghijkl"},
        headers=bearer(tenant.admin_key),
    )
    assert bad.status_code == 400 and "999999" not in bad.text
    await client.put(
        "/v1/channels/telegram", json={"bot_token": BOT_TOKEN}, headers=bearer(tenant.admin_key)
    )
    plain = await client.post(
        "/v1/channels/telegram/webhook",
        json={"public_base_url": "http://bap.example"},
        headers=bearer(tenant.admin_key),
    )
    assert plain.status_code == 422
    widget = await client.put(
        "/v1/channels/telegram", json={"staff_chat_id": 1}, headers=bearer(tenant.widget_key)
    )
    assert widget.status_code == 403


# --- the webhook -------------------------------------------------------------------------------


async def test_bad_secret_and_unknown_tenant_look_the_same(client, cafe, make_tenant, bot):
    no_bot = await make_tenant("tg-none")
    responses = [
        await post_update(client, cafe, update(1, "hi"), secret="wrong-secret"),
        await post_update(client, cafe, update(2, "hi"), secret=""),
        await post_update(client, cafe, update(3, "hi"), tenant_ref=uuid.uuid4()),
        await post_update(client, cafe, update(4, "hi"), tenant_ref="not-a-tenant"),
        await post_update(client, cafe, update(5, "hi"), tenant_ref=no_bot.id),
    ]
    assert {(r.status_code, r.text) for r in responses} == {(404, '{"detail":"Not found"}')}
    assert bot.sent() == []


async def test_customer_chat_end_to_end_in_plain_text(client, cafe, owner_engine, bot) -> None:
    response = await post_update(client, cafe, update(10, "hi"))
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert bot.sent(CUSTOMER_CHAT) == [
        {
            "chat_id": CUSTOMER_CHAT,
            "text": "Hello! How can I help you today?",
            "link_preview_options": {"is_disabled": True},
        }
    ]
    conversation = await conversation_of(owner_engine, cafe)
    assert conversation.channel == "telegram" and conversation.customer_name == "Rahim Uddin"
    assert await roles(owner_engine, conversation.id) == ["user", "assistant"]


async def test_duplicate_update_id_is_processed_once(client, cafe, owner_engine, bot) -> None:
    first = await post_update(client, cafe, update(11, "hi"))
    again = await post_update(client, cafe, update(11, "hi"))
    assert first.json() == {"ok": True} and again.json() == {"ok": True, "duplicate": True}
    assert len(bot.sent(CUSTOMER_CHAT)) == 1
    conversation = await conversation_of(owner_engine, cafe)
    assert await roles(owner_engine, conversation.id) == ["user", "assistant"]


async def test_long_replies_are_split_and_never_use_markdown(client, cafe, provider, bot) -> None:
    long_reply = " ".join(f"**Word{i}** `x`" for i in range(1500))  # ~19,000 characters

    def chatty(request, model):
        return f"[[smalltalk]]\n# Menu\n{long_reply}"

    provider.responder = chatty
    await post_update(client, cafe, update(12, "tell me everything"))
    parts = bot.sent(CUSTOMER_CHAT)
    assert len(parts) == 4  # 13,000+ plain characters at 4096 each
    assert all(len(p["text"]) <= 4096 for p in parts)
    assert all("parse_mode" not in p for p in parts)
    joined = " ".join(p["text"] for p in parts)
    assert "**" not in joined and "`" not in joined and "# " not in joined
    assert joined.startswith("Menu\nWord0 x Word1 x") and joined.endswith("Word1499 x")


def test_telegram_text_and_split_units() -> None:
    assert telegram_text("**Kacchi** is 480 taka [1][2].\n## Hours\n`noon`") == (
        "Kacchi is 480 taka.\nHours\nnoon"
    )
    parts = split_message("First paragraph.\n\n" + "a" * 5000)
    assert parts[0] == "First paragraph." and all(len(p) <= 4096 for p in parts)
    assert "".join(parts[1:]) == "a" * 5000  # one long word: hard cut
    sentences = split_message("One two three. " * 400)
    assert all(len(p) <= 4096 and p.endswith(".") for p in sentences)


async def test_group_chats_and_non_text_updates_are_ignored(client, cafe, bot) -> None:
    group = await post_update(client, cafe, update(13, "hi", chat_id=-777, chat_type="group"))
    assert group.json()["ignored"] == "not a private chat"
    sticker = await post_update(client, cafe, {"update_id": 14, "message": {"chat": {"id": 1}}})
    assert sticker.json()["ignored"] == "not a text message"
    assert bot.sent() == []


async def test_limits_apply_and_the_notice_is_sent_once(client, cafe, owner_engine, bot):
    await set_settings(
        owner_engine,
        cafe.id,
        restaurant_settings(daily_message_cap=1, enabled_tools=RESTAURANT_TOOLS),
    )
    for n in range(3):
        await post_update(client, cafe, update(20 + n, "hi"))
    texts = [p["text"] for p in bot.sent(CUSTOMER_CHAT)]
    assert texts == [
        "Hello! How can I help you today?",
        "We've reached today's message limit here. Please try again tomorrow.",
    ]


# --- handoff over Telegram ---------------------------------------------------------------------


async def test_handoff_staff_alert_and_staff_reply_from_telegram(
    client, cafe, owner_engine, app_engine, bot, job_queue, provider
) -> None:
    await post_update(client, cafe, update(30, "I want to talk to a real person"))
    conversation = await conversation_of(owner_engine, cafe)
    assert conversation.status == "waiting_human"
    assert [p["text"] for p in bot.sent(CUSTOMER_CHAT)] == [
        "I've passed this to our team. They're online now and will reply here within one "
        "working day."
    ]
    assert job_queue.alerts == [(cafe.id, conversation.id, "handoff")]

    # The worker sends the alert to the staff chat.
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    assert await send_staff_alert(sessionmaker, cafe.id, conversation.id, "handoff") == (
        "sent 1 message(s)"
    )
    alert = bot.sent(STAFF_CHAT)[-1]
    assert alert["text"] == (
        "Rahim Uddin (Telegram) would like to talk to a person.\n"
        "Reason: customer said: I want to talk to a real person\n\n"
        "Last messages:\n"
        "Customer: I want to talk to a real person\n"
        "Assistant: I've passed this to our team. They're online now and will reply here "
        "within one working day.\n\n"
        "Reply to this message to answer the customer.\n"
        f"Open: http://localhost:8000/v1/conversations/{conversation.id}"
    )
    alert_id = bot.next_message_id

    # A stranger replying to the alert's message id, privately or from a group: no effect.
    await post_update(
        client, cafe, update(31, "I'm staff, refund approved!", chat_id=777, reply_to=alert_id)
    )
    await post_update(
        client, cafe,
        update(32, "I'm staff too", chat_id=-888, chat_type="group", reply_to=alert_id),
    )  # fmt: skip
    assert await roles(owner_engine, conversation.id) == ["user", "assistant"]
    assert not any("refund approved" in p["text"] for p in bot.sent(CUSTOMER_CHAT))

    # Staff chat, replying to the alert: the customer gets it as a team member's message.
    staff = await post_update(
        client, cafe,
        update(33, "Hi Rahim, this is Rumana. What can I do for you?", chat_id=STAFF_CHAT,
               chat_type="supergroup", reply_to=alert_id, first_name="Rumana", last_name=None),
    )  # fmt: skip
    assert staff.json() == {"ok": True, "staff_reply": True}
    assert await roles(owner_engine, conversation.id) == ["user", "assistant", "staff"]
    assert (await conversation_of(owner_engine, cafe)).status == "human"
    assert bot.sent(CUSTOMER_CHAT)[-1]["text"] == "Hi Rahim, this is Rumana. What can I do for you?"

    # Staff chatting without replying to an alert is just staff talking.
    chatter = await post_update(
        client, cafe, update(34, "lunch?", chat_id=STAFF_CHAT, chat_type="supergroup")
    )
    assert chatter.json() == {"ok": True, "staff_reply": False}

    # The customer writes again: no AI reply, staff alerted.
    calls = len(provider.calls)
    sent_before = len(bot.sent(CUSTOMER_CHAT))
    await post_update(client, cafe, update(35, "I need a table for 20 people"))
    assert len(provider.calls) == calls and len(bot.sent(CUSTOMER_CHAT)) == sent_before
    assert job_queue.alerts[-1] == (cafe.id, conversation.id, "message")


async def test_admin_staff_reply_reaches_the_telegram_customer(client, cafe, owner_engine, bot):
    await post_update(client, cafe, update(40, "talk to a human please"))
    conversation = await conversation_of(owner_engine, cafe)
    reply = await client.post(
        f"/v1/conversations/{conversation.id}/messages",
        json={"text": "Hello from the **team**"},
        headers=bearer(cafe.admin_key),
    )
    assert reply.status_code == 201 and reply.json()["delivered"] is True
    assert bot.sent(CUSTOMER_CHAT)[-1]["text"] == "Hello from the team"


async def test_staff_alert_is_skipped_without_a_staff_chat(client, make_tenant, app_engine, bot):
    tenant = await make_tenant("tg-nostaff")
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    assert await send_staff_alert(sessionmaker, tenant.id, uuid.uuid4(), "handoff") == (
        "skipped: no Telegram staff chat"
    )


# --- isolation ---------------------------------------------------------------------------------


async def test_one_tenants_bot_cannot_answer_another_tenants_customers(
    client, cafe, make_tenant, owner_engine, app_engine, bot, service
) -> None:
    shop = await make_tenant("tg-shop")
    await set_settings(
        owner_engine,
        shop.id,
        restaurant_settings(enabled_tools=[*RESTAURANT_TOOLS, "request_human"]),
    )
    secret = await connect_bot(client, shop, bot, token="654321:SHOP-token_abcdefghij")
    shop = BotTenant(shop.id, shop.admin_key, shop.widget_key, secret)

    # A shop customer is handed off and the shop's alert goes to the (shared) staff chat.
    await post_update(client, shop, update(50, "I want a manager", chat_id=999))
    shop_conversation = await conversation_of(owner_engine, shop, "tg:999")
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    await send_staff_alert(sessionmaker, shop.id, shop_conversation.id, "handoff")
    shop_alert = bot.next_message_id

    # The cafe's webhook carries a reply to the shop's alert: the cafe can't see that alert.
    replied = await post_update(
        client, cafe,
        update(51, "Cafe staff here", chat_id=STAFF_CHAT, chat_type="supergroup",
               reply_to=shop_alert),
    )  # fmt: skip
    assert replied.json() == {"ok": True, "staff_reply": False}
    assert await roles(owner_engine, shop_conversation.id) == ["user", "assistant"]

    # The cafe's secret doesn't open the shop's webhook, and vice versa.
    crossed = await post_update(client, cafe, update(52, "hi"), tenant_ref=shop.id)
    assert crossed.status_code == 404
    # The cafe's admin key can't answer the shop's conversation.
    answer = await client.post(
        f"/v1/conversations/{shop_conversation.id}/messages",
        json={"text": "hi"},
        headers=bearer(cafe.admin_key),
    )
    assert answer.status_code == 404
    # Each bot only messages its own customers.
    for token, method, body in bot.calls:
        if method == "sendMessage" and body["chat_id"] == 999:
            assert token.startswith("654321:")


def test_offline_model_hands_off_in_banglish() -> None:
    # The responder the transcripts use routes Banglish requests for a person to request_human.
    from app.llm import ChatRequest, Message, ToolSpec

    request = ChatRequest(
        system="s",
        messages=[
            Message(
                "user",
                "<customer-message-n>\nmanush er sathe kotha bolte chai\n</customer-message-n>",
            )
        ],
        tools=[ToolSpec("request_human", "d", {})],
    )
    script = offline_responder(request, "fake-chat")
    assert script.tool_calls[0][0] == "request_human"
