"""Provider-neutral chat types: messages with tool calls, streamed events, errors."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments object


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant
    tool_call_id: str | None = None  # tool
    tool_name: str | None = None  # tool
    # Provider-specific data needed to replay this message exactly (e.g. Gemini parts with
    # thought signatures), keyed by "<provider>:<model>".
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.4
    max_output_tokens: int = 800


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    call: ToolCall


@dataclass(frozen=True)
class Finish:
    """Always the last event of a successful stream."""

    usage: Usage
    model: str  # "<provider>:<model>" that actually answered
    assistant: Message  # the assistant message to append to the conversation for the next step


StreamEvent = TextDelta | ToolCallEvent | Finish


class ChatError(Exception):
    """The model call failed. The message is for logs and the stored error, not customers."""

    def __init__(self, message: str, *, model: str | None = None) -> None:
        super().__init__(message)
        self.model = model  # the model whose call failed (the last one tried, in a chain)


class ChatUnavailable(ChatError):
    """Overloaded, rate-limited or unreachable before anything was streamed: try another model."""


class ChatProvider(ABC):
    #: Short provider name ("gemini", "openai_compat", "fake").
    name: str
    #: True for the offline fake model (the widget shows a notice).
    offline: bool = False

    @abstractmethod
    def stream(self, request: ChatRequest, *, model: str) -> AsyncIterator[StreamEvent]:
        """Yield TextDelta / ToolCallEvent events, then exactly one Finish."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass
