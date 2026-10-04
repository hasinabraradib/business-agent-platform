"""Retrieval comparison: recall@k, MRR and latency per mode and language, and the relevance
threshold analysis. Uses embeddings and the reranker only, never the chat model."""

import statistics
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from evaluation.dataset import Question, chunk_key

MODES = ("vector", "keyword", "hybrid", "hybrid_rerank")
TOP_K = 5
SWEEP = (0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75)


@dataclass
class QuestionResult:
    id: str
    tenant: str
    language: str
    style: str
    answerable: bool
    mode: str
    expected: list[str]
    ranked: list[str]  # chunk keys, best first
    latency_ms: float  # retrieval time with the query embedding already cached
    top_similarity: float | None
    strong_keyword: bool
    rerank_applied: bool = False
    error: str | None = None

    @property
    def first_hit(self) -> int | None:
        for rank, key in enumerate(self.ranked, 1):
            if key in self.expected:
                return rank
        return None


async def run_retrieval(
    retriever, tenants: dict[str, uuid.UUID], questions: Iterable[Question],
    modes: Iterable[str] = MODES, top_k: int = TOP_K,
) -> list[QuestionResult]:  # fmt: skip
    """Every question in every mode. The query embedding is warmed first (and timed
    separately by the caller if it wants), so per-mode latency is search (+ rerank) only."""
    questions = list(questions)
    for q in questions:  # warm the embedding cache: latency below excludes the embedding call
        await retriever.retrieve(tenants[q.tenant], q.question, "vector", top_k)
    results = []
    for mode in modes:
        for q in questions:
            started = time.perf_counter()
            try:
                found = await retriever.retrieve(tenants[q.tenant], q.question, mode, top_k)
            except Exception as exc:  # recorded; the run goes on
                results.append(_failed(q, mode, f"{type(exc).__name__}: {exc}"))
                continue
            results.append(
                QuestionResult(
                    id=q.id, tenant=q.tenant, language=q.language, style=q.style,
                    answerable=q.answerable, mode=mode, expected=list(q.expected),
                    ranked=[chunk_key(c.metadata) for c in found.chunks],
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    top_similarity=found.top_vector_similarity,
                    strong_keyword=found.strong_keyword_match,
                    rerank_applied=found.rerank_applied,
                    error=found.rerank_error,
                )
            )  # fmt: skip
    return results


def _failed(q: Question, mode: str, error: str) -> QuestionResult:
    return QuestionResult(
        id=q.id, tenant=q.tenant, language=q.language, style=q.style, answerable=q.answerable,
        mode=mode, expected=list(q.expected), ranked=[], latency_ms=0.0, top_similarity=None,
        strong_keyword=False, error=error,
    )  # fmt: skip


def recall_at(results: list[QuestionResult], k: int) -> float | None:
    """Share of answerable questions with at least one expected chunk in the top k."""
    answerable = [r for r in results if r.answerable]
    if not answerable:
        return None
    return sum(1 for r in answerable if r.first_hit is not None and r.first_hit <= k) / len(
        answerable
    )


def mrr(results: list[QuestionResult]) -> float | None:
    answerable = [r for r in results if r.answerable]
    if not answerable:
        return None
    return sum(1 / r.first_hit for r in answerable if r.first_hit) / len(answerable)


def median_latency(results: list[QuestionResult]) -> float | None:
    values = [r.latency_ms for r in results if r.error is None or r.mode == "hybrid_rerank"]
    return round(statistics.median(values), 1) if values else None


def summarize(results: list[QuestionResult]) -> list[dict]:
    """One row per mode and language slice ('all', 'en', 'banglish', 'bn')."""
    rows = []
    for mode in dict.fromkeys(r.mode for r in results):
        in_mode = [r for r in results if r.mode == mode]
        for language in ("all", "en", "banglish", "bn"):
            subset = (
                in_mode if language == "all" else [r for r in in_mode if r.language == language]
            )
            if not subset:
                continue
            rows.append(
                {
                    "mode": mode,
                    "language": language,
                    "questions": sum(1 for r in subset if r.answerable),
                    "recall@3": _round(recall_at(subset, 3)),
                    "recall@5": _round(recall_at(subset, 5)),
                    "mrr": _round(mrr(subset)),
                    "median_ms": median_latency(subset),
                    "rerank_applied": sum(r.rerank_applied for r in subset)
                    if mode == "hybrid_rerank" else None,
                }
            )  # fmt: skip
    return rows


def threshold_sweep(
    results: list[QuestionResult], thresholds: Iterable[float] = SWEEP
) -> list[dict]:
    """The "has relevant context" gate (top vector similarity >= threshold, or a strong keyword
    match) at each threshold: unanswerable questions wrongly passed, answerable wrongly blocked.
    Uses one result per question (the hybrid run, else the first mode present)."""
    by_question: dict[str, QuestionResult] = {}
    for r in sorted(results, key=lambda r: r.mode != "hybrid"):
        by_question.setdefault(f"{r.tenant}:{r.id}", r)
    rows = []
    unanswerable = [r for r in by_question.values() if not r.answerable]
    answerable = [r for r in by_question.values() if r.answerable]
    for threshold in thresholds:
        wrongly_passed = [r.id for r in unanswerable if passes_gate(r, threshold)]
        wrongly_blocked = [r.id for r in answerable if not passes_gate(r, threshold)]
        rows.append(
            {
                "threshold": threshold,
                "unanswerable_passed": len(wrongly_passed),
                "unanswerable_total": len(unanswerable),
                "answerable_blocked": len(wrongly_blocked),
                "answerable_total": len(answerable),
                "passed_ids": wrongly_passed,
                "blocked_ids": wrongly_blocked,
            }
        )
    return rows


def passes_gate(result: QuestionResult, threshold: float) -> bool:
    return result.strong_keyword or (result.top_similarity or 0) >= threshold


def recommend_threshold(sweep: list[dict]) -> float | None:
    """Fewest total mistakes; on a tie the higher threshold (letting an unanswerable question
    through is worse than asking a customer to rephrase)."""
    if not sweep:
        return None
    best = min(
        sweep, key=lambda r: (r["unanswerable_passed"] + r["answerable_blocked"], -r["threshold"])
    )
    return best["threshold"]


def as_dicts(results: list[QuestionResult]) -> list[dict]:
    return [asdict(r) for r in results]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
