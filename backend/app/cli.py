"""Platform administration CLI. Connects as the database owner role.

Usage: python -m app.cli <command> [options]
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db_roles import ensure_app_role


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    ensure = commands.add_parser(
        "ensure-app-role", help="create/update the non-superuser role the API connects as"
    )
    ensure.set_defaults(handler=cmd_ensure_app_role)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
