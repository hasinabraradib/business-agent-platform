import hashlib
import math
import re
from collections.abc import Sequence

from app.embeddings.base import EmbeddingInput, EmbeddingProvider


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, offline embeddings for tests and for running without an API key.

    Feature hashing: each word adds +-1 to a dimension chosen by its hash, then the vector is
    L2-normalized. Texts sharing words get similar vectors, so retrieval behaves plausibly.
    """

    model_name = "fake-hashing-768"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]

    async def embed_documents(self, documents: Sequence[EmbeddingInput]) -> list[list[float]]:
        return [self._vector(f"{d.title or ''}\n{d.text}") for d in documents]

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(query)
