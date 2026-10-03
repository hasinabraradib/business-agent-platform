"""Enqueueing ingestion jobs. The API depends on JobQueue; arq on Redis implements it."""

import uuid
from functools import lru_cache
from typing import Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.config import get_settings

INGEST_JOB = "ingest_document"
WEBHOOK_JOB = "deliver_webhook"
STAFF_ALERT_JOB = "send_staff_alert"


class JobQueue(Protocol):
    async def enqueue_ingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None: ...

    async def enqueue_webhook(
        self, tenant_id: uuid.UUID, delivery_id: uuid.UUID, defer_seconds: int = 0
    ) -> None: ...

    async def enqueue_staff_alert(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, kind: str
    ) -> None: ...


class ArqJobQueue:
    def __init__(self, redis_url: str) -> None:
        self._settings = RedisSettings.from_dsn(redis_url)
        self._pool: ArqRedis | None = None

    async def enqueue_ingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        if self._pool is None:
            self._pool = await create_pool(self._settings)
        # A unique job id per request: a reingest while a job is running is processed again
        # afterwards rather than silently dropped. The final chunk swap locks the document row,
        # so overlapping jobs cannot interleave.
        await self._pool.enqueue_job(
            INGEST_JOB, str(tenant_id), str(document_id), _job_id=f"ingest:{uuid.uuid4()}"
        )

    async def enqueue_webhook(
        self, tenant_id: uuid.UUID, delivery_id: uuid.UUID, defer_seconds: int = 0
    ) -> None:
        if self._pool is None:
            self._pool = await create_pool(self._settings)
        await self._pool.enqueue_job(
            WEBHOOK_JOB,
            str(tenant_id),
            str(delivery_id),
            _job_id=f"webhook:{delivery_id}:{uuid.uuid4().hex[:8]}",
            _defer_by=defer_seconds or None,
        )

    async def enqueue_staff_alert(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, kind: str
    ) -> None:
        """kind: "handoff" (a person was asked for) or "message" (the customer wrote again)."""
        if self._pool is None:
            self._pool = await create_pool(self._settings)
        await self._pool.enqueue_job(
            STAFF_ALERT_JOB,
            str(tenant_id),
            str(conversation_id),
            kind,
            _job_id=f"alert:{conversation_id}:{uuid.uuid4().hex[:8]}",
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


@lru_cache
def get_job_queue() -> ArqJobQueue:
    return ArqJobQueue(get_settings().redis_url)
