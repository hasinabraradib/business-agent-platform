from functools import lru_cache

from app.chat.actions import (
    CaptureLeadTool,
    CreateReservationTool,
    LookupOrderTool,
    QueryCatalogTool,
    RequestHumanTool,
)
from app.chat.ratelimit import RateLimiter
from app.chat.service import ChatConfig, ChatService
from app.chat.tools import SearchKnowledgeTool, ToolRegistry
from app.config import get_settings
from app.db import get_sessionmaker
from app.ingestion.queue import get_job_queue
from app.llm import get_chat_chain
from app.retrieval import get_retriever


@lru_cache
def get_chat_service() -> ChatService:
    settings = get_settings()
    tools = ToolRegistry(
        [
            SearchKnowledgeTool(get_retriever(), settings.chat_retrieval_mode, settings.chat_top_k),
            QueryCatalogTool(),
            CreateReservationTool(),
            LookupOrderTool(),
            CaptureLeadTool(),
            RequestHumanTool(),
        ],
        max_searches=settings.chat_max_searches,
    )
    return ChatService(
        sessionmaker=get_sessionmaker(),
        chain=get_chat_chain(),
        tools=tools,
        config=ChatConfig(
            max_searches=settings.chat_max_searches,
            history_messages=settings.chat_history_messages,
            first_token_timeout_seconds=settings.chat_first_token_timeout_seconds,
            total_timeout_seconds=settings.chat_total_timeout_seconds,
        ),
        limiter=get_rate_limiter(),
        queue=get_job_queue(),
    )


@lru_cache
def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_settings().redis_url)
