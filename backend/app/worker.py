"""Background worker: `arq app.worker.WorkerSettings`.

arq: an asyncio-native, Redis-backed job queue (MIT) with few dependencies; it fits this async
stack directly and supports burst mode for tests.
"""

import logging
import uuid

from arq import func
from arq.connections import RedisSettings

from app.channels.alerts import send_staff_alert
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.embeddings import get_embedding_provider
from app.ingestion.pipeline import IngestDeps, process_document
from app.ingestion.queue import INGEST_JOB, STAFF_ALERT_JOB, WEBHOOK_JOB
from app.ingestion.storage import get_storage
from app.webhooks.delivery import deliver

# arq's CLI configures its own "arq" logger; give our "app" loggers a handler of their own
# (rather than the root logger, which would print arq's lines twice).
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_app_logger = logging.getLogger("app")
_app_logger.addHandler(_handler)
_app_logger.setLevel(get_settings().log_level)
_app_logger.propagate = False


async def startup(ctx: dict) -> None:
    ctx.setdefault(
        "deps",
        IngestDeps(
            sessionmaker=get_sessionmaker(),  # DATABASE_URL: the application role, RLS applies
            embedder=get_embedding_provider(),
            storage=get_storage(),
        ),
    )
    logging.getLogger(__name__).info(
        "Worker ready (embeddings: %s)", ctx["deps"].embedder.model_name
    )


async def shutdown(ctx: dict) -> None:
    await ctx["deps"].embedder.aclose()
    await get_engine().dispose()


async def ingest_document(ctx: dict, tenant_id: str, document_id: str) -> str:
    return await process_document(ctx["deps"], uuid.UUID(tenant_id), uuid.UUID(document_id))


async def deliver_webhook(ctx: dict, tenant_id: str, delivery_id: str) -> str:
    """One signed delivery attempt; schedules the next attempt with backoff on failure."""
    result = await deliver(ctx["deps"].sessionmaker, uuid.UUID(tenant_id), uuid.UUID(delivery_id))
    if result.retry_in and ctx.get("redis") is not None:
        await ctx["redis"].enqueue_job(
            WEBHOOK_JOB,
            tenant_id,
            delivery_id,
            _job_id=f"webhook:{delivery_id}:{uuid.uuid4().hex[:8]}",
            _defer_by=result.retry_in,
        )
    return result.status


async def staff_alert(ctx: dict, tenant_id: str, conversation_id: str, kind: str) -> str:
    """Tell the tenant's Telegram staff chat that a customer needs a person."""
    return await send_staff_alert(
        ctx["deps"].sessionmaker, uuid.UUID(tenant_id), uuid.UUID(conversation_id), kind
    )


class WorkerSettings:
    functions = (
        # The job records its own failures on the document; arq retries only if the job is
        # interrupted (e.g. worker restart), so a document is not left in 'processing'.
        func(
            ingest_document,
            name=INGEST_JOB,
            max_tries=3,
            timeout=get_settings().ingest_job_timeout_seconds,
        ),
        func(deliver_webhook, name=WEBHOOK_JOB, max_tries=1, timeout=60),
        func(staff_alert, name=STAFF_ALERT_JOB, max_tries=3, timeout=60),
    )
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
