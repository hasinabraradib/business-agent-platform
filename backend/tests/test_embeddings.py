import json
import math

import httpx
import pytest

from app.embeddings import (
    EmbeddingError,
    EmbeddingInput,
    EmbeddingSettings,
    FakeEmbeddingProvider,
    GeminiEmbeddingProvider,
    get_embedding_provider,
)

DIMS = 768


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_fake_provider_is_deterministic_normalized_and_semantic() -> None:
    provider = FakeEmbeddingProvider()
    docs = [
        EmbeddingInput("Kacchi biryani with mutton", title="Menu"),
        EmbeddingInput("Returns accepted within 7 days", title="Policy"),
    ]
    first = await provider.embed_documents(docs)
    assert first == await provider.embed_documents(docs)
    assert all(len(v) == DIMS and math.isclose(math.sqrt(_cosine(v, v)), 1.0) for v in first)

    query = await provider.embed_query("mutton biryani")
    assert _cosine(query, first[0]) > _cosine(query, first[1])
    assert len(await provider.embed_query("")) == DIMS


def _gemini_response(texts: list[str], dims: int = DIMS) -> httpx.Response:
    # Unnormalized vectors, so the provider's normalization is exercised.
    return httpx.Response(
        200, json={"embeddings": [{"values": [float(i + 1)] * dims} for i, _ in enumerate(texts)]}
    )


class FakeGemini:
    """Scripted Gemini endpoint: returns queued responses, then successful embeddings."""

    def __init__(self, failures: list[httpx.Response | Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        texts = [r["content"]["parts"][0]["text"] for r in json.loads(request.content)["requests"]]
        return _gemini_response(texts)


def _provider(fake: FakeGemini, sleeps: list[float], **kwargs) -> GeminiEmbeddingProvider:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return GeminiEmbeddingProvider(
        "test-key-not-real",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        sleep=record_sleep,
        **kwargs,
    )


async def test_gemini_request_shape_batching_and_normalization() -> None:
    fake, sleeps = FakeGemini(), []
    provider = _provider(fake, sleeps, batch_size=100)
    docs = [EmbeddingInput(f"chunk {i}", title="Menu") for i in range(250)]

    vectors = await provider.embed_documents(docs)

    assert len(vectors) == 250
    assert all(math.isclose(math.sqrt(_cosine(v, v)), 1.0) for v in vectors)
    assert [len(json.loads(r.content)["requests"]) for r in fake.requests] == [100, 100, 50]
    request = fake.requests[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-embedding-2:batchEmbedContents"
    )
    assert request.headers["x-goog-api-key"] == "test-key-not-real"
    assert "test-key-not-real" not in str(request.url)
    first = json.loads(request.content)["requests"][0]
    assert first == {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": "title: Menu | text: chunk 0"}]},
        "outputDimensionality": 768,
    }
    assert sleeps == []


async def test_gemini_query_uses_search_task_prefix() -> None:
    fake = FakeGemini()
    await _provider(fake, []).embed_query("is there parking?")
    text = json.loads(fake.requests[0].content)["requests"][0]["content"]["parts"][0]["text"]
    assert text == "task: search result | query: is there parking?"


async def test_gemini_retries_rate_limits_with_backoff() -> None:
    rate_limited = httpx.Response(429, json={"error": {"message": "Resource exhausted"}})
    fake, sleeps = FakeGemini([rate_limited, rate_limited]), []
    provider = _provider(fake, sleeps, base_delay=1.0, max_delay=30.0)

    vectors = await provider.embed_documents([EmbeddingInput("hello")])

    assert len(vectors) == 1
    assert len(fake.requests) == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1.0 and 0 <= sleeps[1] <= 2.0  # full jitter, doubling cap


async def test_gemini_honours_retry_after_and_retries_transport_errors() -> None:
    fake = FakeGemini(
        [
            httpx.Response(503, headers={"retry-after": "7"}),
            httpx.ConnectTimeout("timed out"),
        ]
    )
    sleeps: list[float] = []
    await _provider(fake, sleeps).embed_documents([EmbeddingInput("hello")])
    assert len(fake.requests) == 3
    assert sleeps[0] >= 7


async def test_gemini_gives_up_after_max_attempts() -> None:
    fake = FakeGemini([httpx.Response(429, json={"error": {"message": "quota"}})] * 10)
    sleeps: list[float] = []
    with pytest.raises(EmbeddingError, match="after 3 attempts: 429: quota") as exc_info:
        await _provider(fake, sleeps, max_attempts=3).embed_documents([EmbeddingInput("x")])
    assert len(fake.requests) == 3
    assert len(sleeps) == 2
    assert "test-key-not-real" not in str(exc_info.value)


async def test_gemini_does_not_retry_client_errors() -> None:
    fake = FakeGemini([httpx.Response(400, json={"error": {"message": "API key not valid"}})])
    with pytest.raises(EmbeddingError, match=r"\(400\): API key not valid"):
        await _provider(fake, []).embed_documents([EmbeddingInput("x")])
    assert len(fake.requests) == 1


async def test_gemini_rejects_wrong_dimensions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _gemini_response(["x"], dims=3072)

    provider = GeminiEmbeddingProvider(
        "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(EmbeddingError, match="3072 dimensions, expected 768"):
        await provider.embed_documents([EmbeddingInput("x")])


def test_provider_selection() -> None:
    assert isinstance(get_embedding_provider(EmbeddingSettings()), FakeEmbeddingProvider)
    auto_with_key = EmbeddingSettings(embedding_provider="auto", gemini_api_key="k")
    assert isinstance(get_embedding_provider(auto_with_key), GeminiEmbeddingProvider)
    assert isinstance(
        get_embedding_provider(EmbeddingSettings(embedding_provider="fake", gemini_api_key="k")),
        FakeEmbeddingProvider,
    )
    with pytest.raises(ValueError, match="needs an API key"):
        get_embedding_provider(EmbeddingSettings(embedding_provider="gemini"))
    with pytest.raises(ValueError, match="Unknown EMBEDDING_PROVIDER"):
        get_embedding_provider(EmbeddingSettings(embedding_provider="nope"))
