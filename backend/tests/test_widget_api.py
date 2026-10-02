"""GET /v1/widget/config and GET /widget.js."""

import os

import pytest
from httpx import AsyncClient

from app.config import get_settings
from tests.chat_helpers import ORIGIN, configure_tenant
from tests.conftest import bearer


@pytest.fixture
async def shops(make_tenant, owner_engine):
    a = await make_tenant("widget-a")
    b = await make_tenant("widget-b")
    await configure_tenant(
        owner_engine,
        a.id,
        assistant_name="Nodi",
        business_name="Nodi Kitchen",
        greeting="Assalamu alaikum!",
        accent_color="#B5432F",
        suggested_questions=["What's on the menu?", "শুক্রবার কখন খোলেন?"],
    )
    await configure_tenant(
        owner_engine,
        b.id,
        assistant_name="Mithila",
        business_name="Jamdani Lane",
        accent_color="#F2C14E",
        allowed_origins=["https://shop.example"],
    )
    return a, b


async def get_config(client: AsyncClient, key: str, origin: str | None = ORIGIN):
    headers = bearer(key) | ({"Origin": origin} if origin else {})
    return await client.get("/v1/widget/config", headers=headers)


async def test_config_returns_the_tenants_widget_settings(client, shops) -> None:
    a, _ = shops
    response = await get_config(client, a.widget_key)
    assert response.status_code == 200
    assert response.json() == {
        "assistant_name": "Nodi",
        "business_name": "Nodi Kitchen",
        "greeting": "Assalamu alaikum!",
        "accent_color": "#B5432F",
        "suggested_questions": ["What's on the menu?", "শুক্রবার কখন খোলেন?"],
    }
    assert response.headers["access-control-allow-origin"] == ORIGIN


async def test_config_never_exposes_another_tenants_settings(client, shops) -> None:
    _, b = shops
    body_b = (await get_config(client, b.widget_key, "https://shop.example")).json()
    assert body_b["assistant_name"] == "Mithila" and body_b["accent_color"] == "#F2C14E"
    assert body_b["suggested_questions"] == []
    assert body_b["greeting"] == "Hi! How can I help you today?"  # default
    # B's key from A's website is refused, and A's settings are never returned for B's key.
    refused = await get_config(client, b.widget_key, ORIGIN)
    assert refused.status_code == 403
    assert "Nodi" not in refused.text


@pytest.mark.parametrize("origin", ["https://evil.example", None])
async def test_config_refuses_disallowed_origins(client, shops, origin) -> None:
    a, _ = shops
    response = await get_config(client, a.widget_key, origin)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


async def test_config_requires_a_valid_key(client, shops) -> None:
    response = await client.get("/v1/widget/config", headers={"Origin": ORIGIN})
    assert response.status_code == 401


async def test_config_preflight(client) -> None:
    response = await client.options(
        "/v1/widget/config", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
    )
    assert response.status_code == 204
    assert "GET" in response.headers["access-control-allow-methods"]


async def test_invalid_accent_colour_falls_back_safely(client, make_tenant, owner_engine) -> None:
    tenant = await make_tenant("bad-colour")
    await configure_tenant(
        owner_engine, tenant.id, accent_color="red; background:url(x)", greeting="Hello!"
    )
    response = await get_config(client, tenant.widget_key)
    assert response.status_code == 200
    assert response.json()["accent_color"] == "#C5EE4F"  # invalid value replaced by default
    assert response.json()["greeting"] == "Hello!"  # valid fields are kept


async def test_widget_bundle_is_served_with_cache_headers(client, tmp_path, monkeypatch) -> None:
    (tmp_path / "widget.js").write_text("console.log('widget');")
    monkeypatch.setattr(get_settings(), "widget_dist_dir", str(tmp_path))
    response = await client.get("/widget.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.text == "console.log('widget');"
    assert "max-age=300" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"
    etag = response.headers["etag"]
    revalidated = await client.get("/widget.js", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304 and revalidated.content == b""

    (tmp_path / "widget.js").write_text("console.log('v2');")
    os.utime(tmp_path / "widget.js", (1, 1))  # a new mtime invalidates the cached bundle
    changed = await client.get("/widget.js", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.text == "console.log('v2');"


async def test_missing_bundle_returns_404_with_a_hint(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "widget_dist_dir", str(tmp_path / "nothing"))
    response = await client.get("/widget.js")
    assert response.status_code == 404
    assert "npm run build" in response.json()["detail"]
