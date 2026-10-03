"""Gemini chat over REST with server-sent events and function calling (no SDK dependency).

Models (checked 2026-10, https://ai.google.dev/gemini-api/docs/models): gemini-3.8-flash is
Google's default general-purpose model; gemini-3.6-flash is the fallback. Each request uses the
lowest thinking level the model supports (3.8-flash: low; 3.6-flash: minimal).

Gemini 3 attaches thought signatures to response parts (e.g. functionCall parts); the model's
parts are replayed exactly as received in the follow-up request. Function calls made by another
model (a failover in the middle of a tool loop) have no signature, and Gemini 3 rejects them
with a 400; they carry the placeholder Google documents for that case instead.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.base import (
    ChatError,
    ChatProvider,
    ChatRequest,
    ChatUnavailable,
    Finish,
    Message,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    Usage,
)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
BLOCKING_FINISH_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_ERROR_CHARS = 2000
# Lowest supported thinking level per model (Google's thinking guide, 2026-10).
LOWEST_THINKING = {"gemini-3.8-flash": "LOW", "gemini-3.6-flash": "MINIMAL"}

# Google's documented thoughtSignature for function calls Gemini did not make itself.
UNSIGNED_CALL_SIGNATURE = "skip_thought_signature_validator"


class GeminiChatProvider(ChatProvider):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        thinking_levels: dict[str, str] | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiChatProvider needs an API key (GEMINI_API_KEY)")
        self._thinking = {**LOWEST_THINKING, **(thinking_levels or {})}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10))
        self._headers = {"x-goog-api-key": api_key}  # never in the URL

    def _contents(self, messages: list[Message], model: str) -> list[dict[str, Any]]:
        key = f"{self.name}:{model}"
        contents: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "user":
                contents.append({"role": "user", "parts": [{"text": message.text}]})
            elif message.role == "assistant":
                parts = message.provider_state.get(key)
                if parts is None:  # written by another model/provider: rebuild generically
                    parts = ([{"text": message.text}] if message.text else []) + [
                        {
                            "functionCall": {"name": call.name, "args": call.arguments},
                            "thoughtSignature": UNSIGNED_CALL_SIGNATURE,
                        }
                        for call in message.tool_calls
                    ]
                contents.append({"role": "model", "parts": parts})
            else:
                part = {
                    "functionResponse": {
                        "name": message.tool_name,
                        "response": {"result": message.text},
                    }
                }
                # Consecutive tool results belong in one user turn.
                if (
                    contents
                    and contents[-1]["role"] == "user"
                    and "functionResponse" in (contents[-1]["parts"][0])
                ):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
        return contents

    def build_body(self, request: ChatRequest, model: str) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_output_tokens,
            "thinkingConfig": {"thinkingLevel": self._thinking.get(model, "LOW")},
        }
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": self._contents(request.messages, model),
            "generationConfig": config,
        }
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t.name, "description": t.description, "parameters": t.parameters}
                        for t in request.tools
                    ]
                }
            ]
        return body

    async def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[StreamEvent]:
        label = f"{self.name}:{model}"
        url = f"{API_BASE}/models/{model}:streamGenerateContent?alt=sse"
        usage = Usage()
        parts: list[dict[str, Any]] = []  # raw model parts, replayed verbatim next step
        text: list[str] = []
        calls: list[ToolCall] = []
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
                    raise _for_status(response.status_code)(error, model=label)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:])
                    except ValueError as exc:
                        error = "Gemini sent a malformed stream event"
                        raise ChatError(error, model=label) from exc
                    if reason := (event.get("promptFeedback") or {}).get("blockReason"):
                        raise ChatError(f"Gemini blocked the prompt ({reason})", model=label)
                    for candidate in event.get("candidates") or []:
                        for part in (candidate.get("content") or {}).get("parts") or []:
                            if part.get("thought"):
                                continue  # never stream or replay reasoning text
                            parts.append(part)
                            if "functionCall" in part:
                                call = ToolCall(
                                    id=part["functionCall"].get("id") or uuid.uuid4().hex,
                                    name=part["functionCall"].get("name", ""),
                                    arguments=part["functionCall"].get("args") or {},
                                )
                                calls.append(call)
                                started = True
                                yield ToolCallEvent(call)
                            elif part.get("text"):
                                text.append(part["text"])
                                started = True
                                yield TextDelta(part["text"])
                        reason = candidate.get("finishReason")
                        if reason in BLOCKING_FINISH_REASONS:
                            raise ChatError(f"Gemini stopped the answer ({reason})", model=label)
                    if metadata := event.get("usageMetadata"):
                        usage = Usage(
                            prompt_tokens=int(metadata.get("promptTokenCount", 0)),
                            completion_tokens=int(metadata.get("candidatesTokenCount", 0))
                            + int(metadata.get("thoughtsTokenCount", 0)),
                        )
        except httpx.TimeoutException as exc:
            error = f"Gemini chat request timed out ({type(exc).__name__})"
            raise (ChatError if started else ChatUnavailable)(error, model=label) from exc
        except httpx.TransportError as exc:
            error = f"Gemini chat request failed ({type(exc).__name__})"
            raise (ChatError if started else ChatUnavailable)(error, model=label) from exc
        assistant = Message(
            role="assistant", text="".join(text), tool_calls=calls, provider_state={label: parts}
        )
        yield Finish(usage=usage, model=label, assistant=assistant)

    async def aclose(self) -> None:
        await self._client.aclose()


def _for_status(status: int) -> type[ChatError]:
    """Overload, rate limits and server errors mean "try another model"."""
    return ChatUnavailable if status in RETRYABLE_STATUS else ChatError


def _error_message(body: bytes) -> str:
    try:
        return str(json.loads(body)["error"]["message"])[:MAX_ERROR_CHARS]
    except (ValueError, KeyError, TypeError):
        return "unknown error"
