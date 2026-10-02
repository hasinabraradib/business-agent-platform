"""Tenant provisioning. Platform-level: runs with the owner role, not through the API."""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tenant
from app.security import NewApiKey, new_api_key

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TenantExistsError(Exception):
    pass


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.fullmatch(slug) or len(slug) > 63:
        raise ValueError(
            f"Invalid slug {slug!r}: use lowercase letters, digits and single hyphens (max 63)"
        )
    return slug


async def create_tenant(session: AsyncSession, name: str, slug: str) -> tuple[Tenant, NewApiKey]:
    """Create a tenant and its first admin key. The caller commits."""
    validate_slug(slug)
    if not name.strip():
        raise ValueError("Tenant name must not be empty")
    if await session.scalar(select(Tenant.id).where(Tenant.slug == slug)) is not None:
        raise TenantExistsError(f"A tenant with slug {slug!r} already exists")

    tenant = Tenant(name=name.strip(), slug=slug)
    session.add(tenant)
    await session.flush()
    admin_key = new_api_key(tenant.id, "admin", label="Initial admin key")
    session.add(admin_key.record)
    await session.flush()
    return tenant, admin_key
