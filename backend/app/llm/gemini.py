"""Gemini chat over the REST API with server-sent events (no SDK dependency).

Models (checked 2026-10, https://ai.google.dev/gemini-api/docs/models):
- answers: gemini-3.8-flash, Google's current default general-purpose model;
- helper calls: gemini-3.5-flash-lite, the low-cost model the reranker also uses.
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.llm.base import ChatChunk, ChatError, ChatProvider, ChatRequest, Usage

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_ANSWER_MODEL = "gemini-3.8-flash"
DEFAULT_HELPER_MODEL = "gemini-3.5-flash-lite"
BLOCKING_FINISH_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}


class GeminiChatProvider(ChatProvider):
    def __init__(
        self,
        api_key: str,
        answer_model: str = DEFAULT_ANSWER_MODEL,
        helper_model: str = DEFAULT_HELPER_MODEL,
        *,
        answer_thinking_level: str | None = "LOW",
        helper_thinking_level: str | None = "MINIMAL",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiChatProvider needs an API key (GEMINI_API_KEY)")
        self.answer_model = answer_model
        self.helper_model = helper_model
        self._thinking = {answer_model: answer_thinking_level, helper_model: helper_thinking_level}
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
        url = f"{API_BASE}/models/{model}:streamGenerateContent?alt=sse"
        usage = Usage()
        try:
            async with self._client.stream(
                "POST", url, json=self.build_body(request, model), headers=self._headers
            ) as response:
                if not response.is_success:
                    raise ChatError(
                        f"Gemini chat request failed ({response.status_code}): "
                        f"{_error_message(await response.aread())}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:])
                    except ValueError as exc:
                        raise ChatError("Gemini sent a malformed stream event") from exc
                    text, usage_update = _parse_event(event)
                    if usage_update is not None:
                        usage = usage_update
                    if text:
                        yield ChatChunk(text=text)
        except httpx.TimeoutException as exc:
            raise ChatError(f"Gemini chat request timed out ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise ChatError(f"Gemini chat request failed ({type(exc).__name__})") from exc
        yield ChatChunk(usage=usage)

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
        return str(json.loads(body)["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return "unknown error"
