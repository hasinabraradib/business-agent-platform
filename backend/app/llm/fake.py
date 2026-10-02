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

DEV_NO_ANSWER = (
    "[[no_answer]]\n(Offline demo model) I couldn't find that in our information. "
    "Set CHAT_PROVIDER=gemini with a GEMINI_API_KEY for real answers."
)
GREETING = re.compile(r"^(hi|hello|hey|thanks?|thank you|assalamu? ?alaikum|হ্যালো|ধন্যবাদ)\b", re.I)


def _default_responder(request: ChatRequest, model: str) -> str:
    """Offline development replies: greet, quote the best passage with a citation, or say it
    does not know. Good enough to exercise the widget end to end without an API key."""
    prompt = request.turns[-1].text
    message = re.search(r"<customer-message-(\w+)>\n(.*)\n</customer-message-\1>", prompt, re.S)
    text = message.group(2).strip() if message else prompt.strip().splitlines()[-1]
    if model == FakeChatProvider.helper_model:
        return text
    if GREETING.match(text):
        return "[[smalltalk]]\nHello! (Offline demo model) How can I help you today?"
    passage = re.search(r"^\[1\] [^\n]*\n([^\n]+)", prompt, re.M)
    if passage:
        return f"[[answered]]\n(Offline demo model) From our information: {passage.group(1)} [1]"
    return DEV_NO_ANSWER


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
