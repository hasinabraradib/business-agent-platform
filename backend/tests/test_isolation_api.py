"""Application-layer tenant isolation, exercised through the HTTP API."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Tenant
from app.tenancy import TenantDB
from tests.conftest import bearer


@pytest.fixture
async def two_tenants(make_tenant):
    a = await make_tenant("restaurant-a", documents=("A menu", "A opening hours"))
    b = await make_tenant("shop-b", documents=("B returns policy",))
    return a, b


async def test_documents_list_only_own_tenant(client: AsyncClient, two_tenants) -> None:
    a, b = two_tenants
    docs_a = (await client.get("/v1/documents", headers=bearer(a.admin_key))).json()
    docs_b = (await client.get("/v1/documents", headers=bearer(b.admin_key))).json()
    assert sorted(d["title"] for d in docs_a) == ["A menu", "A opening hours"]
    assert [d["title"] for d in docs_b] == ["B returns policy"]
    assert all(d["status"] == "pending" for d in docs_a + docs_b)


async def test_tenant_endpoint_returns_own_tenant(client: AsyncClient, two_tenants) -> None:
    a, b = two_tenants
    assert (await client.get("/v1/tenant", headers=bearer(a.admin_key))).json()["slug"] == a.slug
    assert (await client.get("/v1/tenant", headers=bearer(b.admin_key))).json()["slug"] == b.slug


async def test_api_keys_list_only_own_tenant(client: AsyncClient, two_tenants) -> None:
    a, b = two_tenants
    keys_a = (await client.get("/v1/api-keys", headers=bearer(a.admin_key))).json()
    keys_b = (await client.get("/v1/api-keys", headers=bearer(b.admin_key))).json()
    assert len(keys_a) == len(keys_b) == 2
    assert not {k["id"] for k in keys_a} & {k["id"] for k in keys_b}


async def test_cannot_revoke_another_tenants_key(client: AsyncClient, two_tenants) -> None:
    a, b = two_tenants
    keys_b = (await client.get("/v1/api-keys", headers=bearer(b.admin_key))).json()
    widget_b = next(k for k in keys_b if k["kind"] == "widget")

    response = await client.delete(f"/v1/api-keys/{widget_b['id']}", headers=bearer(a.admin_key))
    assert response.status_code == 404
    keys_b_after = (await client.get("/v1/api-keys", headers=bearer(b.admin_key))).json()
    assert next(k for k in keys_b_after if k["id"] == widget_b["id"])["revoked_at"] is None


async def test_tenant_db_refuses_non_tenant_models_and_foreign_rows() -> None:
    db = TenantDB(session=AsyncSession(), tenant_id=uuid.uuid4())
    with pytest.raises(TypeError):
        db.select(Tenant)  # type: ignore[type-var]
    with pytest.raises(ValueError):
        db.add(Document(tenant_id=uuid.uuid4(), title="x", source_type="upload"))


async def test_tenant_db_scopes_queries_and_stamps_new_rows() -> None:
    tenant_id = uuid.uuid4()
    db = TenantDB(session=AsyncSession(), tenant_id=tenant_id)
    sql = str(db.select(Document).compile(compile_kwargs={"literal_binds": False}))
    assert "WHERE documents.tenant_id = " in sql
    doc = Document(title="x", source_type="upload")
    db.add(doc)
    assert doc.tenant_id == tenant_id
