"""The embeddable widget: its bundle (/widget.js) and its per-tenant config."""

import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth import AuthContext, authenticate
from app.chat.origins import check_origin, preflight
from app.chat.settings import TenantChatSettings
from app.config import get_settings

router = APIRouter(tags=["widget"])
config_router = APIRouter(prefix="/widget", tags=["widget"])

AnyKeyAuth = Annotated[AuthContext, Depends(authenticate)]


class WidgetConfig(BaseModel):
    assistant_name: str
    business_name: str
    greeting: str
    accent_color: str
    suggested_questions: list[str]


@config_router.options("/config", include_in_schema=False)
async def config_preflight(request: Request) -> Response:
    return preflight(request, "GET, OPTIONS")


@config_router.get("/config", response_model=WidgetConfig)
async def widget_config(request: Request, auth: AnyKeyAuth) -> WidgetConfig:
    """Appearance and greeting for the tenant that owns the key (same origin rules as chat)."""
    tenant = await auth.db.tenant()
    settings = TenantChatSettings.from_tenant(tenant.name, tenant.settings)
    check_origin(request, auth, settings)
    return WidgetConfig(
        assistant_name=settings.assistant_name,
        business_name=settings.business_name,
        greeting=settings.greeting,
        accent_color=settings.accent_color,
        suggested_questions=settings.suggested_questions,
    )


_bundle_cache: dict[str, tuple[float, bytes, str]] = {}


def _load_bundle() -> tuple[bytes, str]:
    path = Path(get_settings().widget_dist_dir) / "widget.js"
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="widget.js has not been built (run `npm run build` in web/widget)",
        ) from None
    cached = _bundle_cache.get(str(path))
    if cached is None or cached[0] != mtime:
        data = path.read_bytes()
        cached = (mtime, data, f'"{hashlib.sha256(data).hexdigest()[:20]}"')
        _bundle_cache[str(path)] = cached
    return cached[1], cached[2]


@router.get("/widget.js", include_in_schema=False)
async def widget_bundle(request: Request) -> Response:
    data, etag = _load_bundle()
    headers = {
        "ETag": etag,
        # The URL is not versioned, so caches revalidate after 5 minutes (cheap: ETag -> 304)
        # and may serve a stale copy for a day while they do.
        "Cache-Control": "public, max-age=300, stale-while-revalidate=86400",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=data, media_type="application/javascript; charset=utf-8", headers=headers
    )
