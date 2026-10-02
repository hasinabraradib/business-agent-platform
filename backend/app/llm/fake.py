import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

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


@dataclass
class Scripted:
    """What the fake model does for one call."""

    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_after_chunks: int | None = None  # raise ChatError after streaming this many chunks
    delay: float = 0.0  # seconds before each chunk
    first_delay: float = 0.0  # seconds before the first event (slow-primary tests)
    unavailable: bool = False  # fail at once with ChatUnavailable (overload / rate limit)


Responder = Callable[[ChatRequest, str], "str | Scripted"]
GREETING = re.compile(
    r"^\s*(hi|hello|hey|thanks?|thank you|assalamu? ?alaikum|হ্যালো|ধন্যবাদ)\b", re.IGNORECASE
)


def last_user_text(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            return message.text.strip()
    return ""


def offline_responder(request: ChatRequest, model: str) -> str | Scripted:
    """Offline development behaviour: greet; search once for anything else; answer from the
    first passage found (with its citation) or say nothing was found."""
    last = request.messages[-1]
    if last.role == "tool":
        passage = re.search(r"^\[(\d+)\] [^\n]*\n([^\n]+)", last.text, re.M)
        if passage:
            return f"[[answered]]\nFrom what we have: {passage.group(2)} [{passage.group(1)}]"
        return "[[no_answer]]\nSorry, I couldn't find that in our information."
    text = last_user_text(request)
    if GREETING.match(text):
        return "[[smalltalk]]\nHello! How can I help you today?"
    if any(tool.name == "search_knowledge" for tool in request.tools):
        return Scripted(tool_calls=[("search_knowledge", {"query": text})])
    return "[[no_answer]]\nSorry, I couldn't find that in our information."


class FakeChatProvider(ChatProvider):
    """Deterministic, scripted chat model for tests and the offline demo."""

    name = "fake"
    offline = True

    def __init__(
        self, responder: Responder | None = None, chunk_size: int | None = None, name: str = "fake"
    ) -> None:
        self.responder = responder or offline_responder
        self.chunk_size = chunk_size  # None: stream word by word
        self.name = name
        self.calls: list[tuple[str, ChatRequest]] = []

    def _pieces(self, text: str) -> list[str]:
        if self.chunk_size:
            return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]
        return re.findall(r"\S+\s*|\s+", text)

    async def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[StreamEvent]:
        label = f"{self.name}:{model}"
        self.calls.append((model, request))
        script = self.responder(request, model)
        if isinstance(script, str):
            script = Scripted(script)
        if script.first_delay:
            await asyncio.sleep(script.first_delay)
        if script.unavailable:
            raise ChatUnavailable("fake model unavailable", model=label)
        pieces = self._pieces(script.text)
        for index, piece in enumerate(pieces):
            if script.fail_after_chunks is not None and index >= script.fail_after_chunks:
                raise ChatError("fake model failure", model=label)
            if script.delay:
                await asyncio.sleep(script.delay)
            yield TextDelta(piece)
        if script.fail_after_chunks is not None and script.fail_after_chunks >= len(pieces):
            raise ChatError("fake model failure", model=label)
        calls = [ToolCall(uuid.uuid4().hex[:12], name, args) for name, args in script.tool_calls]
        for call in calls:
            yield ToolCallEvent(call)
        prompt = request.system + "".join(m.text for m in request.messages)
        yield Finish(
            usage=Usage(len(prompt) // 4, max(1, len(script.text) // 4)),
            model=label,
            assistant=Message(role="assistant", text=script.text, tool_calls=calls),
        )

    def requests(self, model: str | None = None) -> list[ChatRequest]:
        return [request for called, request in self.calls if model is None or called == model]
