from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ChatTurn:
    role: Literal["user", "model"]
    text: str


@dataclass(frozen=True)
class ChatRequest:
    system: str
    turns: list[ChatTurn]
    temperature: float = 0.2
    max_output_tokens: int = 1024


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class ChatChunk:
    """A streamed piece of the reply. The final chunk carries usage (and usually no text)."""

    text: str = ""
    usage: Usage | None = None


@dataclass
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)


class ChatError(Exception):
    """The model call failed. The message is for logs and the stored error, not customers."""


class ChatProvider(ABC):
    #: Model for customer-facing answers.
    answer_model: str
    #: Low-cost model for small helper calls (e.g. rewriting follow-up questions).
    helper_model: str

    @abstractmethod
    def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[ChatChunk]:
        """Yield text chunks as they are generated, then a final chunk with usage."""

    async def complete(self, request: ChatRequest, *, model: str) -> Completion:
        text, usage = [], Usage()
        async for chunk in self.stream(request, model=model):
            text.append(chunk.text)
            if chunk.usage is not None:
                usage = chunk.usage
        return Completion("".join(text), usage)

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass
