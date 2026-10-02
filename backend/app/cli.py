"""Platform administration CLI.

Tenant and key management connects as the database owner role (OWNER_DATABASE_URL). Document
ingestion (seed-demo) connects as the application role (DATABASE_URL) with the tenant bound, so
Row-Level Security applies to it just as it does to the API and the worker.

Usage: python -m app.cli <command> [options]
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db_roles import ensure_app_role
from app.embeddings import get_embedding_provider
from app.ingestion.documents import UPLOAD_TYPES, UploadRejected, create_upload_document
from app.ingestion.pipeline import IngestDeps, process_document
from app.ingestion.storage import get_storage
from app.models import Document, Tenant
from app.security import new_api_key
from app.tenancy import tenant_db
from app.tenants import TenantExistsError, create_tenant

# demo/ at the repo root locally; mounted at /demo in Docker Compose (/app/app/cli.py -> /demo).
DEFAULT_DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"

DEMO_TENANTS = [
    {
        "name": "Nodi Kitchen",
        "slug": "demo-restaurant",
        "dir": "restaurant",
        "titles": {
            "menu.csv": "Nodi Kitchen menu",
            "about.md": "Nodi Kitchen: hours, location, reservations and FAQ",
        },
    },
    {
        "name": "Jamdani Lane",
        "slug": "demo-shop",
        "dir": "shop",
        "titles": {
            "products.csv": "Jamdani Lane product catalogue",
            "shipping.md": "Shipping",
            "returns.md": "Returns and refunds",
            "faq.md": "Frequently asked questions",
        },
    },
]


@asynccontextmanager
async def owner_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().owner_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def cmd_ensure_app_role(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.owner_database_url)
    try:
        async with engine.begin() as conn:
            await ensure_app_role(conn, settings.database_url)
    finally:
        await engine.dispose()
    print("Application database role is ready.")
    return 0


async def cmd_create_tenant(args: argparse.Namespace) -> int:
    async with owner_session() as session:
        try:
            tenant, admin_key = await create_tenant(session, name=args.name, slug=args.slug)
        except (TenantExistsError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        await session.commit()
    print(f"tenant_id: {tenant.id}")
    print(f"admin_key: {admin_key.full_key}")
    print("The admin key is shown only once. Store it now.")
    return 0


async def cmd_create_key(args: argparse.Namespace) -> int:
    async with owner_session() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == args.tenant))
        if tenant is None:
            print(f"error: no tenant with slug {args.tenant!r}", file=sys.stderr)
            return 1
        created = new_api_key(tenant.id, args.kind, label=args.label)
        session.add(created.record)
        await session.commit()
    print(f"key_id: {created.record.id}")
    print(f"{args.kind}_key: {created.full_key}")
    print("The key is shown only once. Store it now.")
    return 0


async def _ensure_demo_tenants() -> dict[str, uuid.UUID]:
    ids = {}
    async with owner_session() as session:
        for demo in DEMO_TENANTS:
            existing = await session.scalar(select(Tenant).where(Tenant.slug == demo["slug"]))
            if existing is not None:
                print(f"{demo['slug']}: tenant already exists ({existing.id})")
                ids[demo["slug"]] = existing.id
                continue
            tenant, admin_key = await create_tenant(session, name=demo["name"], slug=demo["slug"])
            widget_key = new_api_key(tenant.id, "widget", label="Demo website widget")
            session.add(widget_key.record)
            await session.commit()
            ids[demo["slug"]] = tenant.id
            print(f"{demo['slug']}: created {tenant.id}")
            print(f"  admin_key:  {admin_key.full_key}")
            print(f"  widget_key: {widget_key.full_key}")
    return ids


def _read_demo_files(demo_dir: Path) -> dict[str, list[tuple[str, bytes]]]:
    """{tenant slug: [(filename, contents)]} for the supported files in each demo folder."""
    return {
        demo["slug"]: [
            (path.name, path.read_bytes())
            for path in sorted((demo_dir / demo["dir"]).iterdir())
            if path.suffix.lower() in UPLOAD_TYPES
        ]
        for demo in DEMO_TENANTS
    }


async def cmd_seed_demo(args: argparse.Namespace) -> int:
    try:
        demo_files = await asyncio.to_thread(_read_demo_files, Path(args.demo_dir))
    except FileNotFoundError as exc:
        print(f"error: demo files not found: {exc.filename}", file=sys.stderr)
        return 1

    tenant_ids = await _ensure_demo_tenants()
    print("Keys are shown only when a tenant is first created. Store them now.")

    embedder = get_embedding_provider()
    engine = create_async_engine(get_settings().database_url)  # the application role
    deps = IngestDeps(
        sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
        embedder=embedder,
        storage=get_storage(),
    )
    print(f"Ingesting demo documents (embeddings: {embedder.model_name})")
    failures = 0
    try:
        for demo in DEMO_TENANTS:
            tenant_id = tenant_ids[demo["slug"]]
            started = time.perf_counter()
            total_chunks = 0
            print(f"{demo['slug']}:")
            for name, data in demo_files[demo["slug"]]:
                async with tenant_db(deps.sessionmaker, tenant_id) as db:
                    try:
                        document, created = await create_upload_document(
                            db, deps.storage, name, data, demo["titles"].get(name)
                        )
                    except UploadRejected as exc:
                        print(f"  {name}: rejected ({exc.message})")
                        failures += 1
                        continue
                    document_id, status = document.id, document.status
                if created or status != "ready":
                    status = await process_document(deps, tenant_id, document_id)
                else:
                    status = "unchanged"
                async with tenant_db(deps.sessionmaker, tenant_id) as db:
                    document = await db.get(Document, document_id)
                total_chunks += document.chunk_count
                detail = f": {document.error}" if document.status == "failed" else ""
                print(f"  {name}: {status}, {document.chunk_count} chunks{detail}")
                failures += document.status == "failed"
            print(f"  total {total_chunks} chunks in {time.perf_counter() - started:.1f}s")
    finally:
        await embedder.aclose()
        await engine.dispose()
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    ensure = commands.add_parser(
        "ensure-app-role", help="create/update the non-superuser role the API connects as"
    )
    ensure.set_defaults(handler=cmd_ensure_app_role)

    create = commands.add_parser(
        "create-tenant", help="create a tenant and print its id and first admin key"
    )
    create.add_argument("--name", required=True, help='display name, e.g. "Rosa\'s Pizzeria"')
    create.add_argument("--slug", required=True, help="unique URL-safe id, e.g. rosas-pizzeria")
    create.set_defaults(handler=cmd_create_tenant)

    key = commands.add_parser("create-key", help="create an API key for a tenant (shown once)")
    key.add_argument("--tenant", required=True, help="tenant slug")
    key.add_argument("--kind", required=True, choices=["admin", "widget"])
    key.add_argument("--label", default="Created with the CLI", help="label shown in key lists")
    key.set_defaults(handler=cmd_create_key)

    seed = commands.add_parser(
        "seed-demo",
        help="create demo-restaurant and demo-shop and ingest demo/ (safe to run repeatedly)",
    )
    seed.add_argument("--demo-dir", default=str(DEFAULT_DEMO_DIR), help="default: %(default)s")
    seed.set_defaults(handler=cmd_seed_demo)
    return parser


async def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return await args.handler(args)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
