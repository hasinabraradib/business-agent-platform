from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.chat.deps import get_chat_service, get_rate_limiter
from app.ingestion.queue import get_job_queue
from app.retrieval import get_retriever
from app.routers import (
    api_keys,
    chat,
    conversations,
    documents,
    health,
    search,
    tenant,
    widget,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await get_job_queue().aclose()
    if get_retriever.cache_info().currsize:  # only if a search ever ran in this process
        retriever = get_retriever()
        await retriever.reranker.aclose()
        await retriever.embedder.aclose()
        if hasattr(retriever.cache, "aclose"):
            await retriever.cache.aclose()
    if get_chat_service.cache_info().currsize:
        await get_chat_service().provider.aclose()
    if get_rate_limiter.cache_info().currsize:
        await get_rate_limiter().aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="Business Agent Platform", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(widget.router)

    v1 = APIRouter(prefix="/v1")
    v1.include_router(tenant.router)
    v1.include_router(api_keys.router)
    v1.include_router(documents.router)
    v1.include_router(search.router)
    v1.include_router(chat.router)
    v1.include_router(conversations.router)
    v1.include_router(widget.config_router)
    app.include_router(v1)

    @app.middleware("http")
    async def widget_cors(request: Request, call_next):
        # Set by the chat route only after the Origin passed the tenant's allow-list.
        response = await call_next(request)
        if origin := getattr(request.state, "cors_origin", None):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            response.headers["Vary"] = "Origin"
        return response

    return app


app = create_app()
