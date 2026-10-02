import hashlib
import math
import re
from collections.abc import Sequence

from app.embeddings.base import EmbeddingInput, EmbeddingProvider


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic, offline embeddings for tests and for running without an API key.

    A hashed bag of words plus character trigrams: each feature adds +-weight to a dimension
    chosen by its hash, then the vector is L2-normalized. Texts sharing words get similar
    vectors, and trigrams make related word forms ("shipping", "shipment") similar too, so
    vector search behaves plausibly (including paraphrase-ish matches keyword search misses).
    """

    model_name = "fake-hashing-768-v2"
    # Off-topic text scores near 0; on-topic overlap typically lands well above this.
    relevance_threshold = 0.25
    WORD_WEIGHT = 1.0
    TRIGRAM_WEIGHT = 0.5

    def _features(self, text: str) -> list[tuple[str, float]]:
        features = []
        for word in re.findall(r"\w+", text.lower()):
            features.append((f"w:{word}", self.WORD_WEIGHT))
            padded = f"#{word}#"
            features.extend(
                (f"g:{padded[i : i + 3]}", self.TRIGRAM_WEIGHT) for i in range(len(padded) - 2)
            )
        return features

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, weight in self._features(text):
            digest = hashlib.sha256(feature.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += weight if digest[4] & 1 else -weight
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]

    async def embed_documents(self, documents: Sequence[EmbeddingInput]) -> list[list[float]]:
        return [self._vector(f"{d.title or ''}\n{d.text}") for d in documents]

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(query)
