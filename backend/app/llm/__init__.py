"""Chat models behind one interface, chosen by the CHAT_PROVIDER setting.

To add a provider: subclass ChatProvider in this package, add its settings to LLMSettings and
register a factory in PROVIDERS. Nothing outside this package changes.
"""

import logging
from collections.abc import Callable

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.base import (
    ChatChunk,
    ChatError,
    ChatProvider,
    ChatRequest,
    ChatTurn,
    Completion,
    Usage,
)
from app.llm.fake import FakeChatProvider, Scripted
from app.llm.gemini import DEFAULT_ANSWER_MODEL, DEFAULT_HELPER_MODEL, GeminiChatProvider

__all__ = [
    "ChatChunk",
    "ChatError",
    "ChatProvider",
    "ChatRequest",
    "ChatTurn",
    "Completion",
    "FakeChatProvider",
    "GeminiChatProvider",
    "LLMSettings",
    "Scripted",
    "Usage",
    "get_chat_provider",
]

logger = logging.getLogger(__name__)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # auto: gemini when GEMINI_API_KEY is set, otherwise the fake model. Or gemini | fake.
    chat_provider: str = "auto"
    gemini_api_key: str = ""
    chat_model: str = DEFAULT_ANSWER_MODEL
    helper_model: str = DEFAULT_HELPER_MODEL
    chat_thinking_level: str = "LOW"
    helper_thinking_level: str = "MINIMAL"


PROVIDERS: dict[str, Callable[[LLMSettings], ChatProvider]] = {
    "fake": lambda settings: FakeChatProvider(),
    "gemini": lambda settings: GeminiChatProvider(
        settings.gemini_api_key,
        settings.chat_model,
        settings.helper_model,
        answer_thinking_level=settings.chat_thinking_level or None,
        helper_thinking_level=settings.helper_thinking_level or None,
    ),
}


def get_chat_provider(settings: LLMSettings | None = None) -> ChatProvider:
    settings = settings or LLMSettings()
    name = settings.chat_provider.strip().lower()
    if name == "auto":
        name = "gemini" if settings.gemini_api_key else "fake"
        if name == "fake":
            logger.warning("No GEMINI_API_KEY set: using the fake chat model (not for production)")
    if name not in PROVIDERS:
        raise ValueError(f"Unknown CHAT_PROVIDER {name!r}; choose from {sorted(PROVIDERS)}")
    return PROVIDERS[name](settings)
