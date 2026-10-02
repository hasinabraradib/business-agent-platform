import json

import httpx
import pytest

from app.llm import (
    ChatError,
    ChatRequest,
    ChatTurn,
    FakeChatProvider,
    GeminiChatProvider,
    LLMSettings,
    Scripted,
    get_chat_provider,
)

REQUEST = ChatRequest(system="Be brief.", turns=[ChatTurn("user", "Hi")], temperature=0.1)


def _sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\r\n\r\n" for e in events)


def _text(text: str, finish: str | None = None, **extra) -> dict:
    candidate: dict = {"content": {"parts": [{"text": text}]}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate], **extra}


async def _no_sleep(seconds: float) -> None:
    pass


def _provider(handler) -> GeminiChatProvider:
    return GeminiChatProvider(
        "test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=_no_sleep,
    )


async def _collect(provider, model=None):
    chunks = [c async for c in provider.stream(REQUEST, model=model or provider.answer_model)]
    return "".join(c.text for c in chunks), chunks[-1].usage


async def test_gemini_streams_text_and_usage_and_skips_thoughts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = _sse(
            {"candidates": [{"content": {"parts": [{"text": "plan...", "thought": True}]}}]},
            _text("Hello"),
            _text(
                " there!",
                "STOP",
                usageMetadata={
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 3,
                    "thoughtsTokenCount": 5,
                },
            ),
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    provider = _provider(handler)
    text, usage = await _collect(provider)
    assert text == "Hello there!"
    assert (usage.prompt_tokens, usage.completion_tokens) == (12, 8)

    request = seen[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.8-flash:streamGenerateContent?alt=sse"
    )
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Hi"}]}]
    assert body["generationConfig"]["temperature"] == 0.1
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}


async def test_gemini_helper_model_uses_minimal_thinking() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(_text("q")))

    provider = _provider(handler)
    completion = await provider.complete(REQUEST, model=provider.helper_model)
    assert completion.text == "q"
    assert seen[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    assert provider.helper_model == "gemini-3.5-flash-lite"


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (
            httpx.Response(429, json={"error": {"message": "Resource exhausted"}}),
            r"\(429\): Resource",
        ),
        (
            httpx.Response(200, content=_sse({"promptFeedback": {"blockReason": "SAFETY"}})),
            "blocked",
        ),
        (httpx.Response(200, content=_sse(_text("a", "SAFETY"))), "stopped"),
        (httpx.Response(200, content=b"data: {not json\r\n\r\n"), "malformed"),
    ],
)
async def test_gemini_errors_raise_chat_error(response, match) -> None:
    with pytest.raises(ChatError, match=match):
        await _collect(_provider(lambda request: response))


async def test_gemini_transport_errors_raise_chat_error() -> None:
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ChatError, match="timed out"):
        await _collect(_provider(handler))


async def test_fake_provider_scripts_and_records_calls() -> None:
    fake = FakeChatProvider(lambda request, model: Scripted("one two three", fail_after_chunks=2))
    chunks = []
    with pytest.raises(ChatError):
        async for chunk in fake.stream(REQUEST, model=fake.answer_model):
            chunks.append(chunk.text)
    assert chunks == ["one ", "two "]
    assert fake.calls_to("fake-chat") == [REQUEST]

    char_fake = FakeChatProvider(lambda request, model: "abc", chunk_size=1)
    text, usage = await _collect(char_fake)
    assert text == "abc" and usage.completion_tokens >= 1


def test_chat_provider_selection() -> None:
    assert isinstance(get_chat_provider(LLMSettings()), FakeChatProvider)
    auto = get_chat_provider(LLMSettings(chat_provider="auto", gemini_api_key="k"))
    assert isinstance(auto, GeminiChatProvider)
    assert auto.answer_model == "gemini-3.8-flash"
    with pytest.raises(ValueError, match="needs an API key"):
        get_chat_provider(LLMSettings(chat_provider="gemini"))
    with pytest.raises(ValueError, match="Unknown CHAT_PROVIDER"):
        get_chat_provider(LLMSettings(chat_provider="nope"))


def _retrying_provider(handler, sleeps, **kwargs) -> GeminiChatProvider:
    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    return GeminiChatProvider(
        "test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=record,
        **kwargs,
    )


async def test_gemini_retries_overload_before_streaming_starts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(200, content=_sse(_text("Hello")))

    sleeps: list[float] = []
    provider = _retrying_provider(handler, sleeps)
    chunks = [c async for c in provider.stream(REQUEST, model=provider.answer_model)]
    assert "".join(c.text for c in chunks) == "Hello"
    assert chunks[-1].model == "gemini-3.8-flash"
    assert len(calls) == 3 and len(sleeps) == 2


async def test_gemini_falls_back_to_the_fallback_model() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[1])
        if "gemini-3.8-flash" in request.url.path:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, content=_sse(_text("From fallback")))

    provider = _retrying_provider(handler, [], max_attempts=2)
    chunks = [c async for c in provider.stream(REQUEST, model=provider.answer_model)]
    assert "".join(c.text for c in chunks) == "From fallback"
    assert chunks[-1].model == "gemini-3.6-flash"
    assert calls == [
        "gemini-3.8-flash:streamGenerateContent",
        "gemini-3.8-flash:streamGenerateContent",
        "gemini-3.6-flash:streamGenerateContent",
    ]


async def test_gemini_gives_up_when_every_model_is_unavailable() -> None:
    provider = _retrying_provider(
        lambda request: httpx.Response(503, json={"error": {"message": "busy"}}), [], max_attempts=2
    )
    with pytest.raises(ChatError, match=r"\(503\): busy"):
        await _collect(provider)


async def test_gemini_helper_calls_do_not_use_the_answer_fallback() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503, json={"error": {"message": "busy"}})

    provider = _retrying_provider(handler, [], max_attempts=1)
    with pytest.raises(ChatError):
        await provider.complete(REQUEST, model=provider.helper_model)
    assert len(calls) == 1


async def test_gemini_client_errors_are_not_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(ChatError, match="bad request"):
        await _collect(_retrying_provider(handler, []))
    assert len(calls) == 1
