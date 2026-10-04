"""The chat turn: one tool-calling loop over a failover chain of chat models.

history + current message -> model (with tools) -> [tool calls -> tool results -> model]* ->
reply streamed through the outcome-tag and citation filters -> message stored.

The model decides when to search (search_knowledge, at most CHAT_MAX_SEARCHES per turn) and
writes its own queries. Every fact must be cited to a source found in this conversation; the
outcome is decided in code from what can be verified. Every run ends with exactly one DoneEvent
or ErrorEvent, so a stream never hangs.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.filters import (
    CitationFilter,
    ClosingFilter,
    OrderFactFilter,
    OutcomeTagFilter,
    PlainTextFilter,
    decide_outcome,
)
from app.chat.prompts import (
    ContextChunk,
    HistoryTurn,
    apology,
    contact_line,
    customer_block,
    history_user_block,
    is_identity_question,
    mentions_contact,
    new_nonce,
    reply_language,
    system_prompt,
    uses_bengali_script,
)
from app.chat.settings import TenantChatSettings
from app.chat.tools import Source, ToolRegistry, TurnContext, location
from app.handoff import acknowledgement
from app.llm import (
    ChatChain,
    ChatError,
    ChatRequest,
    Finish,
    Message,
    TextDelta,
    ToolCallEvent,
    Usage,
)
from app.models import Chunk, Conversation, Document, ToolCallRecord
from app.models import Message as StoredMessage
from app.tenancy import tenant_db

logger = logging.getLogger(__name__)

MAX_STORED_ERROR_CHARS = 2000
STAFF_SOURCE_TITLE = "Message from the team"
# Keeps references to detached "store the message" tasks started when a client disconnects.
_background: set[asyncio.Task] = set()


@dataclass
class ChatConfig:
    max_searches: int = 2
    max_tool_steps: int = 4  # model calls with tools per turn; one more call must answer
    history_messages: int = 6
    first_token_timeout_seconds: float = 25.0  # whole chain, per model call
    idle_timeout_seconds: float = 20.0
    total_timeout_seconds: float = 90.0
    max_output_tokens: int = 800
    temperature: float = 0.5


@dataclass(frozen=True)
class ChatTurnInput:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    message: str
    history: list[HistoryTurn]
    settings: TenantChatSettings
    earlier_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    visitor_id: str = ""
    # (message id, text) of recent team-member messages: citable as "Message from the team".
    staff_messages: list[tuple[uuid.UUID, str]] = field(default_factory=list)


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
    model: str | None = None
    conversation_status: str = "ai"  # waiting_human after a handoff


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
    reply: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    stored: bool = False
    context: TurnContext | None = None

    def ms_since(self, since: float) -> float:
        return round((time.perf_counter() - since) * 1000, 1)

    def add_usage(self, usage: Usage) -> None:
        self.usage = Usage(
            self.usage.prompt_tokens + usage.prompt_tokens,
            self.usage.completion_tokens + usage.completion_tokens,
        )


class ChatService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        chain: ChatChain,
        tools: ToolRegistry,
        config: ChatConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        limiter: Any = None,  # per-visitor tool limits (RateLimiter); None disables them
        queue: Any = None,  # webhook delivery jobs (JobQueue); None: deliveries stay queued
    ) -> None:
        self.sessionmaker = sessionmaker
        self.chain = chain
        self.tools = tools
        self.config = config or ChatConfig()
        self.clock = clock
        self.limiter = limiter
        self.queue = queue
        self.tools.max_searches = self.config.max_searches

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
        now = self.clock()
        context = TurnContext(
            turn.tenant_id,
            turn.settings,
            new_nonce(),
            conversation_id=turn.conversation_id,
            user_message_id=turn.user_message_id,
            visitor_id=turn.visitor_id,
            message=turn.message,
            now=now,
            sessionmaker=self.sessionmaker,
            limiter=self.limiter,
            queue=self.queue,
        )
        run.context = context
        earlier = await self._earlier_sources(turn, context)
        messages: list[Message] = []
        for past in turn.history:
            if past.role == "user":
                messages.append(Message("user", history_user_block(past.content, context.nonce)))
            else:
                messages.append(Message("assistant", past.content))
        current = customer_block(turn.message, context.nonce, earlier)
        messages.append(Message("user", current))
        system = system_prompt(
            turn.settings, now, context.nonce, self.tools.guidance(turn.settings)
        )

        tags = OutcomeTagFilter()
        citations = CitationFilter(context.valid_markers)  # grows as searches find sources
        plain = PlainTextFilter()
        closing = ClosingFilter()
        orders = OrderFactFilter(
            lambda: [
                r.result
                for r in context.records
                if r.tool == "lookup_order" and r.status == "ok" and "status " in r.result
            ]
        )
        prefer: str | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(self.config.total_timeout_seconds):
                for step in range(self.config.max_tool_steps + 1):
                    context.step = step + 1
                    exclude = (
                        frozenset({"search_knowledge"})
                        if len(context.searches) >= self.config.max_searches
                        else frozenset()
                    )
                    last = step == self.config.max_tool_steps
                    request = ChatRequest(
                        system=system,
                        messages=messages,
                        tools=[] if last else self.tools.specs(turn.settings, exclude=exclude),
                        temperature=self.config.temperature,
                        max_output_tokens=self.config.max_output_tokens,
                    )
                    since = time.perf_counter()
                    finish: Finish | None = None
                    calls = []
                    async for event in self._model_events(request, prefer):
                        if isinstance(event, TextDelta):
                            text = closing.feed(
                                orders.feed(citations.feed(plain.feed(tags.feed(event.text))))
                            )
                            if text:
                                if "first_token" not in run.timings:
                                    run.timings["first_token"] = run.ms_since(run.started)
                                run.reply.append(text)
                                yield TokenEvent(text)
                        elif isinstance(event, ToolCallEvent):
                            calls.append(event.call)
                        else:
                            finish = event
                    assert finish is not None
                    run.timings[f"model_{step + 1}"] = run.ms_since(since)
                    run.add_usage(finish.usage)
                    run.model = prefer = finish.model
                    if not calls:
                        break
                    messages.append(finish.assistant)
                    for call in calls:
                        messages.append(await self.tools.execute(context, call))
                    if context.handoff:
                        break  # the acknowledgement below is the whole reply
        except TimeoutError:
            error = "model timed out"
        except ChatError as exc:
            logger.warning("Answer generation failed: %s", exc)
            error = f"model error: {exc}"
            run.model = exc.model or run.model
        except Exception as exc:
            logger.exception("Chat turn failed")
            error = f"internal error: {type(exc).__name__}"
        if error is not None:
            async for event in self._fail(turn, run, error):
                yield event
            return

        tail = citations.feed(plain.feed(tags.flush()) + plain.flush()) + citations.flush()
        tail = orders.feed(tail) + orders.flush()
        tail = closing.feed(tail) + closing.flush()
        if context.handoff:
            # Written by code from the tenant's settings, not by the model: it can't invent a
            # reply time or office hours. Anything the model wrote first stays before it.
            ack = acknowledgement(turn.settings, reply_language(turn.message), now)
            tail = f"{tail}\n\n{ack}" if "".join(run.reply) or tail.strip() else ack
        if tail:
            if "first_token" not in run.timings:
                run.timings["first_token"] = run.ms_since(run.started)
            run.reply.append(tail)
            yield TokenEvent(tail)
        outcome = decide_outcome(
            tags.tag,
            citations.used,
            bool(context.searches),
            action=bool(context.actions),
            lookup=context.lookups > 0,
            # Collecting details: a proposal awaiting confirmation, or a call missing details.
            proposed=any(
                r.status in ("needs_confirmation", "invalid_arguments") for r in context.records
            ),
            handoff=context.handoff,
            identity=is_identity_question(turn.message),
        )
        contact = turn.settings.fallback_contact
        if outcome == "no_answer" and contact and not mentions_contact("".join(run.reply), contact):
            extra = contact_line(turn.settings, turn.message)
            run.reply.append(extra)
            yield TokenEvent(extra)
        reply = "".join(run.reply).strip()
        if tags.tag and tags.tag != outcome:
            logger.info("Outcome %s overrides the model's tag %s", outcome, tags.tag)
        checks = {
            "model_tag": tags.tag,
            "dropped_markers": citations.dropped,
            "script_matches": uses_bengali_script(reply) == uses_bengali_script(turn.message),
        }
        cited = [_citation(context.sources[n]) for n in citations.used]
        run.timings["total"] = run.ms_since(run.started)
        retrieval = self._retrieval_record(context, earlier, checks)
        message_id = await self._store(turn, run, reply, cited, outcome, None, retrieval)
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
            retrieval=retrieval,
            model=run.model,
            conversation_status="waiting_human" if context.handoff else "ai",
        )

    async def _model_events(self, request: ChatRequest, prefer: str | None):
        """The chain's events with a first-event timeout and an idle timeout."""
        events = aiter(self.chain.stream(request, prefer=prefer))
        timeout = self.config.first_token_timeout_seconds
        try:
            while True:
                try:
                    event = await asyncio.wait_for(anext(events), timeout)
                except StopAsyncIteration:
                    return
                timeout = self.config.idle_timeout_seconds
                yield event
        finally:
            await events.aclose()

    async def _earlier_sources(
        self, turn: ChatTurnInput, context: TurnContext
    ) -> list[ContextChunk]:
        """Chunks cited earlier in the conversation, and recent messages from the team, become
        citable sources [1..k] up front, so a follow-up can be answered from them without
        searching again (and an answer from the team's message is grounded, not no_answer)."""
        found = []
        for message_id, content in turn.staff_messages:
            source = Source(
                0, message_id, turn.conversation_id, STAFF_SOURCE_TITLE, {"staff_message": True},
                content,
            )  # fmt: skip
            found.append(ContextChunk(context.add_source(source), STAFF_SOURCE_TITLE, "", content))
        if not turn.earlier_chunk_ids:
            return found
        async with tenant_db(self.sessionmaker, turn.tenant_id) as db:
            chunks = await db.scalars(db.select(Chunk).where(Chunk.id.in_(turn.earlier_chunk_ids)))
            document_ids = {chunk.document_id for chunk in chunks}
            documents = await db.scalars(db.select(Document).where(Document.id.in_(document_ids)))
        titles = {document.id: document.title for document in documents}
        by_id = {chunk.id: chunk for chunk in chunks}
        for chunk_id in turn.earlier_chunk_ids:
            chunk = by_id.get(chunk_id)
            if chunk is None or chunk.document_id not in titles:
                continue  # deleted or re-ingested since: no longer citable
            source = Source(
                0, chunk.id, chunk.document_id, titles[chunk.document_id], chunk.meta, chunk.content
            )
            marker = context.add_source(source)
            found.append(
                ContextChunk(marker, source.document_title, location(chunk.meta), chunk.content)
            )
        return found

    def _retrieval_record(
        self, context: TurnContext, earlier: list[ContextChunk], checks: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "searches": [
                {
                    "query": s.query,
                    "relevant": s.relevant,
                    "chunk_ids": s.chunk_ids,
                    "markers": s.markers,
                    "top_similarity": s.top_similarity,
                    "strong_keyword_match": s.strong_keyword_match,
                    "duration_ms": s.duration_ms,
                    "embedding_cached": s.embedding_cached,
                }
                for s in context.searches
            ],
            "earlier_sources": [str(context.sources[c.marker].chunk_id) for c in earlier],
            "tools": [
                {
                    "step": r.step,
                    "tool": r.tool,
                    "status": r.status,
                    "summary": r.result_summary,
                    "arguments": r.arguments,
                    "result": r.result,
                }
                for r in context.records
            ],
            "actions": [
                {"tool": a.tool, "result_id": str(a.result_id), "summary": a.summary}
                for a in context.actions
            ],
            "checks": checks,
        }

    async def _fail(self, turn: ChatTurnInput, run: _Run, error: str) -> AsyncIterator[ChatEvent]:
        text = apology(turn.settings, turn.message)
        run.timings["total"] = run.ms_since(run.started)
        partial = "".join(run.reply)
        detail = f"{error}; {len(partial)} characters had been streamed" if partial else error
        retrieval = None
        if run.context is not None:
            retrieval = self._retrieval_record(run.context, [], {})
        message_id = None
        try:
            message_id = await self._store(turn, run, text, [], "error", detail, retrieval)
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
        retrieval: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        run.stored = True
        async with tenant_db(self.sessionmaker, turn.tenant_id) as db:
            message = StoredMessage(
                conversation_id=turn.conversation_id,
                in_reply_to=turn.user_message_id,
                role="assistant",
                content=content,
                citations=citations,
                outcome=outcome,
                model=run.model or self.chain.primary,
                prompt_tokens=run.usage.prompt_tokens or None,
                completion_tokens=run.usage.completion_tokens or None,
                timings=run.timings,
                retrieval=retrieval,
                error=error[:MAX_STORED_ERROR_CHARS] if error else None,
            )
            db.add(message)
            await db.flush()
            for record in run.context.records if run.context else []:
                db.add(
                    ToolCallRecord(
                        conversation_id=turn.conversation_id,
                        message_id=message.id,
                        step=record.step,
                        tool=record.tool,
                        arguments=record.arguments,
                        status=record.status,
                        result_summary=record.result_summary,
                        duration_ms=record.duration_ms,
                    )
                )
            conversation = await db.get(Conversation, turn.conversation_id)
            if conversation is not None:
                conversation.updated_at = func.now()  # newest-first conversation lists
            await db.flush()
            await db.commit()
            return message.id


def _citation(source: Source) -> dict[str, Any]:
    return {
        "marker": source.marker,
        "chunk_id": str(source.chunk_id),
        "document_id": str(source.document_id),
        "document_title": source.document_title,
        "metadata": source.metadata,
        "snippet": source.content[:240],
    }
