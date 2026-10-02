from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from app.models import EMBEDDING_DIMENSIONS


@dataclass(frozen=True)
class EmbeddingInput:
    """A document chunk to embed. The title gives the chunk context out of its document."""

    text: str
    title: str | None = None


class EmbeddingError(Exception):
    """Embedding failed permanently (after any retries). The message is safe to show tenants."""


class EmbeddingProvider(ABC):
    #: Stored with every chunk; vectors from different models must never be compared.
    model_name: str
    dimensions: int = EMBEDDING_DIMENSIONS
    #: Suggested minimum query-to-chunk cosine similarity for "relevant context". Similarity
    #: scales differ between models, so each provider suggests its own starting point.
    relevance_threshold: float = 0.5

    @abstractmethod
    async def embed_documents(self, documents: Sequence[EmbeddingInput]) -> list[list[float]]:
        """One vector per document, in order, each of length `dimensions`."""

    @abstractmethod
    async def embed_query(self, query: str) -> list[float]:
        """A vector for a search query, comparable with embed_documents output."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release network resources, if any."""
