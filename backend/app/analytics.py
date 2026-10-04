"""Numbers for the dashboard: the overview, and knowledge gaps grouped by similarity.

Knowledge gaps are customer questions the assistant could not answer (outcome no_answer) that
nobody has answered from the dashboard yet. Similar questions are grouped by word overlap after
normalising (case, punctuation, Bengali digits, filler words), which is free and deterministic;
no embeddings are spent on it.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.tenancy import TenantDB

# Published prices, USD per million tokens (checked 2026-10-04: console.groq.com/docs/models,
# ai.google.dev/gemini-api/docs/pricing). Unknown models are left out of the estimate.
PRICES: dict[str, tuple[float, float]] = {
    "openai_compat:openai/gpt-oss-120b": (0.15, 0.60),
    "gemini:gemini-3.8-flash": (0.75, 3.75),
    "gemini:gemini-3.6-flash": (0.75, 3.75),
}
OUTCOMES = ("answered", "smalltalk", "no_answer", "action", "handoff", "error")
WINDOW_DAYS = 7

STOPWORDS = {
    # English
    "a", "an", "the", "is", "are", "do", "does", "you", "your", "i", "me", "my", "we", "can",
    "to", "of", "for", "in", "on", "at", "and", "or", "it", "this", "that", "what", "how",
    "please", "any", "have", "has", "there", "be", "will", "would",
    # Banglish
    "ki", "ache", "achhe", "apnara", "apnader", "apni", "amar", "ami", "er", "ta", "koto", "na",
    "hobe", "den", "kivabe", "kothay",
    # Bengali
    "কি", "কী", "আছে", "আপনারা", "আপনাদের", "আপনি", "আমি", "আমার", "কত", "না", "হবে",
}  # fmt: skip
BN_DIGITS = str.maketrans("".join(chr(0x09E6 + i) for i in range(10)), "0123456789")
WORD = re.compile("[\\w" + chr(0x0980) + "-" + chr(0x09FF) + "]+")
SIMILARITY = 0.5


def tokens(question: str) -> frozenset[str]:
    words = WORD.findall(question.translate(BN_DIGITS).casefold())
    return frozenset(w for w in words if w not in STOPWORDS and len(w) > 1)


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return a == b
    return len(a & b) / len(a | b) >= SIMILARITY


@dataclass
class Gap:
    question: str
    asked_at: datetime
    message_id: uuid.UUID  # the assistant's no_answer reply
    conversation_id: uuid.UUID


@dataclass
class GapGroup:
    question: str  # the newest wording
    last_asked_at: datetime
    count: int = 0
    message_ids: list[uuid.UUID] = field(default_factory=list)
    conversation_ids: list[uuid.UUID] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    _tokens: frozenset[str] = frozenset()


def group_gaps(gaps: list[Gap], max_examples: int = 3) -> list[GapGroup]:
    """Greedy grouping, newest first: a question joins the first group it resembles."""
    groups: list[GapGroup] = []
    for gap in sorted(gaps, key=lambda g: g.asked_at, reverse=True):
        words = tokens(gap.question)
        group = next((g for g in groups if similar(words, g._tokens)), None)
        if group is None:
            group = GapGroup(gap.question, gap.asked_at, _tokens=words)
            groups.append(group)
        group.count += 1
        group.message_ids.append(gap.message_id)
        if gap.conversation_id not in group.conversation_ids:
            group.conversation_ids.append(gap.conversation_id)
        if gap.question not in group.examples and len(group.examples) < max_examples:
            group.examples.append(gap.question)
    return groups


async def open_gaps(db: TenantDB, limit: int = 500) -> list[Gap]:
    rows = await db.execute_sql(
        text(
            "SELECT a.id, a.conversation_id, a.created_at, q.content AS question "
            "FROM messages a JOIN messages q "
            "  ON q.tenant_id = a.tenant_id AND q.id = a.in_reply_to "
            "WHERE a.tenant_id = :tenant_id AND a.role = 'assistant' "
            "  AND a.outcome = 'no_answer' AND a.gap_closed_at IS NULL "
            "ORDER BY a.created_at DESC LIMIT :limit"
        ),
        {"limit": limit},
    )
    return [Gap(r.question, r.created_at, r.id, r.conversation_id) for r in rows]


def estimate_cost(tokens_by_model: dict[str, tuple[int, int]]) -> float:
    total = 0.0
    for model, (prompt, completion) in tokens_by_model.items():
        price = PRICES.get(model)
        if price:
            total += prompt / 1e6 * price[0] + completion / 1e6 * price[1]
    return round(total, 6)


async def overview(db: TenantDB, timezone: str) -> dict[str, Any]:
    """The last 7 days (and today) in the business's own timezone."""
    params = {"tz": timezone, "days": WINDOW_DAYS}
    window = (
        "created_at >= (date_trunc('day', now() AT TIME ZONE :tz) - make_interval(days => "
        ":days - 1)) AT TIME ZONE :tz"
    )
    today = "created_at >= date_trunc('day', now() AT TIME ZONE :tz) AT TIME ZONE :tz"
    counts = (
        await db.execute_sql(
            text(
                f"SELECT count(*) FILTER (WHERE {today}) AS today, count(*) AS week, "
                "count(*) FILTER (WHERE handoff_requested_at IS NOT NULL) AS handed "
                f"FROM conversations WHERE tenant_id = :tenant_id AND {window}"
            ),
            params,
        )
    ).one()
    daily = await db.execute_sql(
        text(
            "SELECT to_char(created_at AT TIME ZONE :tz, 'YYYY-MM-DD') AS day, count(*) AS n "
            f"FROM conversations WHERE tenant_id = :tenant_id AND {window} "
            "GROUP BY 1 ORDER BY 1"
        ),
        params,
    )
    outcomes = await db.execute_sql(
        text(
            "SELECT outcome, count(*) AS n FROM messages WHERE tenant_id = :tenant_id "
            f"AND role = 'assistant' AND outcome IS NOT NULL AND {window} GROUP BY outcome"
        ),
        params,
    )
    stats = (
        await db.execute_sql(
            text(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "(timings->>'first_token')::float) AS median_ttft "
                "FROM messages WHERE tenant_id = :tenant_id AND role = 'assistant' "
                f"AND timings ? 'first_token' AND {window}"
            ),
            params,
        )
    ).one()
    usage = await db.execute_sql(
        text(
            "SELECT model, coalesce(sum(prompt_tokens), 0) AS p, "
            "coalesce(sum(completion_tokens), 0) AS c FROM messages "
            f"WHERE tenant_id = :tenant_id AND role = 'assistant' AND {window} GROUP BY model"
        ),
        params,
    )
    tokens_by_model = {r.model or "unknown": (int(r.p), int(r.c)) for r in usage}
    by_outcome = {o: 0 for o in OUTCOMES}
    by_outcome.update({r.outcome: r.n for r in outcomes})
    gaps = (await open_gaps(db, limit=5))[:5]
    return {
        "timezone": timezone,
        "window_days": WINDOW_DAYS,
        "conversations_today": counts.today,
        "conversations_week": counts.week,
        "daily": [{"day": r.day, "conversations": r.n} for r in daily],
        "outcomes": by_outcome,
        "handoff_rate": round(counts.handed / counts.week, 4) if counts.week else None,
        "median_first_token_ms": round(stats.median_ttft, 1) if stats.median_ttft else None,
        "tokens": {
            "prompt": sum(p for p, _ in tokens_by_model.values()),
            "completion": sum(c for _, c in tokens_by_model.values()),
            "by_model": {
                m: {"prompt": p, "completion": c} for m, (p, c) in tokens_by_model.items()
            },
        },
        "estimated_cost_usd": estimate_cost(tokens_by_model),
        "recent_gaps": [
            {
                "question": g.question,
                "asked_at": g.asked_at,
                "conversation_id": g.conversation_id,
                "message_id": g.message_id,
            }
            for g in gaps
        ],
    }
