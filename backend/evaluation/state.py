"""Live-tier state (evals/results/live_state.json): finished chat cases and token usage per day.

Chat cases are resumable: each invocation runs at most N pending cases, finished ones are kept,
and the next invocation continues. A partial run is reported as partial, never as the result.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from evaluation.chat import Case
from evaluation.dataset import RESULTS_DIR

STATE_PATH = RESULTS_DIR / "live_state.json"


@dataclass
class State:
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)  # case id -> result
    usage: dict[str, dict[str, int]] = field(default_factory=dict)  # day -> requests, tokens
    stops: list[dict[str, str]] = field(default_factory=list)  # where runs stopped, and why

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "State":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(raw.get("cases", {}), raw.get("usage", {}), raw.get("stops", []))

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    def done(self, case_id: str) -> bool:
        return self.cases.get(case_id, {}).get("status") == "done"

    def used_today(self, today: str | None = None) -> dict[str, int]:
        return self.usage.get(today or date.today().isoformat(), {"requests": 0, "tokens": 0})

    def record_usage(self, requests: int, tokens: int, today: str | None = None) -> None:
        day = self.usage.setdefault(today or date.today().isoformat(), {"requests": 0, "tokens": 0})
        day["requests"] += requests
        day["tokens"] += tokens


@dataclass
class Plan:
    cases: list[Case]
    estimated_tokens: int
    skipped_for_budget: list[str]
    pending_total: int


def plan_run(
    cases: list[Case],
    state: State,
    *,
    max_cases: int,
    tokens_per_request: int,
    requests_per_turn: float,
    run_token_budget: int,
    daily_token_budget: int,
    today: str | None = None,
    only: set[str] | None = None,
) -> Plan:
    """Pending cases in file order, up to max_cases, while the estimate fits both the run budget
    and what is left of today's budget."""
    pending = [c for c in cases if not state.done(c.id) and (not only or c.id in only)]
    left_today = daily_token_budget - state.used_today(today)["tokens"]
    budget = min(run_token_budget, left_today)
    chosen, skipped, total = [], [], 0
    for case in pending:
        estimate = int(case.estimated_requests(requests_per_turn) * tokens_per_request)
        if len(chosen) >= max_cases:
            break
        if total + estimate > budget:
            skipped.append(case.id)
            continue
        chosen.append(case)
        total += estimate
    return Plan(chosen, total, skipped, len(pending))
