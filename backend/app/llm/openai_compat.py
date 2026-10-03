"""OpenAI-compatible chat completions (OpenAI, and the many servers that copy its API):
streaming with server-sent events and function/tool calls, over httpx."""

import json
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

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_ERROR_CHARS = 2000


class OpenAICompatChatProvider(ChatProvider):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        reasoning_effort: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("OpenAI-compatible provider needs OPENAI_COMPAT_BASE_URL and _API_KEY")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._reasoning_effort = reasoning_effort
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10))
        self._headers = {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def _messages(request: ChatRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for message in request.messages:
            if message.role == "user":
                messages.append({"role": "user", "content": message.text})
            elif message.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.text or None}
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ]
                messages.append(item)
            else:
                messages.append(
                    {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text}
                )
        return messages

    def build_body(self, request: ChatRequest, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        return body

    async def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[StreamEvent]:
        label = f"{self.name}:{model}"
        usage = Usage()
        text: list[str] = []
        pending: dict[int, dict[str, str]] = {}  # tool calls arrive in fragments, by index
        started = False
        try:
            async with self._client.stream(
                "POST", self._url, json=self.build_body(request, model), headers=self._headers
            ) as response:
                if not response.is_success:
                    error = (
                        f"OpenAI-compatible request failed ({response.status_code}): "
                        f"{_error_message(await response.aread())}"
                    )
                    if _for_status(response.status_code) is ChatUnavailable:
                        retry_after = _retry_after(response.headers.get("retry-after"))
                        raise ChatUnavailable(error, model=label, retry_after=retry_after)
                    raise ChatError(error, model=label)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError as exc:
                        raise ChatError("malformed stream event", model=label) from exc
                    if error := event.get("error"):
                        detail = f"provider error: {str(error)[:MAX_ERROR_CHARS]}"
                        raise ChatError(detail, model=label)
                    if event.get("usage"):
                        usage = Usage(
                            prompt_tokens=int(event["usage"].get("prompt_tokens") or 0),
                            completion_tokens=int(event["usage"].get("completion_tokens") or 0),
                        )
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if content := delta.get("content"):
                            text.append(content)
                            started = True
                            yield TextDelta(content)
                        for fragment in delta.get("tool_calls") or []:
                            slot = pending.setdefault(int(fragment.get("index", 0)), _empty_call())
                            slot["id"] = fragment.get("id") or slot["id"]
                            function = fragment.get("function") or {}
                            slot["name"] += function.get("name") or ""
                            slot["arguments"] += function.get("arguments") or ""
                            started = True
                        if choice.get("finish_reason") == "content_filter":
                            raise ChatError("the provider filtered the answer", model=label)
        except httpx.TimeoutException as exc:
            error = f"request timed out ({type(exc).__name__})"
            raise (ChatError if started else ChatUnavailable)(error, model=label) from exc
        except httpx.TransportError as exc:
            error = f"request failed ({type(exc).__name__})"
            raise (ChatError if started else ChatUnavailable)(error, model=label) from exc

        calls = []
        for index in sorted(pending):
            slot = pending[index]
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except ValueError as exc:
                error = f"tool call {slot['name']!r} had invalid JSON"
                raise ChatError(error, model=label) from exc
            call_id = slot["id"] or f"call_{index}"
            call = ToolCall(id=call_id, name=slot["name"], arguments=arguments)
            calls.append(call)
            yield ToolCallEvent(call)
        assistant = Message(role="assistant", text="".join(text), tool_calls=calls)
        yield Finish(usage=usage, model=label, assistant=assistant)

    async def aclose(self) -> None:
        await self._client.aclose()


def _empty_call() -> dict[str, str]:
    return {"id": "", "name": "", "arguments": ""}


def _for_status(status: int) -> type[ChatError]:
    """Overload, rate limits and server errors mean "try another model"."""
    return ChatUnavailable if status in RETRYABLE_STATUS else ChatError


def _retry_after(value: str | None) -> float | None:
    """Retry-After in seconds (the HTTP-date form is rare for APIs and ignored)."""
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _error_message(body: bytes) -> str:
    try:
        error = json.loads(body)["error"]
        return str(error.get("message") if isinstance(error, dict) else error)[:MAX_ERROR_CHARS]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "unknown error"
