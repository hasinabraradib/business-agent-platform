from fastapi import APIRouter, FastAPI

from app.routers import api_keys, documents, health, tenant


def create_app() -> FastAPI:
    app = FastAPI(title="Business Agent Platform")
    app.include_router(health.router)

    v1 = APIRouter(prefix="/v1")
    v1.include_router(tenant.router)
    v1.include_router(api_keys.router)
    v1.include_router(documents.router)
    app.include_router(v1)
    return app


app = create_app()
