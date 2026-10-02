import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db import SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness probe: the process is up. Never touches the database."""
    return {"status": "ok"}


@router.get("/health")
async def health(response: Response, session: SessionDep) -> dict[str, str]:
    """Readiness probe: runs SELECT 1 against the database; 503 if it is unreachable."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Database health check failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "down"}
    return {"status": "ok", "database": "up"}
