"""Bearer API-key authentication.

A request's key is resolved to (tenant, kind) via the resolve_api_key() SQL function, and the
tenant is bound to the request's transaction with set_config('app.current_tenant', ..., true)
so Row-Level Security applies to every query that follows.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from app.db import get_sessionmaker
from app.security import hash_api_key, is_well_formed_api_key
from app.tenancy import TenantDB, bind_tenant

bearer_scheme = HTTPBearer(auto_error=False, description="API key: bap_admin_… or bap_widget_…")


@dataclass(frozen=True)
class AuthContext:
    tenant_id: uuid.UUID
    api_key_id: uuid.UUID
    kind: str
    db: TenantDB


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AsyncIterator[AuthContext]:
    """Any valid, unrevoked key (admin or widget). 401 otherwise."""
    if credentials is None or not is_well_formed_api_key(credentials.credentials):
        raise _unauthorized()

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("SELECT api_key_id, tenant_id, kind FROM resolve_api_key(:key_hash)"),
                {"key_hash": hash_api_key(credentials.credentials)},
            )
        ).one_or_none()
        if row is None:
            raise _unauthorized()
        await bind_tenant(session, row.tenant_id)
        yield AuthContext(
            tenant_id=row.tenant_id,
            api_key_id=row.api_key_id,
            kind=row.kind,
            db=TenantDB(session, row.tenant_id),
        )


async def require_admin(auth: Annotated[AuthContext, Depends(authenticate)]) -> AuthContext:
    """Admin routes: widget keys are valid but not allowed here (403)."""
    if auth.kind != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This route requires an admin API key"
        )
    return auth


AdminAuth = Annotated[AuthContext, Depends(require_admin)]
