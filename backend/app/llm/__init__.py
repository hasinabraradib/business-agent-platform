"""Chat models behind one interface, chained for failover.

CHAT_MODELS lists "provider:model" candidates in order (primary first). Providers: gemini,
openai_compat (any OpenAI-compatible endpoint, via OPENAI_COMPAT_*), fake (offline). When
OPENAI_COMPAT_MODEL is set and CHAT_MODELS has no openai_compat entry, it goes first: the
OpenAI-compatible model is primary and the CHAT_MODELS entries are its fallbacks. To add a
provider: subclass ChatProvider in this package and register it in PROVIDERS.
"""

import logging
from collections.abc import Callable

from pydantic_settings import BaseSettings, SettingsConfigDict

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
    ToolSpec,
    Usage,
)
from app.llm.chain import Candidate, ChatChain
from app.llm.fake import FakeChatProvider, Scripted
from app.llm.gemini import GeminiChatProvider
from app.llm.openai_compat import OpenAICompatChatProvider

__all__ = [
    "Candidate",
    "ChatChain",
    "ChatError",
    "ChatProvider",
    "ChatRequest",
    "ChatUnavailable",
    "FakeChatProvider",
    "Finish",
    "GeminiChatProvider",
    "LLMSettings",
    "Message",
    "OpenAICompatChatProvider",
    "Scripted",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallEvent",
    "ToolSpec",
    "Usage",
    "get_chat_chain",
]

logger = logging.getLogger(__name__)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # auto: the CHAT_MODELS chain (skipping providers without credentials), or the offline fake
    # model if none is usable. fake: always the offline model.
    chat_provider: str = "auto"
    chat_models: str = "gemini:gemini-3.8-flash,gemini:gemini-3.6-flash"
    chat_failover_seconds: float = 3.0
    chat_cooldown_seconds: float = 60.0
    gemini_api_key: str = ""
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    openai_compat_model: str = ""
    openai_compat_reasoning_effort: str = ""  # e.g. "low"; empty: not sent


PROVIDERS: dict[str, Callable[[LLMSettings], ChatProvider | None]] = {
    "gemini": lambda s: GeminiChatProvider(s.gemini_api_key) if s.gemini_api_key else None,
    "openai_compat": lambda s: (
        OpenAICompatChatProvider(
            s.openai_compat_base_url,
            s.openai_compat_api_key,
            reasoning_effort=s.openai_compat_reasoning_effort or None,
        )
        if s.openai_compat_base_url and s.openai_compat_api_key
        else None
    ),
}


def _entries(settings: LLMSettings) -> list[tuple[str, str]]:
    entries = []
    for raw in settings.chat_models.split(","):
        provider, _, model = raw.strip().partition(":")
        if provider and model:
            entries.append((provider.strip(), model.strip()))
    configured = settings.openai_compat_model and settings.openai_compat_base_url
    if configured and not any(p == "openai_compat" for p, _ in entries):
        entries.insert(0, ("openai_compat", settings.openai_compat_model))  # primary
    return entries


def get_chat_chain(settings: LLMSettings | None = None) -> ChatChain:
    settings = settings or LLMSettings()
    chain_options = {
        "first_event_timeout": settings.chat_failover_seconds,
        "cooldown_seconds": settings.chat_cooldown_seconds,
    }
    if settings.chat_provider.strip().lower() == "fake":
        return ChatChain([Candidate(FakeChatProvider(), "fake-chat")], **chain_options)
    providers: dict[str, ChatProvider | None] = {}
    candidates = []
    for provider_name, model in _entries(settings):
        if provider_name not in PROVIDERS:
            raise ValueError(f"Unknown chat provider {provider_name!r} in CHAT_MODELS")
        if provider_name not in providers:
            providers[provider_name] = PROVIDERS[provider_name](settings)
        if (provider := providers[provider_name]) is None:
            logger.warning("Skipping %s:%s (no credentials configured)", provider_name, model)
            continue
        candidates.append(Candidate(provider, model))
    if not candidates:
        logger.warning("No chat model credentials configured: using the offline fake model")
        return ChatChain([Candidate(FakeChatProvider(), "fake-chat")], **chain_options)
    return ChatChain(candidates, **chain_options)
