import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from app.llm.base import ChatChunk, ChatError, ChatProvider, ChatRequest, Usage


@dataclass
class Scripted:
    """What the fake model does for one call."""

    text: str
    fail_after_chunks: int | None = None  # raise ChatError after streaming this many chunks
    delay: float = 0.0  # seconds to wait before each chunk (for timeout tests)


Responder = Callable[[ChatRequest, str], "str | Scripted"]

DEV_REPLY = (
    "[[no_answer]]\n(This is the offline fake chat model. Set GEMINI_API_KEY for real answers.)"
)


def _default_responder(request: ChatRequest, model: str) -> str:
    if model == FakeChatProvider.helper_model:
        return request.turns[-1].text.strip().splitlines()[-1]
    return DEV_REPLY


class FakeChatProvider(ChatProvider):
    """Deterministic, scripted chat model for tests (and running without an API key)."""

    answer_model = "fake-chat"
    helper_model = "fake-chat-helper"

    def __init__(self, responder: Responder | None = None, chunk_size: int | None = None) -> None:
        self.responder = responder or _default_responder
        self.chunk_size = chunk_size  # None: stream word by word
        self.calls: list[tuple[str, ChatRequest]] = []

    def _pieces(self, text: str) -> list[str]:
        if self.chunk_size:
            return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]
        return re.findall(r"\S+\s*|\s+", text)

    async def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[ChatChunk]:
        self.calls.append((model, request))
        script = self.responder(request, model)
        if isinstance(script, str):
            script = Scripted(script)
        for index, piece in enumerate(self._pieces(script.text)):
            if script.fail_after_chunks is not None and index >= script.fail_after_chunks:
                raise ChatError("fake model failure", model=model)
            if script.delay:
                await asyncio.sleep(script.delay)
            yield ChatChunk(text=piece)
        if script.fail_after_chunks is not None and script.fail_after_chunks >= len(
            self._pieces(script.text)
        ):
            raise ChatError("fake model failure", model=model)
        prompt = request.system + "".join(t.text for t in request.turns)
        yield ChatChunk(usage=Usage(len(prompt) // 4, max(1, len(script.text) // 4)), model=model)

    def calls_to(self, model: str) -> list[ChatRequest]:
        return [request for called, request in self.calls if called == model]
