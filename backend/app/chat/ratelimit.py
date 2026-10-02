"""Fixed-window rate limits and a daily cap, counted in Redis.

Fails open: if Redis is unreachable the request proceeds (and the failure is logged), because
the queue and cache depend on the same Redis and refusing all chat would be worse.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url, socket_timeout=1, socket_connect_timeout=1)

    async def _hit(self, key: str, limit: int, ttl_seconds: int) -> int:
        """Count one request against key; return the new count (0 if Redis failed)."""
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, ttl_seconds, nx=True)
                count, _ = await pipe.execute()
            return int(count)
        except Exception as exc:
            logger.warning("Rate limiter unavailable (%s); allowing request", type(exc).__name__)
            return 0

    async def per_minute(self, name: str, limit: int) -> int | None:
        """None if allowed, else seconds until the window resets."""
        window = int(time.time() // 60)
        if await self._hit(f"bap:rl:{name}:{window}", limit, 60) > limit:
            return max(1, 60 - int(time.time() % 60))
        return None

    async def daily(self, name: str, limit: int) -> int | None:
        now = datetime.now(UTC)
        key = f"bap:cap:{name}:{now:%Y-%m-%d}"
        if await self._hit(key, limit, 2 * 24 * 3600) > limit:
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return max(1, int((tomorrow - now).total_seconds()))
        return None

    async def aclose(self) -> None:
        await self._redis.aclose()
