"""API-key authentication: 401 for bad keys, 403 for widget keys on admin routes."""

import hashlib

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.security import is_well_formed_api_key, new_api_key
from tests.conftest import bearer

ADMIN_ROUTES = [
    ("GET", "/v1/tenant"),
    ("GET", "/v1/api-keys"),
    ("POST", "/v1/api-keys"),
    ("DELETE", "/v1/api-keys/00000000-0000-0000-0000-000000000000"),
    ("GET", "/v1/documents"),
]


async def test_valid_admin_key_works(client: AsyncClient, make_tenant) -> None:
    tenant = await make_tenant("acme")
    response = await client.get("/v1/tenant", headers=bearer(tenant.admin_key))
    assert response.status_code == 200
    assert response.json() == {
        "id": str(tenant.id),
        "name": "Acme",
        "slug": "acme",
        "settings": {},
    }


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_missing_key_returns_401(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Token bap_admin_" + "x" * 43,
        "Bearer not-a-key",
        "Bearer bap_admin_tooshort",
        "Bearer bap_root_" + "x" * 43,
        "Bearer bap_admin_" + "x" * 42 + "!",
    ],
)
async def test_malformed_authorization_returns_401(
    client: AsyncClient, make_tenant, authorization: str
) -> None:
    await make_tenant("acme")
    response = await client.get("/v1/tenant", headers={"Authorization": authorization})
    assert response.status_code == 401


async def test_unknown_key_returns_401(client: AsyncClient, make_tenant) -> None:
    await make_tenant("acme")
    unknown = new_api_key(tenant_id=None, kind="admin").full_key  # well formed, never stored
    assert is_well_formed_api_key(unknown)
    response = await client.get("/v1/tenant", headers=bearer(unknown))
    assert response.status_code == 401


async def test_revoked_key_returns_401(client: AsyncClient, make_tenant) -> None:
    tenant = await make_tenant("acme")
    created = await client.post(
        "/v1/api-keys", json={"kind": "admin", "label": "temp"}, headers=bearer(tenant.admin_key)
    )
    temp_key, temp_id = created.json()["key"], created.json()["id"]
    assert (await client.get("/v1/tenant", headers=bearer(temp_key))).status_code == 200

    revoked = await client.delete(f"/v1/api-keys/{temp_id}", headers=bearer(tenant.admin_key))
    assert revoked.status_code == 204
    assert (await client.get("/v1/tenant", headers=bearer(temp_key))).status_code == 401


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_widget_key_on_admin_route_returns_403(
    client: AsyncClient, make_tenant, method: str, path: str
) -> None:
    tenant = await make_tenant("acme")
    kwargs = {"json": {"kind": "widget"}} if method == "POST" else {}
    response = await client.request(method, path, headers=bearer(tenant.widget_key), **kwargs)
    assert response.status_code == 403


async def test_full_key_is_shown_once_and_never_stored(
    client: AsyncClient, make_tenant, owner_engine: AsyncEngine
) -> None:
    tenant = await make_tenant("acme")
    headers = bearer(tenant.admin_key)

    created = await client.post(
        "/v1/api-keys", json={"kind": "widget", "label": "site"}, headers=headers
    )
    assert created.status_code == 201
    body = created.json()
    full_key = body["key"]
    assert full_key.startswith("bap_widget_")
    assert is_well_formed_api_key(full_key)
    assert body["prefix"] == full_key.removeprefix("bap_widget_")[:8]
    assert set(body) == {"id", "kind", "prefix", "label", "created_at", "revoked_at", "key"}

    listed = await client.get("/v1/api-keys", headers=headers)
    assert listed.status_code == 200
    for item in listed.json():
        assert set(item) == {"id", "kind", "prefix", "label", "created_at", "revoked_at"}
    random_part = full_key.removeprefix("bap_widget_")
    for secret in (full_key, tenant.admin_key, tenant.widget_key):
        assert secret.split("_", 2)[2] not in listed.text

    async with owner_engine.connect() as conn:
        rows = (await conn.scalars(text("SELECT row_to_json(k)::text FROM api_keys k"))).all()
        stored_hash = await conn.scalar(
            text("SELECT key_hash FROM api_keys WHERE id = :id"), {"id": body["id"]}
        )
    assert len(rows) == 3
    for row in rows:
        assert random_part not in row
        assert tenant.admin_key.split("_", 2)[2] not in row
    assert stored_hash == hashlib.sha256(full_key.encode()).hexdigest()


async def test_create_key_rejects_unknown_kind(client: AsyncClient, make_tenant) -> None:
    tenant = await make_tenant("acme")
    response = await client.post(
        "/v1/api-keys", json={"kind": "root"}, headers=bearer(tenant.admin_key)
    )
    assert response.status_code == 422


async def test_cannot_revoke_last_active_admin_key(client: AsyncClient, make_tenant) -> None:
    tenant = await make_tenant("acme")
    headers = bearer(tenant.admin_key)
    keys = (await client.get("/v1/api-keys", headers=headers)).json()
    admin_id = next(k["id"] for k in keys if k["kind"] == "admin")

    response = await client.delete(f"/v1/api-keys/{admin_id}", headers=headers)
    assert response.status_code == 409
    assert (await client.get("/v1/tenant", headers=headers)).status_code == 200


async def test_revoke_is_idempotent(client: AsyncClient, make_tenant) -> None:
    tenant = await make_tenant("acme")
    headers = bearer(tenant.admin_key)
    created = (await client.post("/v1/api-keys", json={"kind": "widget"}, headers=headers)).json()
    for _ in range(2):
        response = await client.delete(f"/v1/api-keys/{created['id']}", headers=headers)
        assert response.status_code == 204
    listed = (await client.get("/v1/api-keys", headers=headers)).json()
    assert next(k for k in listed if k["id"] == created["id"])["revoked_at"] is not None
