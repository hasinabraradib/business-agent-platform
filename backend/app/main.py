from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.ingestion.queue import get_job_queue
from app.retrieval import get_retriever
from app.routers import api_keys, documents, health, search, tenant


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


def create_app() -> FastAPI:
    app = FastAPI(title="Business Agent Platform", lifespan=lifespan)
    app.include_router(health.router)

    v1 = APIRouter(prefix="/v1")
    v1.include_router(tenant.router)
    v1.include_router(api_keys.router)
    v1.include_router(documents.router)
    v1.include_router(search.router)
    app.include_router(v1)
    return app


app = create_app()
