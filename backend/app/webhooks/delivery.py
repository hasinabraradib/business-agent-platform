"""Signed webhook delivery, run by the worker.

Each request carries:
  X-BAP-Event:      the event type, e.g. reservation.created
  X-BAP-Delivery:   the delivery id (stable across retries; receivers can de-duplicate on it)
  X-BAP-Timestamp:  unix seconds when this attempt was signed
  X-BAP-Signature:  "v1=" + hex(HMAC-SHA256(secret, "<timestamp>.<raw body>"))
Receivers recompute the signature over the raw body and reject timestamps older than a few
minutes, which stops replays. Failed attempts are retried with backoff and logged.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.fetch import FetchError, PostResult, UnsafeURLError, post_json
from app.models import WebhookDelivery, WebhookEndpoint
from app.tenancy import tenant_db

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (10, 60, 300, 1800, 7200)  # after attempts 1..5; then give up
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
TIMEOUT_SECONDS = 10.0


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "v1=" + digest.hexdigest()


def verify(
    secret: str, timestamp: int, body: bytes, signature: str, *, now: float, tolerance: int = 300
) -> bool:
    """Reference verification (what a receiver does)."""
    if abs(now - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


Poster = Callable[..., Awaitable[PostResult]]


@dataclass(frozen=True)
class AttemptResult:
    status: str  # delivered | retrying | failed | skipped
    retry_in: int | None = None


async def deliver(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    delivery_id: uuid.UUID,
    *,
    poster: Poster = post_json,
    clock: Callable[[], float] = time.time,
) -> AttemptResult:
    """Make one delivery attempt and record it; tells the caller whether to retry and when."""
    async with tenant_db(sessionmaker, tenant_id) as db:
        delivery = await db.get(WebhookDelivery, delivery_id, for_update=True)
        if delivery is None or delivery.status in ("delivered", "failed"):
            return AttemptResult("skipped")
        endpoint = await db.scalar(db.select(WebhookEndpoint))
        started = time.perf_counter()
        timestamp = int(clock())
        body = json.dumps(delivery.payload, separators=(",", ":"), ensure_ascii=False).encode()
        status_code, error = None, None
        if endpoint is None or not endpoint.enabled:
            error = "webhook endpoint removed or disabled"
        else:
            headers = {
                "X-BAP-Event": delivery.event_type,
                "X-BAP-Delivery": str(delivery.id),
                "X-BAP-Timestamp": str(timestamp),
                "X-BAP-Signature": sign(endpoint.secret, timestamp, body),
            }
            try:
                result = await poster(delivery.url, body, headers, timeout_seconds=TIMEOUT_SECONDS)
                status_code = result.status_code
                if not 200 <= status_code < 300:
                    error = f"HTTP {status_code}: {result.body_excerpt[:200]}"
            except UnsafeURLError as exc:
                error = f"refused: {exc}"
            except FetchError as exc:
                error = str(exc)
        delivery.attempts += 1
        delivery.last_status_code = status_code
        delivery.last_error = error
        delivery.attempt_log = [
            *delivery.attempt_log,
            {
                "at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                "status_code": status_code,
                "error": error,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        ]
        if error is None:
            delivery.status = "delivered"
            delivery.delivered_at = datetime.fromtimestamp(timestamp, UTC)
            delivery.next_attempt_at = None
            outcome = AttemptResult("delivered")
        elif endpoint is None or error.startswith("refused") or delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.next_attempt_at = None
            outcome = AttemptResult("failed")
        else:
            wait = BACKOFF_SECONDS[delivery.attempts - 1]
            delivery.status = "retrying"
            delivery.next_attempt_at = datetime.fromtimestamp(timestamp, UTC) + timedelta(
                seconds=wait
            )
            outcome = AttemptResult("retrying", wait)
        await db.commit()
    if error:
        logger.warning("Webhook delivery %s attempt failed: %s", delivery_id, error)
    return outcome
