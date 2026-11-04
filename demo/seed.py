"""Create (or update) the demo video's tenant, Saffron & Sage Bistro, and ingest demo/tenant/.

Run it inside Compose, so files land in the same upload volume the worker uses:

    docker compose run --rm -v "$PWD/demo/site:/demo-site" migrate \
        python /demo/seed.py --site-config /demo-site/config.local.js

Safe to rerun:
- The tenant is created once; later runs update its settings.
- Unchanged documents are skipped.
- The demo-site widget key is rotated: the old one is revoked and a new one printed and written
  to the git-ignored site config.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (Path.cwd(), HERE.parent / "backend"):  # /app in the image, backend/ locally
    if (candidate / "app").is_dir():
        sys.path.insert(0, str(candidate))
        break

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.cli import owner_session  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.embeddings import get_embedding_provider  # noqa: E402
from app.ingestion.documents import create_upload_document  # noqa: E402
from app.ingestion.pipeline import IngestDeps, process_document  # noqa: E402
from app.ingestion.storage import get_storage  # noqa: E402
from app.models import ApiKey, Document, Tenant  # noqa: E402
from app.security import new_api_key  # noqa: E402
from app.tenancy import tenant_db  # noqa: E402
from app.tenants import create_tenant  # noqa: E402

SLUG = "saffron-sage"
NAME = "Saffron & Sage Bistro"
SITE_ORIGIN = os.environ.get("DEMO_SITE_ORIGIN", "http://localhost:8095")
WIDGET_KEY_LABEL = "Demo video site"
DOCUMENTS = {
    "menu.md": "Saffron & Sage menu",
    "faq.md": "Saffron & Sage FAQ: hours, parking, events, allergies",
    "policies.md": "Saffron & Sage reservation and dining policies",
}
SETTINGS = {
    "assistant_name": "Sage",
    "business_name": NAME,
    "tone": "warm, upbeat and concise, like a great host at a neighbourhood bistro",
    "fallback_contact": "call us at (512) 555-0142 (11 am to 9 pm, Tuesday to Sunday)",
    "allowed_origins": [SITE_ORIGIN],
    "daily_message_cap": 500,
    "greeting": "Hi! I'm Sage. Ask me about our menu, hours or booking a table.",
    "accent_color": "#5B7553",
    "suggested_questions": [
        "Do you have vegan options?",
        "Book a table for Friday",
        "Is there parking?",
    ],
    "timezone": "America/Chicago",
    "currency": "USD",
    "enabled_tools": [
        "search_knowledge",
        "create_reservation",
        "capture_lead",
        "request_human",
    ],
    "opening_hours": {
        "tue": [["11:30", "21:30"]],
        "wed": [["11:30", "21:30"]],
        "thu": [["11:30", "21:30"]],
        "fri": [["11:30", "22:30"]],
        "sat": [["10:00", "22:30"]],
        "sun": [["10:00", "21:00"]],
    },
    "max_online_party_size": 8,
    "follow_up_promise": "within a few hours",
}


async def ensure_tenant() -> tuple[object, str | None]:
    """The tenant id, and its first admin key if it was just created (shown once)."""
    async with owner_session() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == SLUG))
        if tenant is not None:
            if tenant.settings != SETTINGS:
                tenant.settings = SETTINGS
                await session.commit()
                print(f"{SLUG}: settings updated")
            else:
                print(f"{SLUG}: tenant already exists")
            return tenant.id, None
        tenant, admin_key = await create_tenant(session, name=NAME, slug=SLUG)
        tenant.settings = SETTINGS
        await session.commit()
        print(f"{SLUG}: created")
        return tenant.id, admin_key.full_key


async def ingest(tenant_id) -> int:
    embedder = get_embedding_provider()
    engine = create_async_engine(get_settings().database_url)  # the application role
    deps = IngestDeps(
        sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
        embedder=embedder,
        storage=get_storage(),
    )
    failures = 0
    print(f"Ingesting demo/tenant (embeddings: {embedder.model_name})")
    try:
        for name, title in DOCUMENTS.items():
            data = await asyncio.to_thread((HERE / "tenant" / name).read_bytes)
            async with tenant_db(deps.sessionmaker, tenant_id) as db:
                document, created = await create_upload_document(
                    db, deps.storage, name, data, title
                )
                document_id, status = document.id, document.status
            started = time.perf_counter()
            if created or status != "ready":
                status = await process_document(deps, tenant_id, document_id)
            else:
                status = "unchanged"
            async with tenant_db(deps.sessionmaker, tenant_id) as db:
                document = await db.get(Document, document_id)
            detail = f": {document.error}" if document.status == "failed" else ""
            took = time.perf_counter() - started
            print(f"  {name}: {status}, {document.chunk_count} chunks ({took:.1f}s){detail}")
            failures += document.status == "failed"
    finally:
        await embedder.aclose()
        await engine.dispose()
    return failures


async def rotate_widget_key(tenant_id) -> str:
    async with owner_session() as session:
        await session.execute(
            update(ApiKey)
            .where(
                ApiKey.tenant_id == tenant_id,
                ApiKey.label == WIDGET_KEY_LABEL,
                ApiKey.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )
        created = new_api_key(tenant_id, "widget", label=WIDGET_KEY_LABEL)
        session.add(created.record)
        await session.commit()
    return created.full_key


async def main(args: argparse.Namespace) -> int:
    tenant_id, admin_key = await ensure_tenant()
    if admin_key:
        print(f"  admin_key (shown once; for the dashboard): {admin_key}")
    failures = await ingest(tenant_id)
    widget_key = await rotate_widget_key(tenant_id)
    print(f"widget_key: {widget_key}")
    if args.site_config:
        config = {"api": args.api, "key": widget_key}
        content = (
            "// Generated by demo/seed.py. Git-ignored: do not commit.\n"
            f"window.SAFFRON_DEMO = {json.dumps(config, indent=2)};\n"
        )
        await asyncio.to_thread(Path(args.site_config).write_text, content)
        print(f"Site config written to {args.site_config}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", help="write the widget key here for demo/site")
    parser.add_argument("--api", default="http://localhost:8000", help="API base for the widget")
    sys.exit(asyncio.run(main(parser.parse_args())))
