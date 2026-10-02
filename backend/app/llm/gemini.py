"""Gemini chat over the REST API with server-sent events (no SDK dependency).

Models (checked 2026-10, https://ai.google.dev/gemini-api/docs/models):
- answers: gemini-3.8-flash, Google's current default general-purpose model;
- helper calls: gemini-3.5-flash-lite, the low-cost model the reranker also uses.
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from app.llm.base import ChatChunk, ChatError, ChatProvider, ChatRequest, Usage

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_ANSWER_MODEL = "gemini-3.8-flash"
DEFAULT_HELPER_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACK_MODEL = "gemini-3.6-flash"
BLOCKING_FINISH_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ERROR_CHARS = 2000

logger = logging.getLogger(__name__)


class _Unavailable(ChatError):
    """A retryable failure before anything was streamed."""


class GeminiChatProvider(ChatProvider):
    def __init__(
        self,
        api_key: str,
        answer_model: str = DEFAULT_ANSWER_MODEL,
        helper_model: str = DEFAULT_HELPER_MODEL,
        *,
        answer_thinking_level: str | None = "LOW",
        helper_thinking_level: str | None = "MINIMAL",
        fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiChatProvider needs an API key (GEMINI_API_KEY)")
        self.answer_model = answer_model
        self.helper_model = helper_model
        self.fallback_model = fallback_model if fallback_model != answer_model else None
        self._thinking = {answer_model: answer_thinking_level, helper_model: helper_thinking_level}
        if self.fallback_model:
            self._thinking[self.fallback_model] = answer_thinking_level
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10))
        self._headers = {"x-goog-api-key": api_key}  # never in the URL

    def build_body(self, request: ChatRequest, model: str) -> dict:
        generation_config: dict = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_output_tokens,
        }
        if level := self._thinking.get(model):
            generation_config["thinkingConfig"] = {"thinkingLevel": level}
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [
                {"role": turn.role, "parts": [{"text": turn.text}]} for turn in request.turns
            ],
            "generationConfig": generation_config,
        }

    async def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[ChatChunk]:
        """Stream from `model`, retrying overload/rate-limit errors that happen before anything
        was streamed (the customer has seen nothing yet), then trying the fallback model for
        answers. Failures after the first chunk are never retried."""
        models = [model]
        if model == self.answer_model and self.fallback_model:
            models.append(self.fallback_model)
        last_error: ChatError | None = None
        for candidate in models:
            for attempt in range(self._max_attempts):
                try:
                    async for chunk in self._stream_once(request, candidate):
                        yield chunk
                    return
                except _Unavailable as exc:
                    last_error = exc
                    if attempt + 1 < self._max_attempts:
                        delay = random.uniform(0, self._base_delay * 2**attempt)
                        logger.warning(
                            "%s unavailable (%s); retrying in %.1fs", candidate, exc, delay
                        )
                        await self._sleep(delay)
            if candidate != models[-1]:
                logger.warning("%s still unavailable; falling back to %s", candidate, models[-1])
        assert last_error is not None
        raise ChatError(str(last_error), model=last_error.model)

    async def _stream_once(self, request: ChatRequest, model: str) -> AsyncIterator[ChatChunk]:
        url = f"{API_BASE}/models/{model}:streamGenerateContent?alt=sse"
        usage = Usage()
        started = False
        try:
            async with self._client.stream(
                "POST", url, json=self.build_body(request, model), headers=self._headers
            ) as response:
                if not response.is_success:
                    error = (
                        f"Gemini chat request failed ({response.status_code}): "
                        f"{_error_message(await response.aread())}"
                    )
                    if response.status_code in RETRYABLE_STATUS:
                        raise _Unavailable(error, model=model)
                    raise ChatError(error, model=model)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:])
                    except ValueError as exc:
                        error = "Gemini sent a malformed stream event"
                        raise ChatError(error, model=model) from exc
                    try:
                        text, usage_update = _parse_event(event)
                    except ChatError as exc:
                        raise ChatError(str(exc), model=model) from exc
                    if usage_update is not None:
                        usage = usage_update
                    if text:
                        started = True
                        yield ChatChunk(text=text)
        except httpx.TimeoutException as exc:
            error = f"Gemini chat request timed out ({type(exc).__name__})"
            raise ChatError(error, model=model) from exc
        except httpx.TransportError as exc:
            error = f"Gemini chat request failed ({type(exc).__name__})"
            failure = ChatError if started else _Unavailable
            raise failure(error, model=model) from exc
        yield ChatChunk(usage=usage, model=model)

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_event(event: dict) -> tuple[str, Usage | None]:
    if reason := (event.get("promptFeedback") or {}).get("blockReason"):
        raise ChatError(f"Gemini blocked the prompt ({reason})")
    text = []
    for candidate in event.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if not part.get("thought"):  # never stream the model's reasoning
                text.append(part.get("text", ""))
        if (reason := candidate.get("finishReason")) in BLOCKING_FINISH_REASONS:
            raise ChatError(f"Gemini stopped the answer ({reason})")
    usage = None
    if metadata := event.get("usageMetadata"):
        usage = Usage(
            prompt_tokens=int(metadata.get("promptTokenCount", 0)),
            completion_tokens=int(metadata.get("candidatesTokenCount", 0))
            + int(metadata.get("thoughtsTokenCount", 0)),
        )
    return "".join(text), usage


def _error_message(body: bytes) -> str:
    try:
        return str(json.loads(body)["error"]["message"])[:MAX_ERROR_CHARS]
    except (ValueError, KeyError, TypeError):
        return "unknown error"
