"""python -m evaluation {live,report,deterministic} (run from backend/).

live           real providers, budgeted and resumable (see evaluation/live.py)
report         print the latest results table; --update-readme refreshes the README block
deterministic  the CI tier (offline providers): runs tests/test_eval_suite.py
"""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from evaluation import report
from evaluation.chat import load_cases
from evaluation.dataset import RESULTS_DIR
from evaluation.state import State


def _latest_results() -> dict[str, Any]:
    files = sorted(p for p in RESULTS_DIR.glob("20*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else {}


def build_chat_section(config: dict[str, Any]) -> dict[str, Any]:
    from evaluation.live import _cases_summary

    cases = load_cases()
    state = State.load()
    return {
        "cases_summary": _cases_summary(cases, state),
        "groundedness": report.groundedness(state.cases),
        "spelling": report.spelling(state.cases),
        "latency_cost": report.latency_and_cost(
            state.cases, config["prices_usd_per_million_tokens"]
        ),
        "cases": report.cases_table(cases, state.cases),
        "usage_by_day": state.usage,
        "stops": state.stops,
    }


def render(results: dict[str, Any], config: dict[str, Any]) -> str:
    live = config["live"]
    parts = [f"Latest live evaluation: {results.get('date', 'never')}."]
    retrieval = results.get("retrieval")
    if retrieval:
        parts.append(
            f"**Retrieval** ({retrieval['questions']} labelled questions, "
            f"{retrieval['embedding_model']}; reranker {retrieval['reranker']}). recall@k = share "
            "of answerable questions with an expected chunk in the top k.\n\n"
            + report.retrieval_markdown(retrieval, live["relevance_threshold_in_use"])
            + f"\n\nRecommended threshold from this run: {retrieval['recommended_threshold']} "
            f"(in use: {live['relevance_threshold_in_use']}; not changed automatically)."
        )
    chat = results.get("chat")
    if chat:
        summary = chat["cases_summary"]
        partial = summary["done"] < summary["total"]
        label = (
            f"**PARTIAL: {summary['done']} of {summary['total']} cases run**"
            if partial
            else (f"all {summary['total']} cases run")
        )
        g = chat["groundedness"]
        lc = chat["latency_cost"]
        sp = chat["spelling"]
        parts.append(
            f"**Chat cases** ({label}): {summary['passed']} passed, "
            f"{len(summary['failed'])} failed"
            + (f"; pending: {', '.join(summary['pending'])}" if partial else "")
            + ".\n\n"
            + report.table(chat["cases"], ["case", "category", "result", "reason"])
        )
        parts.append(
            f"**Groundedness** (answered turns, checkable claims: prices, times, quantities, "
            f"dates, phones, statuses, delivery estimates): {g['fully_supported']} of "
            f"{g['answered_turns']} fully supported ({_pct(g['fully_supported_pct'])}); "
            f"{_pct(g['with_unsupported_pct'])} with an unsupported claim."
            + "".join(
                f'\n- {f["case"]}: "{f["reply"][:160]}" unsupported {f["unsupported"]}'
                for f in g["failures"]
            )
        )
        parts.append(
            f"**Bengali-script spelling** (suspected, lexicon-based): "
            f"{len(sp['suspected'])} in {sp['bengali_words']} words "
            f"({sp['per_100_words']} per 100 words) "
            f"over {sp['bengali_replies']} replies: {sp['suspected']}."
        )
        parts.append(
            f"**Latency and cost** ({lc['turns']} turns): time to first token median "
            f"{lc['ttft_median_s']} s, p95 {lc['ttft_p95_s']} s; {lc['avg_tokens_per_turn']} "
            f"tokens per turn; about ${lc['cost_per_100_conversations_usd']} per 100 "
            "conversations at published prices."
        )
    gemini = results.get("gemini_latency")
    if gemini:
        rows = [
            {"model": model, "samples": len(v["first_event_s"]), "median_s": v["median"],
             "p95_s": v["p95"], "within_3s": sum(1 for x in v["first_event_s"] if x <= 3.0),
             "errors": len(v["errors"])}
            for model, v in gemini.items()
        ]  # fmt: skip
        parts.append(
            "**Gemini fallback, time to first event** (real system prompt and tools):\n\n"
            + report.table(rows, ["model", "samples", "median_s", "p95_s", "within_3s", "errors"])
        )
    return "\n\n".join(parts)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("live", help="run the live tier (real providers)")
    live.add_argument("--keys-file", required=True, help='JSON {"restaurant": key, "shop": key}')
    live.add_argument("--max-cases", type=int, default=None)
    live.add_argument("--only", default="", help="comma-separated case ids")
    live.add_argument("--skip-retrieval", action="store_true")
    live.add_argument("--skip-chat", action="store_true")
    live.add_argument("--skip-gemini", action="store_true")
    live.add_argument("--dry-run", action="store_true", help="print the plan and estimate only")
    rep = sub.add_parser("report", help="print the latest results")
    rep.add_argument("--update-readme", action="store_true")
    sub.add_parser("deterministic", help="run the CI tier")
    args = parser.parse_args(argv)

    if args.command == "deterministic":
        return subprocess.call(
            [sys.executable, "-m", "pytest", "-q", "-s", "tests/test_eval_suite.py"]
        )
    from evaluation.live import load_config

    config = load_config()
    if args.command == "report":
        results = _latest_results()
        if not results:
            print("No live results yet: run `python -m evaluation live`.")
            return 1
        results["chat"] = build_chat_section(config)
        block = render(results, config)
        print(block)
        if args.update_readme:
            print("README updated." if report.update_readme(block) else "README markers missing.")
        return 0
    return asyncio.run(_live(args, config))


async def _live(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from app.config import get_settings
    from evaluation import live

    keys = json.loads(Path(args.keys_file).read_text(encoding="utf-8"))  # noqa: ASYNC240
    owner_url = get_settings().owner_database_url
    path = live.results_path()
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    results["date"] = date.today().isoformat()
    if not args.skip_retrieval and not args.dry_run:
        print("Retrieval: 100 questions x 4 modes (embeddings and reranker only)...")
        results["retrieval"] = await live.run_live_retrieval(owner_url)
        _save(path, results)
    if not args.skip_chat:
        only = {x.strip() for x in args.only.split(",") if x.strip()} or None
        batch = await live.run_chat_batch(
            keys, config, max_cases=args.max_cases or config["live"]["max_cases_per_run"],
            only=only, dry_run=args.dry_run, owner_url=owner_url,
        )  # fmt: skip
        results["last_batch"] = batch
    if not args.skip_gemini and not args.dry_run and "gemini_latency" not in results:
        print("Gemini fallback latency...")
        results["gemini_latency"] = await live.gemini_latency(
            config["live"]["gemini_latency_samples"]
        )
    if args.dry_run:
        return 0
    results["chat"] = build_chat_section(config)
    _save(path, results)
    print(render(results, config))
    return 0


def _save(path, results: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.dump(results), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
