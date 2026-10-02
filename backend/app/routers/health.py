from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness probe: the process is up. Never touches the database."""
    return {"status": "ok"}
