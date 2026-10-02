"""The answer pipeline behind POST /v1/chat.

history -> follow-up rewrite (helper model, only with history) -> retrieve (hybrid, top 8) ->
relevance (similarity threshold OR strong exact keyword match) -> generate from the retrieved
chunks only -> stream through the outcome-tag and citation filters -> store the message.

Every run ends with exactly one DoneEvent or ErrorEvent, so a stream never hangs.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.filters import CitationFilter, OutcomeTagFilter, decide_outcome
from app.chat.prompts import (
    ContextChunk,
    HistoryTurn,
    answer_request,
    apology,
    clean_rewrite,
    contact_line,
    context_chunks,
    rewrite_request,
    uses_bengali_script,
)
from app.chat.settings import TenantChatSettings
from app.llm import ChatProvider, Usage
from app.models import Conversation, Message
from app.retrieval import Retriever
from app.tenancy import tenant_db

logger = logging.getLogger(__name__)

# Keeps references to detached "store the message" tasks started when a client disconnects.
_background: set[asyncio.Task] = set()


@dataclass
class ChatConfig:
    retrieval_mode: str = "hybrid"  # "hybrid_rerank" switches the reranker on
    top_k: int = 8
    history_messages: int = 6
    rewrite_timeout_seconds: float = 6.0
    first_token_timeout_seconds: float = 25.0
    idle_timeout_seconds: float = 20.0
    total_timeout_seconds: float = 90.0
    max_output_tokens: int = 800


@dataclass(frozen=True)
class ChatTurnInput:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    message: str
    history: list[HistoryTurn]
    settings: TenantChatSettings


@dataclass(frozen=True)
class TokenEvent:
    text: str


@dataclass(frozen=True)
class CitationsEvent:
    citations: list[dict[str, Any]]


@dataclass(frozen=True)
class DoneEvent:
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    outcome: str
    reply: str
    usage: dict[str, int]
    timings: dict[str, float]
    retrieval: dict[str, Any]


@dataclass(frozen=True)
class ErrorEvent:
    message: str  # customer-facing apology with the fallback contact
    message_id: uuid.UUID | None
    conversation_id: uuid.UUID


ChatEvent = TokenEvent | CitationsEvent | DoneEvent | ErrorEvent


@dataclass
class _Run:
    started: float = field(default_factory=time.perf_counter)
    timings: dict[str, float] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    reply: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stored: bool = False

    def mark(self, stage: str, since: float) -> None:
        self.timings[stage] = round((time.perf_counter() - since) * 1000, 1)


class ChatService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: ChatProvider,
        retriever: Retriever,
        config: ChatConfig | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.provider = provider
        self.retriever = retriever
        self.config = config or ChatConfig()

    async def respond(self, turn: ChatTurnInput) -> AsyncIterator[ChatEvent]:
        run = _Run()
        try:
            async for event in self._respond(turn, run):
                yield event
        except asyncio.CancelledError:
            # Client went away mid-stream: still record what happened, without blocking.
            if not run.stored:
                task = asyncio.create_task(
                    self._store(turn, run, "".join(run.reply), [], "error", "client disconnected")
                )
                _background.add(task)
                task.add_done_callback(_background.discard)
            raise

    async def _respond(self, turn: ChatTurnInput, run: _Run) -> AsyncIterator[ChatEvent]:
        # 1. Follow-up rewriting, only when there is history.
        query, rewrite_info = turn.message, {"rewritten": False}
        if turn.history:
            since = time.perf_counter()
            query, rewrite_info = await self._rewrite(turn)
            run.mark("rewrite", since)

        # 2. Retrieval and the relevance decision.
        since = time.perf_counter()
        try:
            result = await self.retriever.retrieve(
                turn.tenant_id, query, self.config.retrieval_mode, self.config.top_k
            )
        except Exception as exc:
            logger.exception("Retrieval failed")
            run.mark("retrieve", since)
            async for event in self._fail(turn, run, f"retrieval failed: {type(exc).__name__}"):
                yield event
            return
        run.mark("retrieve", since)
        relevant = result.has_relevant_context or result.strong_keyword_match
        provided = result.chunks if relevant else []
        chunks = context_chunks(provided)
        run.retrieval = {
            "mode": result.mode,
            "query": query,
            **rewrite_info,
            "chunk_ids": [str(c.chunk_id) for c in result.chunks],
            "provided_chunk_ids": [str(c.chunk_id) for c in provided],
            "top_similarity": result.top_vector_similarity,
            "threshold": result.relevance_threshold,
            "has_relevant_context": result.has_relevant_context,
            "strong_keyword_match": result.strong_keyword_match,
            "relevant": relevant,
            "embedding_cached": result.embedding_cached,
            "timings_ms": result.timings_ms,
        }

        # 3. Generation, streamed through the filters.
        request = answer_request(
            turn.settings,
            turn.history,
            turn.message,
            chunks,
            max_output_tokens=self.config.max_output_tokens,
        )
        tags, citations = OutcomeTagFilter(), CitationFilter({c.marker for c in chunks})
        since = time.perf_counter()
        error = None
        try:
            async with asyncio.timeout(self.config.total_timeout_seconds):
                stream = aiter(self.provider.stream(request, model=self.provider.answer_model))
                first = True
                while True:
                    timeout = (
                        self.config.first_token_timeout_seconds
                        if first
                        else self.config.idle_timeout_seconds
                    )
                    try:
                        chunk = await asyncio.wait_for(anext(stream), timeout)
                    except StopAsyncIteration:
                        break
                    if chunk.usage is not None:
                        run.usage = chunk.usage
                    text = citations.feed(tags.feed(chunk.text))
                    if text:
                        if first:
                            run.mark("first_token", run.started)
                            first = False
                        run.reply.append(text)
                        yield TokenEvent(text)
        except TimeoutError:
            error = "model timed out"
        except Exception as exc:  # ChatError and anything unexpected
            logger.warning("Answer generation failed: %s", exc)
            error = f"model error: {exc}"
        if error is not None:
            run.mark("generate", since)
            async for event in self._fail(turn, run, error):
                yield event
            return

        tail = citations.feed(tags.flush()) + citations.flush()
        if tail:
            run.reply.append(tail)
            yield TokenEvent(tail)
        outcome = decide_outcome(tags.tag, citations.used, bool(chunks))
        # Rule check: a "don't know" reply always offers the business's contact.
        contact = turn.settings.fallback_contact
        if outcome == "no_answer" and contact and contact not in "".join(run.reply):
            extra = contact_line(turn.settings, turn.message)
            run.reply.append(extra)
            yield TokenEvent(extra)
        run.mark("generate", since)
        reply = "".join(run.reply).strip()
        run.retrieval["checks"] = {
            "model_tag": tags.tag,
            "dropped_markers": citations.dropped,
            "script_matches": uses_bengali_script(reply) == uses_bengali_script(turn.message),
        }
        if tags.tag and tags.tag != outcome:
            logger.info("Outcome %s overrides the model's tag %s", outcome, tags.tag)

        cited = [_citation(chunks[n - 1], provided[n - 1]) for n in citations.used]
        run.timings["total"] = round((time.perf_counter() - run.started) * 1000, 1)
        message_id = await self._store(turn, run, reply, cited, outcome, None)
        yield CitationsEvent(cited)
        yield DoneEvent(
            message_id=message_id,
            conversation_id=turn.conversation_id,
            outcome=outcome,
            reply=reply,
            usage={
                "prompt_tokens": run.usage.prompt_tokens,
                "completion_tokens": run.usage.completion_tokens,
            },
            timings=run.timings,
            retrieval=run.retrieval,
        )

    async def _rewrite(self, turn: ChatTurnInput) -> tuple[str, dict[str, Any]]:
        try:
            completion = await asyncio.wait_for(
                self.provider.complete(
                    rewrite_request(turn.history, turn.message), model=self.provider.helper_model
                ),
                self.config.rewrite_timeout_seconds,
            )
        except Exception as exc:
            reason = "timed out" if isinstance(exc, TimeoutError) else str(exc)
            logger.warning("Follow-up rewrite failed, searching the original message: %s", reason)
            return turn.message, {"rewritten": False, "rewrite_error": reason}
        query = clean_rewrite(completion.text)
        earlier = {t.content.strip().casefold() for t in turn.history if t.role == "user"}
        repeated = (
            query is not None
            and query.casefold() in earlier
            and query.casefold() != turn.message.strip().casefold()
        )
        if repeated:
            # A known failure mode: the helper answers with a previous question instead of
            # rewriting the new one (seen with an injection attempt as the new message).
            query = None
        info: dict[str, Any] = {
            "rewritten": query is not None,
            "original_message": turn.message,
            "rewrite_model": self.provider.helper_model,
            "rewrite_tokens": completion.usage.prompt_tokens + completion.usage.completion_tokens,
        }
        if query is None:
            info["rewrite_error"] = (
                "rewrite repeated an earlier message" if repeated else "unusable rewrite output"
            )
            return turn.message, info
        return query, info

    async def _fail(self, turn: ChatTurnInput, run: _Run, error: str) -> AsyncIterator[ChatEvent]:
        text = apology(turn.settings, turn.message)
        run.timings["total"] = round((time.perf_counter() - run.started) * 1000, 1)
        partial = "".join(run.reply)
        detail = f"{error}; {len(partial)} characters had been streamed" if partial else error
        message_id = None
        try:
            message_id = await self._store(turn, run, text, [], "error", detail)
        except Exception:
            logger.exception("Could not store the failed message")
        yield ErrorEvent(message=text, message_id=message_id, conversation_id=turn.conversation_id)

    async def _store(
        self,
        turn: ChatTurnInput,
        run: _Run,
        content: str,
        citations: list[dict[str, Any]],
        outcome: str,
        error: str | None,
    ) -> uuid.UUID:
        run.stored = True
        async with tenant_db(self.sessionmaker, turn.tenant_id) as db:
            message = Message(
                conversation_id=turn.conversation_id,
                role="assistant",
                content=content,
                citations=citations,
                outcome=outcome,
                model=self.provider.answer_model,
                prompt_tokens=run.usage.prompt_tokens or None,
                completion_tokens=run.usage.completion_tokens or None,
                timings=run.timings,
                retrieval=run.retrieval or None,
                error=error,
            )
            db.add(message)
            conversation = await db.get(Conversation, turn.conversation_id)
            if conversation is not None:
                conversation.updated_at = func.now()  # newest-first conversation lists
            await db.flush()
            await db.commit()
            return message.id


def _citation(chunk: ContextChunk, retrieved) -> dict[str, Any]:
    return {
        "marker": chunk.marker,
        "chunk_id": str(retrieved.chunk_id),
        "document_id": str(retrieved.document_id),
        "document_title": retrieved.document_title,
        "metadata": retrieved.metadata,
        "snippet": retrieved.content[:240],
    }
