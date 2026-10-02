"""Tools the chat model may call, behind a small registry.

A tool declares a name, a description and a JSON Schema for its arguments, and runs with a
TurnContext. The registry is the extension point for future tools (reservations, order
lookup, lead capture): register another Tool and the loop offers it to the model.
"""

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.chat.prompts import ContextChunk, search_results_block
from app.chat.settings import TenantChatSettings
from app.llm import ToolSpec
from app.retrieval import Retriever


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


@dataclass
class Source:
    marker: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    metadata: dict[str, Any]
    content: str


@dataclass
class TurnContext:
    """State shared by the tools during one customer turn."""

    tenant_id: uuid.UUID
    settings: TenantChatSettings
    nonce: str
    sources: dict[int, Source] = field(default_factory=dict)  # citation marker -> source
    searches: list[SearchRecord] = field(default_factory=list)
    valid_markers: set[int] = field(default_factory=set)  # shared with the citation filter

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


@dataclass(frozen=True)
class ToolResult:
    content: str  # what the model sees


class Tool(ABC):
    name: str

    @abstractmethod
    def spec(self, settings: TenantChatSettings) -> ToolSpec: ...

    @abstractmethod
    async def run(self, context: TurnContext, arguments: dict[str, Any]) -> ToolResult: ...


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"

    def __init__(self, retriever: Retriever, mode: str = "hybrid", top_k: int = 8) -> None:
        self.retriever = retriever
        self.mode = mode
        self.top_k = top_k

    def spec(self, settings: TenantChatSettings) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"Search {settings.business_name}'s own information (menu or products, prices, "
                "opening hours, location, policies, delivery, FAQ). Returns numbered passages "
                "to cite. Use it only when you need a fact about the business."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Short search keywords, e.g. 'Kacchi Biryani price'.",
                    }
                },
                "required": ["query"],
            },
        )

    async def run(self, context: TurnContext, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()[:300]
        if not query:
            return ToolResult("The search query was empty.")
        started = time.perf_counter()
        result = await self.retriever.retrieve(context.tenant_id, query, self.mode, self.top_k)
        relevant = result.has_relevant_context or result.strong_keyword_match
        chunks = result.chunks if relevant else []
        markers = [context.add_source(chunk) for chunk in chunks]
        context.searches.append(
            SearchRecord(
                query=query,
                relevant=bool(chunks),
                chunk_ids=[str(c.chunk_id) for c in result.chunks],
                markers=markers,
                top_similarity=result.top_vector_similarity,
                strong_keyword_match=result.strong_keyword_match,
                timings_ms=result.timings_ms,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                embedding_cached=result.embedding_cached,
            )
        )
        cited = [
            ContextChunk(marker, context.sources[marker].document_title,
                         _location(context.sources[marker].metadata),
                         context.sources[marker].content)
            for marker in markers
        ]  # fmt: skip
        return ToolResult(search_results_block(cited, context.nonce))


def _location(metadata: dict[str, Any]) -> str:
    if "row" in metadata:
        return f"row {metadata['row']}"
    if section := metadata.get("section"):
        return str(section)
    if "page" in metadata:
        return f"page {metadata['page']}"
    return ""


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(
        self, settings: TenantChatSettings, *, exclude: frozenset[str] = frozenset()
    ) -> list[ToolSpec]:
        return [tool.spec(settings) for name, tool in self._tools.items() if name not in exclude]

    @property
    def names(self) -> list[str]:
        return list(self._tools)
