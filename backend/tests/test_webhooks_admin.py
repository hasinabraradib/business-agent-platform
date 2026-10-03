"""Signed webhook delivery, the admin endpoints, isolation of the new tables, the n8n file."""

import json
import uuid
from functools import partial
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.ingestion.fetch import PostResult, post_json
from app.routers.documents import get_url_resolver
from app.webhooks.delivery import MAX_ATTEMPTS, deliver, sign, verify
from tests.conftest import bearer

NEW_TABLES = (
    "catalog_items",
    "pending_actions",
    "reservations",
    "orders",
    "leads",
    "tool_calls",
    "webhook_endpoints",
    "webhook_deliveries",
)


async def public_resolver(host: str, port: int) -> list[str]:
    return {"hooks.example": ["93.184.216.34"], "internal.example": ["10.0.0.7"]}.get(host, [])


@pytest.fixture(autouse=True)
def _resolver(app):
    app.dependency_overrides[get_url_resolver] = lambda: public_resolver
    yield
    app.dependency_overrides.pop(get_url_resolver, None)


async def add_delivery(owner_engine, tenant_id, url="https://hooks.example/bap") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_endpoints (tenant_id, url, secret, enabled) "
                "VALUES (:t, :u, 'whsec_test', true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant_id, "u": url},
        )
        return await conn.scalar(
            text(
                "INSERT INTO webhook_deliveries (tenant_id, event_type, payload, url) VALUES "
                "(:t, 'lead.created', CAST(:p AS jsonb), :u) RETURNING id"
            ),
            {"t": tenant_id, "u": url, "p": json.dumps({"type": "lead.created", "data": {"x": 1}})},
        )


async def delivery_row(owner_engine, delivery_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM webhook_deliveries WHERE id = :d"), {"d": delivery_id}
            )
        ).one()


def test_signature_round_trip() -> None:
    body = b'{"type":"lead.created"}'
    signature = sign("whsec_test", 1_700_000_000, body)
    assert signature.startswith("v1=") and len(signature) == 3 + 64
    assert verify("whsec_test", 1_700_000_000, body, signature, now=1_700_000_100)
    assert not verify("whsec_test", 1_700_000_000, body + b" ", signature, now=1_700_000_100)
    assert not verify("other", 1_700_000_000, body, signature, now=1_700_000_100)
    assert not verify("whsec_test", 1_700_000_000, body, signature, now=1_700_000_000 + 301)


async def test_delivery_is_signed_and_logged(app_engine, owner_engine, make_tenant) -> None:
    tenant = await make_tenant("hook-a")
    delivery_id = await add_delivery(owner_engine, tenant.id)
    seen = []

    async def poster(url, body, headers, *, timeout_seconds):
        seen.append((url, body, headers))
        return PostResult(200, "ok")

    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    result = await deliver(
        sessionmaker, tenant.id, delivery_id, poster=poster, clock=lambda: 1_700_000_000
    )
    assert result.status == "delivered"
    url, body, headers = seen[0]
    assert url == "https://hooks.example/bap"
    assert headers["X-BAP-Event"] == "lead.created"
    assert headers["X-BAP-Delivery"] == str(delivery_id)
    assert headers["X-BAP-Timestamp"] == "1700000000"
    assert verify("whsec_test", 1_700_000_000, body, headers["X-BAP-Signature"], now=1_700_000_000)
    row = await delivery_row(owner_engine, delivery_id)
    assert (row.status, row.attempts, row.last_status_code) == ("delivered", 1, 200)
    assert row.attempt_log[0]["status_code"] == 200 and row.delivered_at is not None


async def test_failures_are_retried_with_backoff_then_given_up(
    app_engine, owner_engine, make_tenant
):
    tenant = await make_tenant("hook-b")
    delivery_id = await add_delivery(owner_engine, tenant.id)

    async def failing(url, body, headers, *, timeout_seconds):
        return PostResult(503, "busy")

    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    results = [
        await deliver(sessionmaker, tenant.id, delivery_id, poster=failing)
        for _ in range(MAX_ATTEMPTS + 1)
    ]
    assert [r.status for r in results[:-2]] == ["retrying"] * (MAX_ATTEMPTS - 1)
    assert [r.retry_in for r in results[:3]] == [10, 60, 300]
    assert results[MAX_ATTEMPTS - 1].status == "failed"
    assert results[-1].status == "skipped"  # nothing more after giving up
    row = await delivery_row(owner_engine, delivery_id)
    assert (row.status, row.attempts) == ("failed", MAX_ATTEMPTS)
    assert row.last_error == "HTTP 503: busy" and len(row.attempt_log) == MAX_ATTEMPTS


async def test_private_addresses_are_refused(app_engine, owner_engine, make_tenant) -> None:
    tenant = await make_tenant("hook-c")
    delivery_id = await add_delivery(owner_engine, tenant.id, url="https://internal.example/x")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200)

    poster = partial(post_json, resolver=public_resolver, transport=httpx.MockTransport(handler))
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    result = await deliver(sessionmaker, tenant.id, delivery_id, poster=poster)
    assert result.status == "failed" and sent == []
    row = await delivery_row(owner_engine, delivery_id)
    assert row.last_error.startswith("refused: refusing to fetch a non-public address")


async def test_real_post_pins_the_checked_address(app_engine, owner_engine, make_tenant) -> None:
    tenant = await make_tenant("hook-d")
    delivery_id = await add_delivery(owner_engine, tenant.id)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(204)

    poster = partial(post_json, resolver=public_resolver, transport=httpx.MockTransport(handler))
    sessionmaker = async_sessionmaker(app_engine, expire_on_commit=False)
    assert (
        await deliver(sessionmaker, tenant.id, delivery_id, poster=poster)
    ).status == "delivered"
    request = sent[0]
    assert request.url.host == "93.184.216.34" and request.headers["host"] == "hooks.example"
    assert request.headers["x-bap-signature"].startswith("v1=")


# --- admin endpoints ---------------------------------------------------------------------------


async def test_webhook_endpoint_setup_and_secret_rotation(client, make_tenant) -> None:
    tenant = await make_tenant("hook-admin")
    headers = bearer(tenant.admin_key)
    refused = await client.put(
        "/v1/webhooks/endpoint", json={"url": "http://10.0.0.5/hook"}, headers=headers
    )
    assert refused.status_code == 400
    created = (
        await client.put(
            "/v1/webhooks/endpoint", json={"url": "https://hooks.example/n8n"}, headers=headers
        )
    ).json()
    assert created["secret"].startswith("whsec_") and created["url"] == "https://hooks.example/n8n"
    shown = (await client.get("/v1/webhooks/endpoint", headers=headers)).json()
    assert shown["secret"] is None and created["secret"].startswith(shown["secret_hint"][:-1])
    updated = (
        await client.put(
            "/v1/webhooks/endpoint", json={"url": "https://hooks.example/v2"}, headers=headers
        )
    ).json()
    assert updated["secret"] is None  # not re-revealed on update
    rotated = (await client.post("/v1/webhooks/endpoint/rotate-secret", headers=headers)).json()
    assert rotated["secret"] != created["secret"]


@pytest.fixture
async def two_tenants_with_rows(make_tenant, owner_engine: AsyncEngine):
    """Tenants A and B, each with one row in every new table (inserted as the owner)."""
    tenants = [await make_tenant("rows-a"), await make_tenant("rows-b")]
    async with owner_engine.begin() as conn:
        for tenant in tenants:
            t = {"t": tenant.id}
            conversation = await conn.scalar(
                text(
                    "INSERT INTO conversations (tenant_id, visitor_id) "
                    "VALUES (:t, 'v') RETURNING id"
                ),
                t,
            )
            message = await conn.scalar(
                text(
                    "INSERT INTO messages (tenant_id, conversation_id, role, content) "
                    "VALUES (:t, :c, 'assistant', 'hi') RETURNING id"
                ),
                {**t, "c": conversation},
            )
            document = await conn.scalar(
                text(
                    "INSERT INTO documents (tenant_id, title, source_type) "
                    "VALUES (:t, 'Menu', 'csv') RETURNING id"
                ),
                t,
            )
            await conn.execute(
                text(
                    "INSERT INTO catalog_items (tenant_id, document_id, row, name, currency) "
                    "VALUES (:t, :d, 1, 'Tea', 'BDT')"
                ),
                {**t, "d": document},
            )
            await conn.execute(
                text(
                    "INSERT INTO pending_actions (tenant_id, conversation_id, tool, arguments, "
                    "args_hash, proposed_in, status) "
                    "VALUES (:t, :c, 'capture_lead', '{}', 'h', :m, 'pending')"
                ),
                {**t, "c": conversation, "m": message},
            )
            await conn.execute(
                text(
                    "INSERT INTO reservations (tenant_id, conversation_id, reference, starts_at, "
                    "local_date, local_time, party_size, name, phone) VALUES (:t, :c, :r, now(), "
                    "current_date, '20:00', 2, 'X', '017')"
                ),
                {**t, "c": conversation, "r": f"R-{str(tenant.id)[:6]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO orders (tenant_id, order_number, status, items, total, currency, "
                    "phone, placed_at) "
                    "VALUES (:t, 'JL-1', 'processing', '[]', 1, 'BDT', '0170', now())"
                ),
                t,
            )
            await conn.execute(
                text(
                    "INSERT INTO leads (tenant_id, conversation_id, name, contact, interest) "
                    "VALUES (:t, :c, 'X', '017', 'quote')"
                ),
                {**t, "c": conversation},
            )
            await conn.execute(
                text(
                    "INSERT INTO tool_calls (tenant_id, conversation_id, message_id, step, tool, "
                    "arguments, status, result_summary, duration_ms) VALUES (:t, :c, :m, 1, "
                    "'query_catalog', '{}', 'ok', 'x', 1)"
                ),
                {**t, "c": conversation, "m": message},
            )
            await conn.execute(
                text(
                    "INSERT INTO webhook_endpoints (tenant_id, url, secret, enabled) "
                    "VALUES (:t, 'https://hooks.example', 's', true)"
                ),
                t,
            )
            await conn.execute(
                text(
                    "INSERT INTO webhook_deliveries (tenant_id, event_type, payload, url) "
                    "VALUES (:t, 'lead.created', '{}', 'https://hooks.example')"
                ),
                t,
            )
    return tenants


async def test_every_new_table_is_isolated_at_the_database(app_engine, two_tenants_with_rows):
    a, _ = two_tenants_with_rows
    async with app_engine.begin() as conn:
        for table in NEW_TABLES:
            assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(a.id)}
        )
        for table in NEW_TABLES:
            tenants = (await conn.scalars(text(f"SELECT DISTINCT tenant_id FROM {table}"))).all()
            assert tenants == [a.id], table


@pytest.mark.parametrize(
    "path",
    ["/v1/reservations", "/v1/leads", "/v1/orders", "/v1/catalog", "/v1/webhooks/deliveries"],
)
async def test_admin_lists_show_only_the_tenants_own_rows(client, two_tenants_with_rows, path):
    a, b = two_tenants_with_rows
    rows_a = (await client.get(path, headers=bearer(a.admin_key))).json()
    rows_b = (await client.get(path, headers=bearer(b.admin_key))).json()
    assert len(rows_a) == len(rows_b) == 1
    assert rows_a[0]["id"] != rows_b[0]["id"]
    assert (await client.get(path, headers=bearer(a.widget_key))).status_code == 403


async def test_reservation_status_updates_and_isolation(client, two_tenants_with_rows) -> None:
    a, b = two_tenants_with_rows
    reservation = (await client.get("/v1/reservations", headers=bearer(a.admin_key))).json()[0]
    updated = await client.patch(
        f"/v1/reservations/{reservation['id']}",
        json={"status": "cancelled"},
        headers=bearer(a.admin_key),
    )
    assert updated.status_code == 200 and updated.json()["status"] == "cancelled"
    other = await client.patch(
        f"/v1/reservations/{reservation['id']}",
        json={"status": "completed"},
        headers=bearer(b.admin_key),
    )
    assert other.status_code == 404
    bad = await client.patch(
        f"/v1/reservations/{reservation['id']}",
        json={"status": "eaten"},
        headers=bearer(a.admin_key),
    )
    assert bad.status_code == 422


def test_n8n_workflow_is_valid_export_json() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "integrations" / "n8n" / "business-agent-events.json"
    )
    workflow = json.loads(path.read_text())
    assert {"name", "nodes", "connections", "settings"} <= set(workflow)
    names = {node["name"] for node in workflow["nodes"]}
    for node in workflow["nodes"]:
        assert {"parameters", "id", "name", "type", "typeVersion", "position"} <= set(node)
        assert node["type"].startswith("n8n-nodes-base.")
    for source, outputs in workflow["connections"].items():
        assert source in names
        for branch in outputs["main"]:
            assert all(link["node"] in names for link in branch)
    types = {node["type"] for node in workflow["nodes"]}
    assert {
        "n8n-nodes-base.webhook",
        "n8n-nodes-base.code",
        "n8n-nodes-base.googleSheets",
        "n8n-nodes-base.emailSend",
    } <= types
    webhook = next(n for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert webhook["parameters"]["options"]["rawBody"] is True
    code = next(n for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.code")
    js = code["parameters"]["jsCode"]
    for expected in (
        "createHmac('sha256'",
        "x-bap-timestamp",
        "x-bap-signature",
        "> 300",
        "timingSafeEqual",
        "`${timestamp}.${raw}`",
    ):
        assert expected in js
