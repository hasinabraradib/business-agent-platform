"""Chat providers (Gemini, OpenAI-compatible, fake) and the failover chain, all offline."""

import asyncio
import json

import httpx
import pytest

from app.llm import (
    Candidate,
    ChatChain,
    ChatError,
    ChatRequest,
    ChatUnavailable,
    FakeChatProvider,
    Finish,
    GeminiChatProvider,
    LLMSettings,
    Message,
    OpenAICompatChatProvider,
    Scripted,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
    get_chat_chain,
)

SEARCH = ToolSpec(
    "search_knowledge",
    "Search the business's information.",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)
REQUEST = ChatRequest(system="Be brief.", messages=[Message("user", "Hi")], temperature=0.1)


def _sse(*events) -> bytes:
    return b"".join(
        b"data: " + (e if isinstance(e, bytes) else json.dumps(e).encode()) + b"\r\n\r\n"
        for e in events
    )


async def collect(provider, request=REQUEST, model="m"):
    events = [e async for e in provider.stream(request, model=model)]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    finish = events[-1]
    assert isinstance(finish, Finish)
    return text, calls, finish


def mocked(cls, handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if cls is GeminiChatProvider:
        return GeminiChatProvider("test-key", client=client, **kwargs)
    return OpenAICompatChatProvider("https://llm.example/v1/", "sk-test", client=client, **kwargs)


# --- Gemini ------------------------------------------------------------------------------------


def _gemini_text(text, **extra):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}], **extra}


async def test_gemini_streams_text_and_reports_usage_and_model() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=_sse(
                {"candidates": [{"content": {"parts": [{"text": "plan", "thought": True}]}}]},
                _gemini_text("Hello"),
                _gemini_text(
                    " there!",
                    usageMetadata={
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 3,
                        "thoughtsTokenCount": 5,
                    },
                ),
            ),
        )

    text, calls, finish = await collect(
        mocked(GeminiChatProvider, handler), model="gemini-3.8-flash"
    )
    assert (text, calls) == ("Hello there!", [])
    assert (finish.usage.prompt_tokens, finish.usage.completion_tokens) == (12, 8)
    assert finish.model == "gemini:gemini-3.8-flash"
    assert str(seen[0].url).endswith("models/gemini-3.8-flash:streamGenerateContent?alt=sse")
    assert seen[0].headers["x-goog-api-key"] == "test-key"
    body = json.loads(seen[0].content)
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    assert "tools" not in body


async def test_gemini_uses_the_lowest_thinking_level_per_model() -> None:
    provider = GeminiChatProvider("k")
    assert provider.build_body(REQUEST, "gemini-3.6-flash")["generationConfig"][
        "thinkingConfig"
    ] == {"thinkingLevel": "MINIMAL"}
    assert provider.build_body(REQUEST, "gemini-3.8-flash")["generationConfig"][
        "thinkingConfig"
    ] == {"thinkingLevel": "LOW"}


async def test_gemini_tool_calls_and_thought_signatures_are_replayed_verbatim() -> None:
    bodies = []
    call_part = {
        "functionCall": {"name": "search_knowledge", "args": {"query": "kacchi price"}},
        "thoughtSignature": "SIG-123",
    }

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200, content=_sse({"candidates": [{"content": {"parts": [call_part]}}]})
            )
        return httpx.Response(200, content=_sse(_gemini_text("BDT 480 [1]")))

    provider = mocked(GeminiChatProvider, handler)
    request = ChatRequest(system="s", messages=[Message("user", "kacchi koto?")], tools=[SEARCH])
    _, calls, finish = await collect(provider, request, model="gemini-3.8-flash")
    assert [(c.name, c.arguments) for c in calls] == [
        ("search_knowledge", {"query": "kacchi price"})
    ]
    assert bodies[0]["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "search_knowledge",
                    "description": SEARCH.description,
                    "parameters": SEARCH.parameters,
                }
            ]
        }
    ]
    follow_up = ChatRequest(
        system="s",
        messages=[
            Message("user", "kacchi koto?"),
            finish.assistant,
            Message(
                "tool",
                "[1] Menu\nprice 480",
                tool_call_id=calls[0].id,
                tool_name="search_knowledge",
            ),
        ],
        tools=[SEARCH],
    )
    text, _, _ = await collect(provider, follow_up, model="gemini-3.8-flash")
    assert text == "BDT 480 [1]"
    contents = bodies[1]["contents"]
    assert contents[1] == {"role": "model", "parts": [call_part]}  # signature kept exactly
    assert contents[2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "search_knowledge",
                    "response": {"result": "[1] Menu\nprice 480"},
                }
            }
        ],
    }


def test_gemini_rebuilds_messages_from_other_providers() -> None:
    message = Message(
        "assistant", "", tool_calls=[ToolCall("x1", "search_knowledge", {"query": "q"})]
    )
    body = GeminiChatProvider("k").build_body(
        ChatRequest(system="s", messages=[message]), "gemini-3.6-flash"
    )
    # Without a thoughtSignature Gemini 3 answers 400 "Function call is missing a
    # thought_signature" (seen when Groq hit its daily limit mid tool loop and Gemini took over).
    assert body["contents"] == [
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"name": "search_knowledge", "args": {"query": "q"}},
                    "thoughtSignature": "skip_thought_signature_validator",
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (
            httpx.Response(503, json={"error": {"message": "high demand"}}),
            ChatUnavailable,
            "high demand",
        ),
        (
            httpx.Response(429, json={"error": {"message": "quota " * 200}}),
            ChatUnavailable,
            "quota",
        ),
        (httpx.Response(400, json={"error": {"message": "bad"}}), ChatError, r"\(400\): bad"),
        (
            httpx.Response(200, content=_sse({"promptFeedback": {"blockReason": "SAFETY"}})),
            ChatError,
            "blocked",
        ),
        (httpx.Response(200, content=b"data: {broken\r\n\r\n"), ChatError, "malformed"),
    ],
)
async def test_gemini_error_mapping(response, error, match) -> None:
    with pytest.raises(error, match=match) as exc_info:
        await collect(mocked(GeminiChatProvider, lambda r: response), model="gemini-3.8-flash")
    assert exc_info.value.model == "gemini:gemini-3.8-flash"
    if response.status_code == 429:
        assert len(str(exc_info.value)) > 1000  # long errors are kept (bounded at 2,000)


async def test_gemini_400_is_not_marked_unavailable() -> None:
    response = httpx.Response(400, json={"error": {"message": "bad"}})
    with pytest.raises(ChatError) as exc_info:
        await collect(mocked(GeminiChatProvider, lambda r: response))
    assert not isinstance(exc_info.value, ChatUnavailable)


# --- OpenAI-compatible -------------------------------------------------------------------------


def _chunk(delta=None, finish=None, usage=None):
    event = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        event = {"choices": [], "usage": usage}
    return event


async def test_openai_compat_streams_text_with_usage() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=_sse(
                _chunk({"role": "assistant", "content": ""}),
                _chunk({"content": "Ji, "}),
                _chunk({"content": "amra khola."}, finish="stop"),
                _chunk(usage={"prompt_tokens": 20, "completion_tokens": 4}),
                b"[DONE]",
            ),
        )

    provider = mocked(OpenAICompatChatProvider, handler, reasoning_effort="minimal")
    text, calls, finish = await collect(provider, model="small-model")
    assert (text, calls) == ("Ji, amra khola.", [])
    assert (finish.usage.prompt_tokens, finish.usage.completion_tokens) == (20, 4)
    assert finish.model == "openai_compat:small-model"
    assert str(seen[0].url) == "https://llm.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    body = json.loads(seen[0].content)
    assert body["model"] == "small-model" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["reasoning_effort"] == "minimal"


async def test_openai_compat_assembles_fragmented_tool_calls_and_converts_history() -> None:
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_sse(
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "search_", "arguments": ""},
                            }
                        ]
                    }
                ),
                _chunk(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "knowledge", "arguments": '{"que'}}
                        ]
                    }
                ),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "kacchi"}'}}]}),
                _chunk(finish="tool_calls"),
                b"[DONE]",
            ),
        )

    provider = mocked(OpenAICompatChatProvider, handler)
    history = [
        Message("user", "kacchi?"),
        Message(
            "assistant", "", tool_calls=[ToolCall("call_0", "search_knowledge", {"query": "x"})]
        ),
        Message("tool", "[1] Menu", tool_call_id="call_0", tool_name="search_knowledge"),
    ]
    request = ChatRequest(system="s", messages=history, tools=[SEARCH])
    _, calls, finish = await collect(provider, request)
    assert calls == [ToolCall("call_1", "search_knowledge", {"query": "kacchi"})]
    assert finish.assistant.tool_calls == calls
    body = bodies[0]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": SEARCH.description,
                "parameters": SEARCH.parameters,
            },
        }
    ]
    assert body["messages"][2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "search_knowledge", "arguments": '{"query": "x"}'},
            }
        ],
    }
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "call_0", "content": "[1] Menu"}


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            ChatUnavailable,
            "rate limited",
        ),
        (httpx.Response(503, text="overloaded"), ChatUnavailable, r"\(503\)"),
        (httpx.Response(401, json={"error": {"message": "bad key"}}), ChatError, "bad key"),
        (httpx.Response(200, content=_sse({"error": {"message": "boom"}})), ChatError, "boom"),
        (
            httpx.Response(
                200,
                content=_sse(
                    _chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c",
                                    "function": {"name": "t", "arguments": "{oops"},
                                }
                            ]
                        }
                    ),
                    b"[DONE]",
                ),
            ),
            ChatError,
            "invalid JSON",
        ),
    ],
)
async def test_openai_compat_error_mapping(response, error, match) -> None:
    with pytest.raises(error, match=match) as exc_info:
        await collect(mocked(OpenAICompatChatProvider, lambda r: response), model="small-model")
    assert exc_info.value.model == "openai_compat:small-model"
    if error is ChatError:
        assert not isinstance(exc_info.value, ChatUnavailable)


# Groq's reply when the model's tool arguments break the schema (seen in the real run).
GROQ_TOOL_USE_FAILED = {
    "message": "Tool call validation failed: parameters for tool capture_lead did not match "
    "schema: errors: [`/name`: minLength: got 0, want 1]",
    "type": "invalid_request_error",
    "code": "tool_use_failed",
    "failed_generation": '{"name": "capture_lead", "arguments": {\n  "name": "",\n  '
    '"contact": "",\n  "interest": "50 sarees for a wedding"\n}}',
}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": GROQ_TOOL_USE_FAILED}),
        httpx.Response(200, content=_sse({"error": GROQ_TOOL_USE_FAILED})),
    ],
)
async def test_openai_compat_hands_back_tool_calls_the_provider_rejected(response) -> None:
    # Was a ChatError (an apology to the customer); the registry should validate the attempt
    # and return the errors to the model instead.
    _, calls, finish = await collect(mocked(OpenAICompatChatProvider, lambda r: response))
    assert [(c.name, c.arguments) for c in calls] == [
        ("capture_lead", {"name": "", "contact": "", "interest": "50 sarees for a wedding"})
    ]
    assert finish.assistant.tool_calls == calls


async def test_openai_compat_other_tool_failures_still_raise() -> None:
    garbled = {**GROQ_TOOL_USE_FAILED, "failed_generation": "not json"}
    response = httpx.Response(400, json={"error": garbled})
    with pytest.raises(ChatError, match="Tool call validation failed"):
        await collect(mocked(OpenAICompatChatProvider, lambda r: response))


async def test_openai_compat_rate_limit_carries_retry_after() -> None:
    # Groq's per-minute token limit answers 429 with "Retry-After: 5"; the chain uses it.
    response = httpx.Response(
        429, headers={"retry-after": "5"}, json={"error": {"message": "TPM limit"}}
    )
    with pytest.raises(ChatUnavailable) as exc_info:
        await collect(mocked(OpenAICompatChatProvider, lambda r: response))
    assert exc_info.value.retry_after == 5.0


async def test_openai_compat_connection_errors_are_unavailable() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ChatUnavailable):
        await collect(mocked(OpenAICompatChatProvider, handler))


# --- the failover chain ------------------------------------------------------------------------


def _fake(name, script):
    return FakeChatProvider(lambda request, model: script, name=name)


async def collect_chain(chain, request=REQUEST, prefer=None):
    events = [e async for e in chain.stream(request, prefer=prefer)]
    return "".join(e.text for e in events if isinstance(e, TextDelta)), events[-1]


async def test_chain_fails_over_fast_when_the_primary_is_slow() -> None:
    slow = _fake("primary", Scripted("too late", first_delay=2))
    fast = _fake("fallback", Scripted("Hello from the fallback"))
    chain = ChatChain([Candidate(slow, "m1"), Candidate(fast, "m2")], first_event_timeout=0.1)
    started = asyncio.get_running_loop().time()
    text, finish = await collect_chain(chain)
    assert text == "Hello from the fallback"
    assert finish.model == "fallback:m2"
    assert asyncio.get_running_loop().time() - started < 1


async def test_chain_fails_over_on_unavailable_and_cools_the_failed_model() -> None:
    now = [0.0]
    down = _fake("primary", Scripted(unavailable=True))
    up = _fake("fallback", Scripted("ok"))
    chain = ChatChain(
        [Candidate(down, "m1"), Candidate(up, "m2")], cooldown_seconds=60, clock=lambda: now[0]
    )
    assert (await collect_chain(chain))[1].model == "fallback:m2"
    assert len(down.calls) == 1
    await collect_chain(chain)  # within the cool-down: the primary is skipped
    assert len(down.calls) == 1
    now[0] = 61
    await collect_chain(chain)  # cooled down: tried again
    assert len(down.calls) == 2


class _RateLimited(FakeChatProvider):
    def __init__(self, retry_after):
        super().__init__(name="limited")
        self.retry_after, self.tries = retry_after, 0

    async def stream(self, request, *, model):
        self.tries += 1
        raise ChatUnavailable("429", model=f"limited:{model}", retry_after=self.retry_after)
        yield  # pragma: no cover


async def test_chain_cooldown_is_no_longer_than_the_providers_retry_after() -> None:
    now = [0.0]
    limited = _RateLimited(retry_after=5)
    chain = ChatChain(
        [Candidate(limited, "m1"), Candidate(_fake("fallback", Scripted("ok")), "m2")],
        cooldown_seconds=60,
        clock=lambda: now[0],
    )
    await collect_chain(chain)
    now[0] = 3
    await collect_chain(chain)  # still within Retry-After: skipped
    assert limited.tries == 1
    now[0] = 6
    await collect_chain(chain)  # a 60 s cool-down would still skip it here
    assert limited.tries == 2


async def test_chain_prefers_the_model_already_used_in_this_turn() -> None:
    a, b = _fake("a", Scripted("from a")), _fake("b", Scripted("from b"))
    chain = ChatChain([Candidate(a, "m"), Candidate(b, "m")])
    assert (await collect_chain(chain, prefer="b:m"))[0] == "from b"


async def test_chain_commits_once_streaming_started() -> None:
    flaky = _fake("primary", Scripted("one two three", fail_after_chunks=1))
    backup = _fake("fallback", Scripted("never used"))
    chain = ChatChain([Candidate(flaky, "m1"), Candidate(backup, "m2")])
    with pytest.raises(ChatError, match="fake model failure"):
        await collect_chain(chain)
    assert backup.calls == []


async def test_chain_raises_the_last_error_when_everything_fails() -> None:
    chain = ChatChain(
        [
            Candidate(_fake("a", Scripted(unavailable=True)), "m1"),
            Candidate(_fake("b", Scripted(unavailable=True)), "m2"),
        ]
    )
    with pytest.raises(ChatUnavailable) as exc_info:
        await collect_chain(chain)
    assert exc_info.value.model == "b:m2"


def _llm_settings(**overrides) -> LLMSettings:
    """Settings independent of the developer's .env (which may hold real provider keys)."""
    blank = {"gemini_api_key": "", "openai_compat_base_url": "", "openai_compat_api_key": ""}
    return LLMSettings(_env_file=None, **{"openai_compat_model": "", **blank, **overrides})


def test_chain_registry() -> None:
    fake = get_chat_chain(_llm_settings(chat_provider="fake"))
    assert fake.offline and fake.primary == "fake:fake-chat"
    assert get_chat_chain(_llm_settings(chat_provider="auto")).offline  # no keys configured
    gemini = get_chat_chain(_llm_settings(chat_provider="auto", gemini_api_key="k"))
    # 3.6 first: it stayed inside the 3 s failover window in the live eval; 3.8 did not.
    assert [c.label for c in gemini.candidates] == [
        "gemini:gemini-3.6-flash",
        "gemini:gemini-3.8-flash",
    ]
    mixed = get_chat_chain(
        _llm_settings(
            chat_provider="auto",
            gemini_api_key="k",
            openai_compat_base_url="https://llm.example/v1",
            openai_compat_api_key="sk",
            openai_compat_model="small",
        )
    )
    assert [c.label for c in mixed.candidates] == [
        "openai_compat:small",  # the OpenAI-compatible model is primary, Gemini the fallback
        "gemini:gemini-3.6-flash",
        "gemini:gemini-3.8-flash",
    ]
    assert not mixed.offline
    only_openai = get_chat_chain(
        _llm_settings(
            chat_provider="auto",
            gemini_api_key="",
            chat_models="gemini:gemini-3.8-flash,openai_compat:small",
            openai_compat_base_url="https://llm.example/v1",
            openai_compat_api_key="sk",
        )
    )
    assert [c.label for c in only_openai.candidates] == ["openai_compat:small"]  # no Gemini key
    with pytest.raises(ValueError, match="Unknown chat provider"):
        get_chat_chain(
            LLMSettings(chat_provider="auto", chat_models="mystery:model", gemini_api_key="k")
        )


async def test_fake_offline_responder_greets_searches_and_answers() -> None:
    fake = FakeChatProvider()
    greet = ChatRequest(system="s", messages=[Message("user", "hello")], tools=[SEARCH])
    text, calls, _ = await collect(fake, greet)
    assert text.startswith("[[smalltalk]]") and calls == []
    ask = ChatRequest(system="s", messages=[Message("user", "kacchi price")], tools=[SEARCH])
    _, calls, finish = await collect(fake, ask)
    assert calls[0].name == "search_knowledge" and calls[0].arguments == {"query": "kacchi price"}
    answer = ChatRequest(
        system="s",
        messages=[
            *ask.messages,
            finish.assistant,
            Message("tool", "<r>\n[3] Menu (row 1)\ndish: Kacchi\n</r>", tool_call_id="x"),
        ],
    )
    text, _, _ = await collect(fake, answer)
    assert text == "[[answered]]\nFrom what we have: dish: Kacchi [3]"
    assert "Offline demo model" not in text
