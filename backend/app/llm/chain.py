"""Ordered failover across chat models and providers.

Each candidate gets a short window (CHAT_FAILOVER_SECONDS, default 3 s) to produce its first
event (a token or a tool call). If it is overloaded, rate-limited, unreachable or simply slow,
the next candidate is tried at once instead of waiting out retries. A candidate that failed is
skipped for a cool-down period so the next turns do not pay the same delay; when the provider
says how long to wait (Retry-After), the cool-down is no longer than that. Once a candidate
has produced an event it is committed to: later failures are errors, never silent switches.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from app.llm.base import ChatError, ChatProvider, ChatRequest, ChatUnavailable, StreamEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    provider: ChatProvider
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider.name}:{self.model}"


class ChatChain:
    def __init__(
        self,
        candidates: list[Candidate],
        *,
        first_event_timeout: float = 3.0,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not candidates:
            raise ValueError("a chat chain needs at least one model")
        self.candidates = candidates
        self.first_event_timeout = first_event_timeout
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._cooling: dict[str, float] = {}

    @property
    def offline(self) -> bool:
        return all(c.provider.offline for c in self.candidates)

    @property
    def primary(self) -> str:
        return self.candidates[0].label

    def _order(self, prefer: str | None) -> list[Candidate]:
        now = self._clock()
        ready = [c for c in self.candidates if self._cooling.get(c.label, 0) <= now]
        order = ready or list(self.candidates)  # everything cooling: try them all anyway
        if prefer:
            order.sort(key=lambda c: c.label != prefer)  # stable: preferred first
        return order

    async def stream(
        self, request: ChatRequest, *, prefer: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        order = self._order(prefer)
        last_error: ChatError | None = None
        for index, candidate in enumerate(order):
            is_last = index == len(order) - 1
            events = aiter(candidate.provider.stream(request, model=candidate.model))
            try:
                if is_last:
                    first = await anext(events)
                else:
                    first = await asyncio.wait_for(anext(events), self.first_event_timeout)
            except TimeoutError:
                last_error = ChatUnavailable(
                    f"no response within {self.first_event_timeout:g}s", model=candidate.label
                )
            except ChatUnavailable as exc:
                last_error = exc
            except StopAsyncIteration:
                last_error = ChatError("empty response", model=candidate.label)
            else:
                yield first
                async for event in events:
                    yield event
                return
            await events.aclose()
            self._cooling[candidate.label] = self._clock() + self._cooldown(last_error)
            if not is_last:
                logger.warning(
                    "%s failed over to %s: %s", candidate.label, order[index + 1].label, last_error
                )
        assert last_error is not None
        raise last_error

    def _cooldown(self, error: ChatError) -> float:
        retry_after = getattr(error, "retry_after", None)
        if retry_after is not None and retry_after >= 0:
            return min(self.cooldown_seconds, retry_after)
        return self.cooldown_seconds

    async def aclose(self) -> None:
        for provider in {id(c.provider): c.provider for c in self.candidates}.values():
            await provider.aclose()
