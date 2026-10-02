"""Origin checks for endpoints that widget keys may call from a business's website."""

from fastapi import HTTPException, Request, Response, status

from app.auth import AuthContext
from app.chat.settings import TenantChatSettings


def check_origin(request: Request, auth: AuthContext, settings: TenantChatSettings) -> None:
    """Widget keys are public, so they only work from the tenant's allowed origins.

    On success for an allowed origin, the CORS middleware echoes that origin back.
    """
    origin = (request.headers.get("origin") or "").strip().rstrip("/").lower()
    allowed = bool(origin) and origin in settings.allowed_origins
    if auth.kind == "widget" and not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This website is not allowed to use this chat widget",
        )
    if allowed:
        request.state.cors_origin = request.headers["origin"]


def preflight(request: Request, methods: str) -> Response:
    # Preflights carry no API key, so the tenant's origin list cannot be checked here; the
    # actual request is checked and only gets CORS headers for an allowed origin.
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
            "Access-Control-Allow-Methods": methods,
            "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        },
    )
