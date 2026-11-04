"""Run the agent stress cases against the running stack and write the report.

    uv run --project backend python tests/agent_eval/run.py                  # everything
    uv run --project backend python tests/agent_eval/run.py --category booking --case oos-wifi
    uv run --project backend python tests/agent_eval/run.py --dry-run        # plan + estimate

Fresh keys are created for every run (an admin key with the CLI, a widget key with it) and
revoked at the end; they are never printed or written to disk. Spend is recorded in
results/spend.json and a run refuses to start if its estimate would exceed the budget in
config.json.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime

import httpx
from harness import (
    CATEGORIES,
    HERE,
    REPO,
    TIMEZONE,
    TurnPacer,
    dhaka_today,
    grade,
    load_cases,
    percentile,
    run_cases,
    turn_cost,
)

RESULTS = HERE / "results"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
ORIGIN = "http://localhost:8080"  # one of the demo tenant's allowed_origins
TENANT = "demo-restaurant"


def env_value(name: str) -> str:
    """From the environment, else the repo's git-ignored .env (never printed)."""
    if os.environ.get(name):
        return os.environ[name]
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


# --- keys --------------------------------------------------------------------------------------


def cli_admin_key() -> str:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migrate",
            "python",
            "-m",
            "app.cli",
            "create-key",
            "--tenant",
            TENANT,
            "--kind",
            "admin",
            "--label",
            "agent-eval",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.search(r"bap_admin_[A-Za-z0-9_-]+", out)
    if not match:
        sys.exit("Could not create an admin key with the CLI (is the stack up?).")
    return match.group(0)


async def make_keys(client: httpx.AsyncClient) -> dict[str, str]:
    admin = cli_admin_key()
    headers = {"Authorization": f"Bearer {admin}"}
    response = await client.post(
        "/v1/api-keys", json={"kind": "widget", "label": "agent-eval"}, headers=headers
    )
    response.raise_for_status()
    return {"admin": admin, "widget": response.json()["key"]}


async def revoke_keys(client: httpx.AsyncClient, keys: dict) -> None:
    headers = {"Authorization": f"Bearer {keys['admin']}"}
    listed = (await client.get("/v1/api-keys", headers=headers)).json()
    mine = [k for k in listed if k.get("label") == "agent-eval" and not k.get("revoked_at")]
    # Widget key first; the admin key last (it is the one making the calls).
    for key in sorted(mine, key=lambda k: k["kind"] == "admin"):
        await client.delete(f"/v1/api-keys/{key['id']}", headers=headers)


# --- settings and chunks -----------------------------------------------------------------------


def settings_text(tenant: dict) -> str:
    s = tenant.get("settings") or {}
    hours = s.get("opening_hours") or {}
    return "\n".join(
        [
            f"Business: {s.get('business_name')} (timezone {s.get('timezone')})",
            f"Contact line: {s.get('fallback_contact')}",
            "Opening hours (online bookings; last booking 30 min before closing): "
            + "; ".join(
                f"{d} {', '.join('-'.join(r) for r in ranges) or 'closed'}"
                for d, ranges in hours.items()
            ),
            f"Online bookings: at most {s.get('max_online_party_size')} people",
            f"Tools enabled: {', '.join(s.get('enabled_tools') or [])}",
        ]
    )


def chunk_fetcher():
    sys.path.insert(0, str(REPO / "backend"))
    os.chdir(REPO / "backend")  # Settings reads the repo's .env relative to backend/
    from app.config import get_settings
    from evaluation.live import chunk_fetcher as fetcher

    return fetcher(get_settings().owner_database_url)


# --- budget ------------------------------------------------------------------------------------


def spend_ledger() -> dict:
    path = RESULTS / "spend.json"
    return json.loads(path.read_text()) if path.exists() else {"total_usd": 0.0, "runs": []}


def estimate(cases) -> dict:
    est = CONFIG["estimate"]
    turns = sum(len(c.turns) for c in cases)
    a_price = CONFIG["assistant_prices"][CONFIG["assistant_model"]]
    j_price = CONFIG["judge_prices"][CONFIG["judge_model"]]
    assistant = (
        turns
        * est["requests_per_turn"]
        * (
            est["input_tokens_per_request"] * a_price["input"]
            + est["output_tokens_per_request"] * a_price["output"]
        )
        / 1e6
    )
    judge = (
        len(cases)
        * (
            est["judge_input_tokens"] * j_price["input"]
            + est["judge_output_tokens"] * j_price["output"]
        )
        / 1e6
    )
    return {
        "cases": len(cases),
        "turns": turns,
        "assistant_usd": round(assistant, 3),
        "judge_usd": round(judge, 3),
        "total_usd": round(assistant + judge, 3),
    }


# --- main --------------------------------------------------------------------------------------


async def main(args) -> int:
    cases = load_cases()
    if args.category or args.case:  # either filter selects a case
        unknown = set(args.case or []) - {c.id for c in cases}
        if unknown:
            sys.exit(f"Unknown case ids: {', '.join(sorted(unknown))}")
        wanted_categories, wanted_ids = set(args.category or []), set(args.case or [])
        cases = [c for c in cases if c.category in wanted_categories or c.id in wanted_ids]
    if not cases:
        sys.exit("No cases match the filters.")
    today = dhaka_today()
    cases = [c.resolved(today) for c in cases]

    plan = estimate(cases)
    if args.no_judge:
        plan["total_usd"] = plan["assistant_usd"]
    ledger = spend_ledger()
    budget = CONFIG["budget_usd_total"]
    print(
        f"Plan: {plan['cases']} cases, {plan['turns']} turns; estimate ${plan['total_usd']:.2f} "
        f"(assistant ${plan['assistant_usd']:.2f}, judge ${plan['judge_usd']:.2f}); "
        f"spent so far ${ledger['total_usd']:.2f} of ${budget:.2f}."
    )
    if ledger["total_usd"] + plan["total_usd"] > budget:
        sys.exit("Refusing to run: the estimate would exceed the budget in config.json.")
    if args.dry_run:
        return 0

    openai_key = env_value("OPENAI_API_KEY")
    if not args.no_judge and not openai_key:
        sys.exit("OPENAI_API_KEY is not set (environment or .env); use --no-judge to skip it.")

    from judge import Judge

    from evaluation.chat import Session

    pacer = TurnPacer(CONFIG["chat_turns_per_minute"])
    async with httpx.AsyncClient(
        base_url=args.api, timeout=180, event_hooks={"request": [pacer]}
    ) as client:
        if (await client.get("/health")).status_code != 200:
            sys.exit(f"The API at {args.api} is not healthy; start the stack first.")
        keys = await make_keys(client)
        fetch = chunk_fetcher()
        started = time.time()
        try:
            tenant = (
                await client.get("/v1/tenant", headers={"Authorization": f"Bearer {keys['admin']}"})
            ).json()
            business = settings_text(tenant)
            session = Session(
                client,
                keys["admin"],
                settings_text=business,
                chat_headers={"Authorization": f"Bearer {keys['widget']}", "Origin": ORIGIN},
            )
            now_text = datetime.now(TIMEZONE).strftime("%A %d %B %Y, %H:%M (Asia/Dhaka)")
            judge = (
                None
                if args.no_judge
                else Judge(openai_key, CONFIG["judge_model"], business, now_text)
            )
            done = 0

            def progress(case, transcript):
                nonlocal done
                done += 1
                print(f"  [{done}/{len(cases)}] {case.id}", flush=True)

            print(f"Running {len(cases)} cases (concurrency {args.concurrency})...")
            transcripts = await run_cases(
                cases, session, fetch, concurrency=args.concurrency, on_done=progress
            )
            verdicts = {}
            if judge:
                print("Judging...")
                semaphore = asyncio.Semaphore(args.concurrency)

                async def judge_one(case):
                    async with semaphore:
                        verdicts[case.id] = await judge.grade(case, transcripts[case.id])

                await asyncio.gather(*(judge_one(c) for c in cases))
        finally:
            await revoke_keys(client, keys)
            await fetch.engine.dispose()

    results = assemble(cases, transcripts, verdicts, now_text, started, args)
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"run-{stamp}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    ledger["total_usd"] = round(ledger["total_usd"] + results["summary"]["cost_usd"]["total"], 4)
    ledger["runs"].append({"file": path.name, "usd": results["summary"]["cost_usd"]["total"]})
    (RESULTS / "spend.json").write_text(json.dumps(ledger, indent=1))

    from report import markdown_summary, write_html

    write_html(results, HERE / "report.html")
    print(markdown_summary(results))
    print(f"\nResults: {path.relative_to(REPO)}; report: tests/agent_eval/report.html")
    return 0 if results["summary"]["pass_rate"] >= 0.95 else 1


def assemble(cases, transcripts, verdicts, now_text, started, args) -> dict:
    prices = CONFIG["assistant_prices"]
    j_price = CONFIG["judge_prices"][CONFIG["judge_model"]]
    rows = []
    for case in cases:
        t = transcripts[case.id]
        g = grade(case, t)
        v = verdicts.get(case.id)
        turn_costs = [turn_cost(x, prices) for x in t.turns]
        judge_cost = (
            ((v.prompt_tokens * j_price["input"] + v.completion_tokens * j_price["output"]) / 1e6)
            if v
            else 0.0
        )
        deterministic_pass = g.passed
        judge_pass = v.passed if v else None
        rows.append(
            {
                "id": case.id,
                "category": case.category,
                "turns": case.turns,
                "expect": case.expect,
                "rubric": case.rubric,
                "transcript": asdict(t),
                "checks": [asdict(c) for c in g.checks],
                "booking_calls": g.booking_calls,
                "deterministic_pass": deterministic_pass,
                "judge": v.as_dict() if v else None,
                "judge_pass": judge_pass,
                "passed": deterministic_pass and (judge_pass is not False),
                "ttft_s": [x.ttft_s for x in t.turns],
                "total_s": [x.total_s for x in t.turns],
                "models": sorted({x.model for x in t.turns if x.model}),
                "cost_usd": round(sum(c for c in turn_costs if c) + judge_cost, 6),
                "assistant_cost_usd": round(sum(c for c in turn_costs if c), 6),
                "unpriced_turns": sum(1 for c in turn_costs if c is None),
            }
        )
    return {
        "summary": summarize(rows, bool(verdicts)),
        "cases": rows,
        "run": {
            "at": now_text,
            "seconds": round(time.time() - started, 1),
            "api": args.api,
            "filters": {"category": args.category, "case": args.case},
            "judge_model": CONFIG["judge_model"] if verdicts else None,
            "expected_assistant_model": CONFIG["assistant_model"],
        },
    }


def summarize(rows, judged: bool) -> dict:
    by_cat = {}
    for cat in CATEGORIES:
        mine = [r for r in rows if r["category"] == cat]
        if mine:
            by_cat[cat] = {"passed": sum(r["passed"] for r in mine), "total": len(mine)}
    first = [x for r in rows for x in r["ttft_s"][:] if x is not None]
    total = [x for r in rows for x in r["total_s"] if x is not None]
    calls = [c for r in rows for c in r["booking_calls"]]
    models = sorted({m for r in rows for m in r["models"]})
    return {
        "cases": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 4) if rows else 0,
        "deterministic_pass_rate": round(sum(r["deterministic_pass"] for r in rows) / len(rows), 4),
        "judge_pass_rate": round(sum(bool(r["judge_pass"]) for r in rows) / len(rows), 4)
        if judged
        else None,
        "by_category": by_cat,
        "ttft_s": {"p50": percentile(first, 0.5), "p95": percentile(first, 0.95)},
        "total_s": {"p50": percentile(total, 0.5), "p95": percentile(total, 0.95)},
        "booking_calls": {"total": len(calls), "valid": sum(not c["problems"] for c in calls)},
        "models_seen": models,
        "cost_usd": {
            "total": round(sum(r["cost_usd"] for r in rows), 4),
            "assistant": round(sum(r["assistant_cost_usd"] for r in rows), 4),
            "per_conversation": round(sum(r["assistant_cost_usd"] for r in rows) / len(rows), 5),
        },
        "unpriced_turns": sum(r["unpriced_turns"] for r in rows),
    }


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--category", action="append", choices=CATEGORIES, help="repeatable")
    p.add_argument("--case", action="append", help="case id; repeatable")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    p.add_argument("--dry-run", action="store_true", help="print the plan and estimate only")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
