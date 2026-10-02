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


async def test_seed_demo_is_idempotent(
    capsys: pytest.CaptureFixture[str], owner_engine: AsyncEngine, client: AsyncClient
) -> None:
    async def snapshot() -> dict[str, tuple[int, int]]:
        async with owner_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT t.slug, "
                    "  (SELECT count(*) FROM api_keys k WHERE k.tenant_id = t.id), "
                    "  (SELECT count(*) FROM documents d WHERE d.tenant_id = t.id) "
                    "FROM tenants t"
                )
            )
            return {slug: (keys, docs) for slug, keys, docs in rows}

    assert await cli.run(["seed-demo"]) == 0
    first_out = capsys.readouterr().out
    first = await snapshot()
    assert first == {"demo-restaurant": (2, 3), "demo-shop": (2, 2)}

    assert await cli.run(["seed-demo"]) == 0
    second_out = capsys.readouterr().out
    assert await snapshot() == first
    assert second_out.count("already exists") == 2
    assert "bap_" not in second_out

    # The keys printed on the first run work, and each sees only its own documents.
    admin_keys = re.findall(r"admin_key:\s+(\S+)", first_out)
    assert len(admin_keys) == 2
    titles = []
    for key in admin_keys:
        docs = (await client.get("/v1/documents", headers=bearer(key))).json()
        titles.append(sorted(d["title"] for d in docs))
    assert titles == [["Allergens", "Menu", "Opening hours"], ["Returns policy", "Shipping FAQ"]]


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
