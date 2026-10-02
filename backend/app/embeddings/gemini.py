"""Gemini embeddings over the REST API (no SDK dependency).

Model: gemini-embedding-2, Google's recommended embedding model as of 2026-10 (replacing the
legacy gemini-embedding-001, whose vectors are not compatible). It takes task instructions in
the text rather than a task_type field, and output_dimensionality=768 is one of its recommended
sizes. https://ai.google.dev/gemini-api/docs/embeddings
"""

import asyncio
import contextlib
import logging
import math
import random
from collections.abc import Awaitable, Callable, Sequence

import httpx

from app.embeddings.base import EmbeddingError, EmbeddingInput, EmbeddingProvider

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-embedding-2"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GeminiEmbeddingProvider(EmbeddingProvider):
    # Starting point from a quick probe of gemini-embedding-2 (2026-10): on-topic questions
    # scored ~0.7+, unrelated text ~0.55. Tuned against evals later; RELEVANCE_THRESHOLD overrides.
    relevance_threshold = 0.65

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        batch_size: int = 100,
        max_attempts: int = 6,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiEmbeddingProvider needs an API key (GEMINI_API_KEY)")
        self.model_name = model
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        # The key travels in a header, never in the URL, so it cannot leak into logged URLs.
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._headers = {"x-goog-api-key": api_key}

    @staticmethod
    def format_document(document: EmbeddingInput) -> str:
        return f"title: {document.title or 'none'} | text: {document.text}"

    @staticmethod
    def format_query(query: str) -> str:
        return f"task: search result | query: {query}"

    async def embed_documents(self, documents: Sequence[EmbeddingInput]) -> list[list[float]]:
        texts = [self.format_document(d) for d in documents]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._batch_embed(texts[start : start + self._batch_size]))
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        return (await self._batch_embed([self.format_query(query)]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _batch_embed(self, texts: list[str]) -> list[list[float]]:
        body = {
            "requests": [
                {
                    "model": f"models/{self.model_name}",
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": self.dimensions,
                }
                for text in texts
            ]
        }
        response = await self._post_with_retries(
            f"{API_BASE}/models/{self.model_name}:batchEmbedContents", body
        )
        try:
            embeddings = [item["values"] for item in response.json()["embeddings"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("Gemini returned an unexpected embedding response") from exc
        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"Gemini returned {len(embeddings)} embeddings for {len(texts)} inputs"
            )
        return [self._check_and_normalize(v) for v in embeddings]

    def _check_and_normalize(self, vector: list[float]) -> list[float]:
        if len(vector) != self.dimensions:
            raise EmbeddingError(
                f"Gemini returned {len(vector)} dimensions, expected {self.dimensions}"
            )
        # gemini-embedding-2 normalizes truncated outputs itself; normalizing again is a cheap
        # guarantee for cosine search (and required for the older gemini-embedding-001).
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        # Exponential backoff with full jitter; honour Retry-After when the server sends one.
        delay = random.uniform(0, min(self._max_delay, self._base_delay * 2**attempt))
        if retry_after:
            with contextlib.suppress(ValueError):  # HTTP-date form: fall back to backoff
                delay = max(delay, min(float(retry_after), self._max_delay))
        return delay

    async def _post_with_retries(self, url: str, body: dict) -> httpx.Response:
        last_error = ""
        for attempt in range(self._max_attempts):
            retry_after = None
            try:
                response = await self._client.post(url, json=body, headers=self._headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}"
            else:
                if response.is_success:
                    return response
                message = _error_message(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise EmbeddingError(
                        f"Gemini embedding request failed ({response.status_code}): {message}"
                    )
                last_error = f"{response.status_code}: {message}"
                retry_after = response.headers.get("retry-after")
            if attempt + 1 < self._max_attempts:
                delay = self._backoff(attempt, retry_after)
                logger.warning(
                    "Gemini embedding attempt %d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    last_error,
                    delay,
                )
                await self._sleep(delay)
        raise EmbeddingError(
            f"Gemini embedding request failed after {self._max_attempts} attempts: {last_error}"
        )


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return response.reason_phrase or "unknown error"
