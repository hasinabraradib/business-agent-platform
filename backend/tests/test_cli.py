import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import cli
from tests.conftest import bearer

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _field(output: str, name: str) -> str:
    match = re.search(rf"^\s*{name}:\s+(\S+)$", output, re.MULTILINE)
    assert match, f"{name} not found in output:\n{output}"
    return match.group(1)


async def test_create_tenant_prints_id_and_working_admin_key(
    capsys: pytest.CaptureFixture[str], client: AsyncClient
) -> None:
    exit_code = await cli.run(["create-tenant", "--name", "Rosa's Pizzeria", "--slug", "rosas"])
    assert exit_code == 0
    out = capsys.readouterr().out
    tenant_id, admin_key = _field(out, "tenant_id"), _field(out, "admin_key")

    response = await client.get("/v1/tenant", headers=bearer(admin_key))
    assert response.status_code == 200
    assert response.json()["id"] == tenant_id
    assert response.json()["name"] == "Rosa's Pizzeria"


@pytest.mark.parametrize("slug", ["Bad Slug", "UPPER", "-leading", "double--hyphen", ""])
async def test_create_tenant_rejects_invalid_slug(
    capsys: pytest.CaptureFixture[str], slug: str
) -> None:
    assert await cli.run(["create-tenant", "--name", "X", f"--slug={slug}"]) == 1
    assert "Invalid slug" in capsys.readouterr().err


async def test_create_tenant_rejects_duplicate_slug(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli.run(["create-tenant", "--name", "One", "--slug", "dupe"]) == 0
    assert await cli.run(["create-tenant", "--name", "Two", "--slug", "dupe"]) == 1
    assert "already exists" in capsys.readouterr().err


async def test_seed_demo_ingests_demo_knowledge_and_is_idempotent(
    capsys: pytest.CaptureFixture[str], owner_engine: AsyncEngine, client: AsyncClient
) -> None:
    async def snapshot() -> dict:
        async with owner_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT t.slug, "
                    "  (SELECT count(*) FROM api_keys k WHERE k.tenant_id = t.id), "
                    "  (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id), "
                    "  (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id "
                    "     AND d.status = 'ready'), "
                    "  (SELECT array_agg(c.id ORDER BY c.id) FROM chunks c "
                    "     WHERE c.tenant_id = t.id) "
                    "FROM tenants t"
                )
            )
            return {slug: (keys, docs, ready, chunks) for slug, keys, docs, ready, chunks in rows}

    assert await cli.run(["seed-demo"]) == 0
    first_out = capsys.readouterr().out
    first = await snapshot()
    assert set(first) == {"demo-restaurant", "demo-shop"}
    assert first["demo-restaurant"][:3] == (2, 2, 2)  # admin + widget key; menu + about
    assert first["demo-shop"][:3] == (2, 4, 4)  # products, shipping, returns, faq
    assert len(first["demo-restaurant"][3]) > 18  # 18 menu rows plus the about page
    assert len(first["demo-shop"][3]) > 14  # 14 products plus three pages
    assert "embeddings: fake-hashing-768" in first_out

    assert await cli.run(["seed-demo"]) == 0
    second_out = capsys.readouterr().out
    assert await snapshot() == first  # same tenants, keys, documents and the very same chunks
    assert second_out.count("tenant already exists") == 2
    assert "bap_" not in second_out
    assert second_out.count(": unchanged,") == 6

    # The printed keys work, and each tenant sees only its own documents.
    admin_keys = re.findall(r"admin_key:\s+(\S+)", first_out)
    assert len(admin_keys) == 2
    titles = []
    for key in admin_keys:
        docs = (await client.get("/v1/documents", headers=bearer(key))).json()
        titles.append(sorted(d["title"] for d in docs))
    assert titles == [
        ["Nodi Kitchen menu", "Nodi Kitchen: hours, location, reservations and FAQ"],
        [
            "Frequently asked questions",
            "Jamdani Lane product catalogue",
            "Returns and refunds",
            "Shipping",
        ],
    ]


async def test_seed_demo_menu_rows_keep_bengali_names(
    capsys: pytest.CaptureFixture[str], owner_engine: AsyncEngine
) -> None:
    assert await cli.run(["seed-demo"]) == 0
    async with owner_engine.connect() as conn:
        content = await conn.scalar(
            text(
                "SELECT content FROM chunks "
                "WHERE content LIKE :pattern AND (metadata->>'row') IS NOT NULL"
            ),
            {"pattern": "dish_en: Kacchi Biryani%"},
        )
    assert "dish_bn: কাচ্চি বিরিয়ানি (খাসি)" in content
    assert "price_bdt: 480" in content


async def test_seed_demo_reports_missing_demo_dir(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    assert await cli.run(["seed-demo", "--demo-dir", str(tmp_path / "nope")]) == 1
    assert "demo files not found" in capsys.readouterr().err


async def test_create_key_prints_a_working_key_once(
    capsys: pytest.CaptureFixture[str], client: AsyncClient, make_tenant, owner_engine
) -> None:
    tenant = await make_tenant("keyed")
    assert await cli.run(["create-key", "--tenant", "keyed", "--kind", "widget"]) == 0
    out = capsys.readouterr().out
    widget_key = _field(out, "widget_key")
    assert widget_key.startswith("bap_widget_")
    # A widget key authenticates (403 on admin routes, not 401).
    response = await client.get("/v1/tenant", headers=bearer(widget_key))
    assert response.status_code == 403

    assert await cli.run(["create-key", "--tenant", "keyed", "--kind", "admin"]) == 0
    admin_key = _field(capsys.readouterr().out, "admin_key")
    assert (await client.get("/v1/tenant", headers=bearer(admin_key))).json()["id"] == str(
        tenant.id
    )
    async with owner_engine.connect() as conn:
        stored = (await conn.scalars(text("SELECT row_to_json(k)::text FROM api_keys k"))).all()
    assert not any(admin_key in row or widget_key in row for row in stored)


async def test_create_key_for_unknown_tenant_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert await cli.run(["create-key", "--tenant", "ghost", "--kind", "admin"]) == 1
    assert "no tenant" in capsys.readouterr().err


def test_create_key_rejects_unknown_kind(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["create-key", "--tenant", "x", "--kind", "root"])


def test_module_entry_point_runs() -> None:
    # Runs the real `python -m app.cli` in a subprocess against the test database.
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "create-tenant", "--name", "Sub", "--slug", "sub-proc"],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert _field(result.stdout, "admin_key").startswith("bap_admin_")
