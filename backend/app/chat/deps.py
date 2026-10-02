from functools import lru_cache

from app.chat.ratelimit import RateLimiter
from app.chat.service import ChatConfig, ChatService
from app.config import get_settings
from app.db import get_sessionmaker
from app.llm import get_chat_provider
from app.retrieval import get_retriever


@lru_cache
def get_chat_service() -> ChatService:
    settings = get_settings()
    return ChatService(
        sessionmaker=get_sessionmaker(),
        provider=get_chat_provider(),
        retriever=get_retriever(),
        config=ChatConfig(
            retrieval_mode=settings.chat_retrieval_mode,
            top_k=settings.chat_top_k,
            history_messages=settings.chat_history_messages,
            first_token_timeout_seconds=settings.chat_first_token_timeout_seconds,
            total_timeout_seconds=settings.chat_total_timeout_seconds,
        ),
    )


@lru_cache
def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_settings().redis_url)
