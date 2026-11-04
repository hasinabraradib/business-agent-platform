"""Render a results file as tests/agent_eval/report.html and a markdown summary.

uv run --project backend python tests/agent_eval/report.py results/run-X.json
uv run --project backend python tests/agent_eval/report.py results/run-B.json --before results/run-A.json
"""

# ruff: noqa: E501  (an HTML template: long lines read better unbroken)
import argparse
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGETS = {"overall": 0.95, "hallucination": 1.0, "injection": 1.0, "booking_args": 1.0}


def root_cause(row: dict) -> str:
    """A heuristic first guess from the evidence in the transcript; the report says so."""
    failed = [c for c in row["checks"] if not c["passed"]]
    names = " ".join(c["name"] for c in failed)
    turns = row["transcript"]["turns"]
    tools = [t["tool"] for turn in turns for t in turn["tools"]]
    final = turns[-1] if turns else {}
    if row["transcript"].get("stopped") or "no errors" in names or "ran to the end" in names:
        return "platform or provider error (the run did not complete cleanly)"
    if row["booking_calls"] and any(c["problems"] for c in row["booking_calls"]):
        return "tool schema / validation: create_reservation called with incomplete or invented details"
    if "booking" in names or "create_reservation." in names:
        return "booking flow: system prompt / tool description (confirmation, limits, dates)"
    if "internals" in names or row["category"] == "injection":
        return "system prompt gap: injection or leak handling"
    if "script" in names:
        return "system prompt gap: reply language"
    if "handed off" in names or "no handoff" in names:
        return "handoff rules: system prompt / request_human description"
    if "citation" in names or "contains" in names:
        if "search_knowledge" not in tools and "query_catalog" not in tools:
            return "system prompt gap: answered (or refused) without searching"
        if final.get("outcome") == "no_answer":
            return (
                "retrieval miss: searched but the right chunk wasn't found or passed the threshold"
            )
        return "retrieval or model: searched, but the reply lacks the expected fact or citation"
    if "doesn't know" in names or row["category"] == "hallucination":
        return "system prompt gap: answered beyond the documents"
    judge = row.get("judge") or {}
    if judge and judge.get("scores", {}).get("groundedness", 5) <= 3:
        return "model limitation / prompt gap: stated facts beyond the documents (judge)"
    if judge and not row.get("judge_pass"):
        return f"model quality (judge): {judge.get('reason', '')}"
    return "see the failed checks"


def _pct(passed: int, total: int) -> str:
    return f"{passed}/{total} ({passed / total:.0%})" if total else "-"


def _fmt_s(v) -> str:
    return f"{v:.2f} s" if isinstance(v, (int, float)) else "-"


def markdown_summary(results: dict, before: dict | None = None) -> str:
    s = results["summary"]
    lines = [
        "## Agent stress test",
        "",
        f"Overall: **{_pct(s['passed'], s['cases'])}** (deterministic "
        f"{s['deterministic_pass_rate']:.0%}"
        + (f", judge {s['judge_pass_rate']:.0%}" if s["judge_pass_rate"] is not None else "")
        + ")",
        f"Booking-tool calls valid: {_pct(s['booking_calls']['valid'], s['booking_calls']['total'])}",
        f"Time to first token p50/p95: {_fmt_s(s['ttft_s']['p50'])} / {_fmt_s(s['ttft_s']['p95'])}; "
        f"total p50/p95: {_fmt_s(s['total_s']['p50'])} / {_fmt_s(s['total_s']['p95'])}",
        f"Cost: ${s['cost_usd']['total']:.4f} total, ${s['cost_usd']['per_conversation']:.5f} "
        f"per conversation (assistant); models seen: {', '.join(s['models_seen']) or '-'}",
    ]
    if s.get("fallback_turns"):
        lines.append(
            f"WARNING: {s['fallback_turns']} turns were answered by a fallback model, not "
            f"{results['run'].get('expected_assistant_model')}; this run measures a mix of models."
        )
    lines += [
        "",
        "| Category | Passed" + (" | Before" if before else "") + " |",
        "|---|---" + ("|---" if before else "") + "|",
    ]
    for cat, v in s["by_category"].items():
        row = f"| {cat} | {_pct(v['passed'], v['total'])}"
        if before:
            b = before["summary"]["by_category"].get(cat)
            row += f" | {_pct(b['passed'], b['total']) if b else '-'}"
        lines.append(row + " |")
    failures = [r for r in results["cases"] if not r["passed"]]
    if failures:
        lines += ["", f"Failures ({len(failures)}):"]
        for r in failures:
            lines.append(f"- `{r['id']}` ({r['category']}): {root_cause(r)}")
    return "\n".join(lines)


def _turns_html(row: dict) -> str:
    out = []
    for i, turn in enumerate(row["transcript"]["turns"], 1):
        out.append(f"<div class='msg user'><b>Customer {i}</b>{html.escape(turn['say'])}</div>")
        for tool in turn["tools"]:
            args = json.dumps(tool.get("arguments") or {}, ensure_ascii=False)
            result = (tool.get("result") or tool.get("summary") or "")[:500]
            out.append(
                f"<div class='tool'><code>{html.escape(tool['tool'])}({html.escape(args)})</code> "
                f"→ <b>{html.escape(str(tool.get('status')))}</b>"
                f"<pre>{html.escape(result)}</pre></div>"
            )
        if turn.get("silent"):
            reply = "<i>(no reply: waiting for the team)</i>"
        elif turn.get("error"):
            reply = f"<i>error: {html.escape(turn['error'])}</i>"
        else:
            reply = html.escape(turn["reply"])
        meta = (
            f"{turn.get('outcome')} · {len(turn.get('citations') or [])} citations · "
            f"status {turn.get('status')} · {turn.get('model') or '-'} · "
            f"first token {_fmt_s(turn.get('ttft_s'))} · total {_fmt_s(turn.get('total_s'))}"
        )
        out.append(
            f"<div class='msg bot'><b>Assistant</b>{reply}<small>{html.escape(meta)}</small></div>"
        )
    if row["transcript"].get("stopped"):
        out.append(f"<p class='bad'>Run stopped: {html.escape(row['transcript']['stopped'])}</p>")
    return "\n".join(out)


def _judge_html(judge: dict | None) -> str:
    if not judge:
        return "<p>Not judged.</p>"
    scores = " · ".join(f"{k} {v}" for k, v in (judge.get("scores") or {}).items())
    verdict = "pass" if judge.get("passed") else "fail"
    return (
        f"<p><b>Judge ({html.escape(judge.get('model', ''))}): {verdict}</b> — {html.escape(scores)}"
        f"<br>{html.escape(judge.get('reason') or judge.get('error') or '')}</p>"
    )


def _case_html(row: dict, open_: bool = False) -> str:
    failed = [c for c in row["checks"] if not c["passed"]]
    checks = "".join(
        f"<li class='{'ok' if c['passed'] else 'bad'}'>{'✓' if c['passed'] else '✗'} "
        f"{html.escape(c['name'])} <small>{html.escape(c['detail'])}</small></li>"
        for c in row["checks"]
    )
    status = "PASS" if row["passed"] else "FAIL"
    cause = (
        ""
        if row["passed"]
        else (f"<p><b>Likely root cause (heuristic):</b> {html.escape(root_cause(row))}</p>")
    )
    expect = html.escape(json.dumps(row["expect"], ensure_ascii=False))
    return (
        f"<details class='case' {'open' if open_ else ''}><summary><b class='{status.lower()}'>"
        f"{status}</b> <code>{html.escape(row['id'])}</code> · {html.escape(row['category'])}"
        f"{' · ' + str(len(failed)) + ' failed checks' if failed else ''}</summary>"
        f"<p><b>Expected:</b> {html.escape(row['rubric'])}<br><small>{expect}</small></p>"
        f"{cause}{_turns_html(row)}<ul class='checks'>{checks}</ul>{_judge_html(row.get('judge'))}"
        "</details>"
    )


def spot_check(results: dict, n: int = 5) -> list[dict]:
    """A spread of judged cases: failures and passes from different categories."""
    judged = [r for r in results["cases"] if r.get("judge") and not r["judge"].get("error")]
    picked, seen = [], set()
    for want_pass in (False, True, False, True, False, True):
        for r in judged:
            if bool(r["judge"]["passed"]) == want_pass and r["category"] not in seen:
                picked.append(r)
                seen.add(r["category"])
                break
    for r in judged:  # top up if categories ran out
        if len(picked) >= n:
            break
        if r not in picked:
            picked.append(r)
    return picked[:n]


CSS = """
:root{--ink:#111;--muted:#5b6170;--line:#e4e6eb;--soft:#f4f5f7;--ok:#1d7a3e;--bad:#b3261e;--accent:#c5ee4f}
@media (prefers-color-scheme: dark){:root:not([data-theme=light]){--ink:#eceef2;--muted:#a4a9b4;--line:#2c3038;--soft:#1b1e24}}
body{margin:0;background:var(--soft);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{margin:0 0 4px}h2{margin-top:32px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.card{background:Canvas;border:1px solid var(--line);border-radius:16px;padding:14px}
.card b{display:block;font-size:24px}
table{border-collapse:collapse;width:100%;background:Canvas;border-radius:12px;overflow:hidden}
td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
.case{background:Canvas;border:1px solid var(--line);border-radius:14px;margin:10px 0;padding:10px 14px}
.case summary{cursor:pointer}.pass,.ok{color:var(--ok)}.fail,.bad{color:var(--bad)}
.msg{border-radius:14px;padding:8px 12px;margin:6px 0;max-width:85%;white-space:pre-wrap}
.msg b{display:block;font-size:12px;color:var(--muted)}.msg small{display:block;color:var(--muted);margin-top:4px}
.user{background:var(--soft)}.bot{background:color-mix(in srgb,var(--accent) 35%,Canvas);margin-left:auto}
.tool{font-size:13px;margin:4px 0 4px 12px}.tool pre{white-space:pre-wrap;margin:4px 0;color:var(--muted)}
.checks{font-size:13px;padding-left:18px}code{font-size:13px}
"""


def write_html(results: dict, path: Path, before: dict | None = None) -> None:
    s = results["summary"]
    cats = "".join(
        f"<tr><td>{html.escape(c)}</td><td>{_pct(v['passed'], v['total'])}</td>"
        + (
            f"<td>{_pct(**{k: before['summary']['by_category'][c][k] for k in ('passed', 'total')})}</td>"
            if before and c in before["summary"]["by_category"]
            else ("<td>-</td>" if before else "")
        )
        + "</tr>"
        for c, v in s["by_category"].items()
    )
    failures = [r for r in results["cases"] if not r["passed"]]
    passes = [r for r in results["cases"] if r["passed"]]
    spot = spot_check(results)
    run = results["run"]
    judge_rate = f"{s['judge_pass_rate']:.0%}" if s["judge_pass_rate"] is not None else "not run"
    body = f"""<main>
<h1>Agent stress test</h1>
<p class="muted">{html.escape(run["at"])} · {s["cases"]} cases · models: {html.escape(", ".join(s["models_seen"]) or "-")}
· judge: {html.escape(run.get("judge_model") or "not run")}</p>
<div class="cards">
<div class="card">Overall pass<b>{s["pass_rate"]:.0%}</b>{s["passed"]}/{s["cases"]} (target 95%)</div>
<div class="card">Deterministic<b>{s["deterministic_pass_rate"]:.0%}</b>checks only</div>
<div class="card">Judge<b>{judge_rate}</b>rubric verdicts</div>
<div class="card">Booking calls valid<b>{_pct(s["booking_calls"]["valid"], s["booking_calls"]["total"])}</b>target 100%</div>
<div class="card">First token p50 / p95<b>{_fmt_s(s["ttft_s"]["p50"])}</b>p95 {_fmt_s(s["ttft_s"]["p95"])}</div>
<div class="card">Total p50 / p95<b>{_fmt_s(s["total_s"]["p50"])}</b>p95 {_fmt_s(s["total_s"]["p95"])}</div>
<div class="card">Cost per conversation<b>${s["cost_usd"]["per_conversation"]:.4f}</b>run total ${s["cost_usd"]["total"]:.4f}</div>
</div>
<h2>By category</h2>
<table><tr><th>Category</th><th>Passed</th>{"<th>Before</th>" if before else ""}</tr>{cats}</table>
<h2>Judge spot-check</h2>
<p class="muted">Five judged cases (failures and passes from different categories) to confirm the judge grades sensibly.</p>
{"".join(_case_html(r, open_=True) for r in spot)}
<h2>Failures ({len(failures)})</h2>
{"".join(_case_html(r, open_=False) for r in failures) or "<p>None.</p>"}
<h2>Passes ({len(passes)})</h2>
{"".join(_case_html(r) for r in passes)}
</main>"""
    path.write_text(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Agent Stress Test</title><style>{CSS}</style></head><body>{body}</body></html>",
        encoding="utf-8",
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("results")
    p.add_argument("--before", help="an earlier results file, for a before/after column")
    p.add_argument("--out", default=str(HERE / "report.html"))
    args = p.parse_args(argv)
    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    before = json.loads(Path(args.before).read_text(encoding="utf-8")) if args.before else None
    write_html(results, Path(args.out), before)
    print(markdown_summary(results, before))
    return 0


if __name__ == "__main__":
    sys.exit(main())
