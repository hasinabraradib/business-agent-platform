"""Tools the chat model may call, behind a registry.

Rules every tool follows (enforced here, not only in the prompt):
- arguments are validated with a Pydantic model before anything runs; invalid arguments go back
  to the model as an error it can correct;
- only the tenant's enabled tools are offered, and only they can run;
- every call is recorded (arguments, status, result summary, duration) with the reply message;
- write tools go through the confirmation gate: the first call records a proposal and asks the
  model to read the details back; the write happens only when the same details were proposed in
  an earlier turn of this conversation and the customer's latest message confirms them;
- per-visitor limits stop spam on write tools and probing on lookups;
- tool output is data for the model, wrapped in nonce-delimited tags.
"""

import hashlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.confirm import is_confirmation
from app.chat.prompts import ContextChunk, search_results_block
from app.chat.settings import TenantChatSettings
from app.llm import Message, ToolCall, ToolSpec
from app.models import PendingAction
from app.tenancy import TenantDB, tenant_db

logger = logging.getLogger(__name__)

PROPOSAL_TTL = timedelta(minutes=30)


@dataclass
class SearchRecord:
    query: str
    relevant: bool
    chunk_ids: list[str]
    markers: list[int]
    top_similarity: float | None
    strong_keyword_match: bool
    timings_ms: dict[str, float]
    duration_ms: float
    embedding_cached: bool = False
    # For the trace view: how the search ran and every chunk it returned, with scores.
    mode: str = ""
    threshold: float | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    reranker: str | None = None
    rerank_applied: bool = False


@dataclass
class Source:
    marker: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    metadata: dict[str, Any]
    content: str


MAX_RECORDED_RESULT_CHARS = 4000


@dataclass
class ToolRecord:
    step: int
    tool: str
    arguments: dict[str, Any]
    status: str
    result_summary: str
    duration_ms: float
    result: str = ""  # what the model saw (truncated), so replies can be checked against it


@dataclass
class ActionDone:
    tool: str
    result_id: uuid.UUID
    summary: str


@dataclass
class TurnContext:
    """State shared by the tools during one customer turn."""

    tenant_id: uuid.UUID
    settings: TenantChatSettings
    nonce: str
    conversation_id: uuid.UUID | None = None
    user_message_id: uuid.UUID | None = None
    visitor_id: str = ""
    message: str = ""  # the customer's current message
    now: datetime | None = None
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    limiter: Any = None  # app.chat.ratelimit.RateLimiter
    queue: Any = None  # app.ingestion.queue.JobQueue (webhook deliveries)
    sources: dict[int, Source] = field(default_factory=dict)  # citation marker -> source
    searches: list[SearchRecord] = field(default_factory=list)
    valid_markers: set[int] = field(default_factory=set)  # shared with the citation filter
    records: list[ToolRecord] = field(default_factory=list)
    actions: list[ActionDone] = field(default_factory=list)  # writes completed this turn
    lookups: int = 0  # successful record lookups this turn (e.g. an order found)
    handoff: bool = False  # request_human ran: the turn ends with the code-written acknowledgement
    step: int = 0

    def add_source(self, chunk) -> int:
        """Register a retrieved chunk as citable; returns its marker (reused if known)."""
        for marker, source in self.sources.items():
            if source.chunk_id == chunk.chunk_id:
                return marker
        marker = len(self.sources) + 1
        self.sources[marker] = Source(
            marker,
            chunk.chunk_id,
            chunk.document_id,
            chunk.document_title,
            chunk.metadata,
            chunk.content,
        )
        self.valid_markers.add(marker)
        return marker

    def db(self):
        assert self.sessionmaker is not None, "this tool needs database access"
        return tenant_db(self.sessionmaker, self.tenant_id)


@dataclass(frozen=True)
class ToolResult:
    content: str  # what the model sees
    status: str = "ok"
    summary: str = ""  # for the tool-call record (defaults to the content, shortened)


def _score(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def location(metadata: dict[str, Any]) -> str:
    if "row" in metadata:
        return f"row {metadata['row']}"
    if section := metadata.get("section"):
        return str(section)
    if "page" in metadata:
        return f"page {metadata['page']}"
    return ""


def tool_result_block(name: str, body: str, nonce: str) -> str:
    return f"<tool-result-{nonce}>\n{body}\n</tool-result-{nonce}>"


def _clean_schema(node: Any) -> Any:
    """Pydantic JSON Schema -> the plain subset both Gemini and OpenAI accept: no titles,
    formats or additionalProperties, nested models inlined, and optional fields as their non-null
    type (optionality comes from 'required')."""
    if isinstance(node, dict):
        if "anyOf" in node:
            branches = [b for b in node["anyOf"] if b.get("type") != "null"]
            if len(branches) == 1:
                merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **branches[0]}
                return _clean_schema(merged)
        dropped = ("title", "default", "format", "additionalProperties")
        return {k: _clean_schema(v) for k, v in node.items() if k not in dropped}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Tool(ABC):
    name: ClassVar[str]
    args_model: ClassVar[type[ToolArgs]]
    writes: ClassVar[bool] = False
    #: (calls, window seconds) per visitor, or None.
    rate_limit: ClassVar[tuple[int, int] | None] = None

    @abstractmethod
    def description(self, settings: TenantChatSettings) -> str: ...

    def guidance(self, settings: TenantChatSettings) -> str:
        """Extra instructions for the system prompt when this tool is enabled."""
        return ""

    def spec(self, settings: TenantChatSettings) -> ToolSpec:
        schema = _inline_refs(self.args_model.model_json_schema())
        return ToolSpec(self.name, self.description(settings), _clean_schema(schema))

    @abstractmethod
    async def run(self, context: TurnContext, args: Any) -> ToolResult: ...


class SearchArgs(ToolArgs):
    query: str = Field(
        min_length=1, max_length=300, description="Short search keywords, e.g. 'Kacchi price'."
    )


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    args_model = SearchArgs

    def __init__(self, retriever, mode: str = "hybrid", top_k: int = 8) -> None:
        self.retriever = retriever
        self.mode = mode
        self.top_k = top_k

    def description(self, settings: TenantChatSettings) -> str:
        return (
            f"Search {settings.business_name}'s own information (menu or products, prices, "
            "opening hours, location, policies, delivery, FAQ). Returns numbered passages "
            "to cite. Use it only when you need a fact about the business."
        )

    async def run(self, context: TurnContext, args: SearchArgs) -> ToolResult:
        started = time.perf_counter()
        result = await self.retriever.retrieve(context.tenant_id, args.query, self.mode, self.top_k)
        relevant = result.has_relevant_context or result.strong_keyword_match
        chunks = result.chunks if relevant else []
        markers = [context.add_source(chunk) for chunk in chunks]
        context.searches.append(
            SearchRecord(
                query=args.query,
                relevant=bool(chunks),
                chunk_ids=[str(c.chunk_id) for c in result.chunks],
                markers=markers,
                top_similarity=result.top_vector_similarity,
                strong_keyword_match=result.strong_keyword_match,
                timings_ms=result.timings_ms,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                embedding_cached=result.embedding_cached,
                mode=result.mode,
                threshold=result.relevance_threshold,
                results=[
                    {
                        "chunk_id": str(c.chunk_id),
                        "document_title": c.document_title,
                        "location": location(c.metadata),
                        "snippet": c.content[:200],
                        "vector_score": _score(c.vector_score),
                        "keyword_score": _score(c.keyword_score),
                        "fused_score": _score(c.fused_score),
                        "rerank_score": _score(c.rerank_score),
                    }
                    for c in result.chunks
                ],
                reranker=result.reranker,
                rerank_applied=result.rerank_applied,
            )
        )
        cited = [
            ContextChunk(m, context.sources[m].document_title,
                         location(context.sources[m].metadata), context.sources[m].content)
            for m in markers
        ]  # fmt: skip
        summary = f"{len(markers)} relevant passages" if markers else "nothing relevant"
        return ToolResult(search_results_block(cited, context.nonce), summary=summary)


@dataclass(frozen=True)
class WriteOutcome:
    summary: str  # for the model (and the pending action / record)
    result_id: uuid.UUID
    event_type: str | None = None
    event_data: dict[str, Any] | None = None


class WriteTool(Tool):
    """A tool that changes something: confirmation-gated and idempotent."""

    writes = True
    rate_limit = (6, 3600)

    @abstractmethod
    def identity(self, args: Any) -> dict[str, Any]:
        """The normalized details that must match between proposal and confirmation."""

    async def check(self, context: TurnContext, args: Any) -> str | None:
        """Business rules; a returned string is a refusal explained to the model."""
        return None

    @abstractmethod
    def read_back(self, context: TurnContext, args: Any) -> str: ...

    @abstractmethod
    async def execute(
        self, context: TurnContext, db: TenantDB, args: Any, pending: PendingAction
    ) -> WriteOutcome: ...

    def _result(
        self, context: TurnContext, text: str, status: str, summary: str = ""
    ) -> ToolResult:
        return ToolResult(
            tool_result_block(self.name, text, context.nonce), status, summary or text
        )

    def done_message(self, outcome: WriteOutcome) -> str:
        return outcome.summary

    async def run(self, context: TurnContext, args: Any) -> ToolResult:
        if refusal := await self.check(context, args):
            return ToolResult(
                tool_result_block(self.name, refusal, context.nonce), "refused", refusal
            )
        identity = self.identity(args)
        args_hash = hashlib.sha256(
            json.dumps({"tool": self.name, **identity}, sort_keys=True, default=str).encode()
        ).hexdigest()
        now = context.now
        assert now is not None
        async with context.db() as db:
            pending = await db.scalar(
                db.select(PendingAction)
                .where(
                    PendingAction.conversation_id == context.conversation_id,
                    PendingAction.tool == self.name,
                    PendingAction.args_hash == args_hash,
                )
                .order_by(PendingAction.created_at.desc())
            )
            if pending is not None and pending.status == "done":
                context.lookups += 1  # an existing result, reported again
                text = (
                    f"Already done earlier in this conversation; do NOT do it again. "
                    f"{pending.result_summary}"
                )
                return self._result(context, text, "ok")
            fresh = pending is not None and now - pending.updated_at <= PROPOSAL_TTL
            confirmed = (
                fresh
                and pending.proposed_in != context.user_message_id
                and is_confirmation(context.message)
            )
            if confirmed:
                try:
                    outcome = await self.execute(context, db, args, pending)
                    pending.status = "done"
                    pending.result_id = outcome.result_id
                    pending.result_summary = outcome.summary
                    delivery_id = None
                    if outcome.event_type:
                        from app.webhooks.events import record_event

                        delivery_id = await record_event(
                            db, outcome.event_type, outcome.event_data or {}
                        )
                    await db.commit()
                except IntegrityError:
                    # A concurrent confirmation already executed this proposal.
                    await db.rollback()
                    return self._result(context, "Already done; do NOT do it again.", "ok")
                if delivery_id is not None and context.queue is not None:
                    try:
                        await context.queue.enqueue_webhook(context.tenant_id, delivery_id)
                    except Exception:
                        logger.exception("Could not enqueue webhook delivery %s", delivery_id)
                context.actions.append(ActionDone(self.name, outcome.result_id, outcome.summary))
                text = self.done_message(outcome)
                return ToolResult(
                    tool_result_block(self.name, text, context.nonce), "ok", outcome.summary
                )
            # Propose (or re-propose): nothing is written until a later turn confirms.
            if pending is not None and pending.status == "pending":
                pending.arguments = args.model_dump(mode="json")
                pending.proposed_in = context.user_message_id
                pending.updated_at = now
            else:
                db.add(
                    PendingAction(
                        conversation_id=context.conversation_id,
                        tool=self.name,
                        arguments=args.model_dump(mode="json"),
                        args_hash=args_hash,
                        proposed_in=context.user_message_id,
                        status="pending",
                        updated_at=now,  # freshness is judged on the same clock as `now`
                    )
                )
            await db.commit()
        details = self.read_back(context, args)
        text = (
            f"NOT DONE YET. Read these details back to the customer and ask them to confirm: "
            f"{details}. Only after they confirm in their next message, call {self.name} again "
            "with exactly the same details."
        )
        return ToolResult(
            tool_result_block(self.name, text, context.nonce), "needs_confirmation", details
        )


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, max_searches: int = 2) -> None:
        self._tools: dict[str, Tool] = {}
        self.max_searches = max_searches
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def enabled(self, settings: TenantChatSettings) -> list[Tool]:
        return [tool for name, tool in self._tools.items() if name in settings.enabled_tools]

    def specs(
        self, settings: TenantChatSettings, *, exclude: frozenset[str] = frozenset()
    ) -> list[ToolSpec]:
        return [t.spec(settings) for t in self.enabled(settings) if t.name not in exclude]

    def guidance(self, settings: TenantChatSettings) -> list[str]:
        return [g for t in self.enabled(settings) if (g := t.guidance(settings))]

    async def execute(self, context: TurnContext, call: ToolCall) -> Message:
        started = time.perf_counter()
        tool = self.get(call.name)
        status, summary = "ok", ""
        if tool is None or call.name not in context.settings.enabled_tools:
            content = f"There is no tool named {call.name!r} available here."
            status = "not_allowed"
        elif call.name == SearchKnowledgeTool.name and len(context.searches) >= self.max_searches:
            content = "Search limit reached for this message. Answer with what you already have."
            status = "refused"
        else:
            try:
                args = tool.args_model.model_validate(call.arguments)
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                    for e in exc.errors()
                )
                content = (
                    f"Invalid arguments for {call.name}: {problems}. Nothing was done. Fix the "
                    "arguments, or ask the customer for the missing or unclear details."
                )
                status = "invalid_arguments"
            else:
                limited = await self._rate_limited(context, tool)
                if limited:
                    contact = context.settings.fallback_contact
                    content = (
                        f"Too many {call.name} requests from this customer right now. Nothing "
                        f"was done. Apologise and offer the contact: {contact}"
                    )
                    status = "rate_limited"
                else:
                    try:
                        result = await tool.run(context, args)
                    except Exception:
                        logger.exception("Tool %s failed", call.name)
                        content = (
                            f"{call.name} failed because of a technical problem. Nothing was done. "
                            f"Apologise and offer the contact: {context.settings.fallback_contact}"
                        )
                        status = "error"
                    else:
                        content, status, summary = result.content, result.status, result.summary
        context.records.append(
            ToolRecord(
                step=context.step,
                tool=call.name,
                arguments=call.arguments,
                status=status,
                result_summary=(summary or content)[:500],
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                result=content[:MAX_RECORDED_RESULT_CHARS],
            )
        )
        return Message(role="tool", text=content, tool_call_id=call.id, tool_name=call.name)

    async def _rate_limited(self, context: TurnContext, tool: Tool) -> bool:
        if tool.rate_limit is None or context.limiter is None:
            return False
        calls, window = tool.rate_limit
        name = f"tool:{tool.name}:{context.tenant_id}:{context.visitor_id}"
        return await context.limiter.window(name, calls, window) is not None
