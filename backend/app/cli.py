"""Platform administration CLI. Connects as the database owner role (OWNER_DATABASE_URL).

Usage: python -m app.cli <command> [options]
"""

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db_roles import ensure_app_role
from app.models import Document, Tenant
from app.security import new_api_key
from app.tenants import TenantExistsError, create_tenant

DEMO_TENANTS = [
    {
        "name": "Demo Restaurant",
        "slug": "demo-restaurant",
        "documents": [("Menu", "upload"), ("Opening hours", "upload"), ("Allergens", "url")],
    },
    {
        "name": "Demo Shop",
        "slug": "demo-shop",
        "documents": [("Returns policy", "upload"), ("Shipping FAQ", "url")],
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


async def cmd_seed_demo(args: argparse.Namespace) -> int:
    async with owner_session() as session:
        for demo in DEMO_TENANTS:
            existing = await session.scalar(select(Tenant).where(Tenant.slug == demo["slug"]))
            if existing is not None:
                print(f"{demo['slug']}: already exists ({existing.id}), skipped")
                continue
            tenant, admin_key = await create_tenant(session, name=demo["name"], slug=demo["slug"])
            widget_key = new_api_key(tenant.id, "widget", label="Demo website widget")
            session.add(widget_key.record)
            session.add_all(
                Document(tenant_id=tenant.id, title=title, source_type=source_type)
                for title, source_type in demo["documents"]
            )
            await session.commit()
            print(f"{demo['slug']}: created {tenant.id}")
            print(f"  admin_key:  {admin_key.full_key}")
            print(f"  widget_key: {widget_key.full_key}")
    print("Keys are shown only when a tenant is first created. Store them now.")
    return 0


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

    seed = commands.add_parser(
        "seed-demo", help="create demo-restaurant and demo-shop (safe to run repeatedly)"
    )
    seed.set_defaults(handler=cmd_seed_demo)
    return parser


async def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return await args.handler(args)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
