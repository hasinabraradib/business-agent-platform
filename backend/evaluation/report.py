"""Turning results into numbers and tables: groundedness, spelling, latency, tokens and cost,
the retrieval comparison and the threshold analysis. Also refreshes the README's results block
(between the eval markers). Partial runs are labelled partial."""

import json
import re
import statistics
from typing import Any

from evaluation.chat import Case
from evaluation.dataset import REPO_ROOT
from evaluation.spelling import check_texts

README = REPO_ROOT / "README.md"
BEGIN = "<!-- eval-results:begin -->"
END = "<!-- eval-results:end -->"
ANSWERED = ("answered", "action")


def turns_of(state_cases: dict[str, dict]) -> list[tuple[str, dict]]:
    return [
        (case_id, turn)
        for case_id, result in state_cases.items()
        if result["status"] == "done"
        for turn in result["transcript"]["turns"]
    ]


def groundedness(state_cases: dict[str, dict]) -> dict[str, Any]:
    """Over answered turns (outcome answered or action): share with every checkable claim
    supported, share with an unsupported claim, and every failure."""
    answered = [(cid, t) for cid, t in turns_of(state_cases) if t["outcome"] in ANSWERED]
    failures = [
        {"case": cid, "say": t["say"], "reply": t["reply"], "unsupported": t["unsupported"]}
        for cid, t in answered
        if t["unsupported"]
    ]
    with_claims = [t for _, t in answered if t.get("unsupported") is not None]
    total = len(answered)
    return {
        "answered_turns": total,
        "fully_supported": total - len(failures),
        "fully_supported_pct": _pct(total - len(failures), total),
        "with_unsupported_pct": _pct(len(failures), total),
        "failures": failures,
        "checked_turns": len(with_claims),
    }


def spelling(state_cases: dict[str, dict]) -> dict[str, Any]:
    from app.chat.prompts import uses_bengali_script

    bengali = [t["reply"] for _, t in turns_of(state_cases) if uses_bengali_script(t["reply"])]
    report = check_texts(bengali)
    return {
        "bengali_replies": len(bengali),
        "bengali_words": report.words,
        "suspected": report.suspected,
        "per_100_words": report.per_100_words,
        "banglish_and_english": "not measured (no lexicon)",
    }


def latency_and_cost(state_cases: dict[str, dict], prices: dict[str, Any]) -> dict[str, Any]:
    turns = [t for _, t in turns_of(state_cases) if not t.get("silent") and t.get("model")]
    ttft = sorted(t["ttft_s"] for t in turns if t.get("ttft_s"))
    by_model: dict[str, dict[str, int]] = {}
    for t in turns:
        usage = by_model.setdefault(t["model"], {"turns": 0, "input": 0, "output": 0})
        usage["turns"] += 1
        usage["input"] += t["prompt_tokens"]
        usage["output"] += t["completion_tokens"]
    conversations = len({cid for cid, _ in turns_of(state_cases)})
    cost = 0.0
    priced = True
    for model, usage in by_model.items():
        price = prices.get(model)
        if price is None:
            priced = False
            continue
        cost += usage["input"] / 1e6 * price["input"] + usage["output"] / 1e6 * price["output"]
    tokens = sum(u["input"] + u["output"] for u in by_model.values())
    return {
        "turns": len(turns),
        "avg_tokens_per_turn": round(tokens / len(turns)) if turns else None,
        "ttft_median_s": round(statistics.median(ttft), 2) if ttft else None,
        "ttft_p95_s": ttft[min(len(ttft) - 1, round(0.95 * (len(ttft) - 1)))] if ttft else None,
        "by_model": by_model,
        "cost_per_100_conversations_usd": round(cost / conversations * 100, 4)
        if conversations and priced else None,
        "conversations": conversations,
    }  # fmt: skip


def cases_table(cases: list[Case], state_cases: dict[str, dict]) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        result = state_cases.get(case.id)
        if result is None:
            rows.append({"case": case.id, "category": case.category, "result": "pending"})
            continue
        failed = [c for c in result["checks"] if not c["passed"]]
        verdict = (
            "blocked" if result["status"] == "blocked" else "pass" if result["passed"] else "FAIL"
        )
        rows.append(
            {
                "case": case.id,
                "category": case.category,
                "result": verdict,
                "reason": "; ".join(f"{c['check']}: {c['reason']}" for c in failed)[:300],
            }
        )
    return rows


# --- markdown ----------------------------------------------------------------------------------


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _cell(value: Any) -> str:
    if value is None:
        return "\u2013"
    if isinstance(value, float):
        return f"{value:.2f}" if value < 1.5 else f"{value:g}"
    return str(value)


def table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(c)) for c in columns) + " |")
    return "\n".join(lines)


def retrieval_markdown(retrieval: dict[str, Any], threshold_in_use: float) -> str:
    rows = [r for r in retrieval["summary"] if r["language"] == "all"]
    by_language = [r for r in retrieval["summary"] if r["language"] != "all"]
    sweep = [
        {
            "threshold": r["threshold"] if r["threshold"] != threshold_in_use
            else f"**{r['threshold']}** (in use)",
            "unanswerable wrongly passed": f"{r['unanswerable_passed']}/{r['unanswerable_total']}",
            "answerable wrongly blocked": f"{r['answerable_blocked']}/{r['answerable_total']}",
        }
        for r in retrieval["sweep"]
    ]  # fmt: skip
    return "\n\n".join(
        [
            table(rows, ["mode", "questions", "recall@3", "recall@5", "mrr", "median_ms"]),
            "By language:\n\n"
            + table(by_language, ["mode", "language", "questions", "recall@3", "recall@5", "mrr"]),
            "Relevance threshold (the hybrid run's top vector similarity, or a strong keyword "
            "match):\n\n"
            + table(
                sweep, ["threshold", "unanswerable wrongly passed", "answerable wrongly blocked"]
            ),
        ]
    )


def update_readme(block: str) -> bool:
    text = README.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        return False
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    README.write_text(pattern.sub(lambda _: f"{BEGIN}\n{block}\n{END}", text), encoding="utf-8")
    return True


def dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)
