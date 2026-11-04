# Agent stress test

This suite checks that the assistant answers real customers correctly, before you show it to
clients. It runs 123 scripted conversations with the demo restaurant (Nodi Kitchen,
`demo-restaurant`) through the same public chat API the widget uses, then grades every reply
twice: with deterministic checks and with an LLM judge.

It extends the existing evaluation suite rather than replacing it. The conversations go
through `backend/evaluation/chat.py`, the runner the live tier already uses, and the
`no_leak` patterns are shared with that tier.

## Run it

You need the stack running with the demo tenant seeded (`docker compose up -d`, then
`docker compose run --rm migrate python -m app.cli seed-demo` once). You also need
`OPENAI_API_KEY` in `.env` for the judge.

```bash
make agent-eval-dry                    # plan and cost estimate; sends nothing
make agent-eval                        # every case, then the judge, then the report
make agent-eval ARGS="--category booking --category injection"
make agent-eval ARGS="--case book-party-12 --case oos-wifi"
make agent-eval ARGS="--no-judge"      # deterministic checks only (no judge cost)
make agent-eval-check                  # offline: validate cases.yaml and the grader (CI)
make agent-eval-report RESULTS=tests/agent_eval/results/run-B.json BEFORE=tests/agent_eval/results/run-A.json
```

`--category` and `--case` can be repeated, and they combine as a union: a case runs if it
matches either filter.

Each run does the following:

1. Prints the number of cases and turns and a cost estimate. It refuses to start if the
   estimate plus the spend recorded in `results/spend.json` would exceed `budget_usd_total`
   in `config.json`.
2. Creates a fresh admin key with the CLI and a widget key through the API. Neither is printed
   or written anywhere. Both are revoked when the run ends, even if it fails.
3. Runs the conversations 5 at a time. Each one is `POST /v1/chat` with streaming, the widget
   key and an allowed `Origin`, exactly what the widget sends. Requests are paced at 50 a
   minute, under the key's limit of 60.
4. Reads each transcript back with the admin key: replies, outcome, tools with arguments and
   results, citations, time to first token, total time, tokens and the model that answered.
5. Grades every case, judges it, and writes `results/run-<time>.json` and `report.html`. It
   prints a markdown summary and exits non-zero below a 95% pass rate.

`results/` is git-ignored. `report.html` shows the latest run.

## Grading

A case passes only if **both** layers pass.

**Deterministic checks** (`harness.grade`) apply to every case:
- The run completes without errors.
- No reply is empty or cut off (ending on a comma, colon or open bracket, or with unbalanced
  `**`).
- Nothing internal leaks: tracebacks, raw JSON, tool or data tags, or the system prompt's
  wording.
- Replies stay under 1,200 characters unless the case allows more.
- There is no handoff unless the case expects one.
- No booking is made unless the case expects exactly one.
- Every `create_reservation` call is valid. It must:
  - not be rejected as `invalid_arguments`;
  - include date, time, party size, name and phone;
  - use only a name, phone and party size that the customer actually gave.

On top of these, each case's `expect` adds its own checks:
- the keywords it must or must not contain;
- the tools it must or must not call, and their arguments;
- a handoff;
- "doesn't know": the reply has the `no_answer` outcome, or says it doesn't know and offers
  the contact;
- a citation when the answer comes from the documents;
- the reply's script (Bengali or Latin).

**LLM judge** (`judge.py`) uses `judge_model` from `config.json` at temperature 0, with a
strict JSON schema. It sees:
- the restaurant's documents and settings;
- the current date and time in Dhaka;
- the case's rubric;
- the whole transcript, including tool calls and results.

It scores correctness, groundedness (no facts beyond the documents), helpfulness and tone from
1 to 5, and gives a pass or fail with a one-line reason. A judge pass also needs correctness
≥ 4, groundedness ≥ 4 and nothing below 3. The report opens five judged cases in its "Judge
spot-check" section, failures and passes from different categories, so you can confirm the
judge grades sensibly.

Each failure in the report gets a **likely root cause**. This is a heuristic guess from the
evidence, not a diagnosis:
- retrieval miss;
- system prompt gap;
- tool schema or validation;
- handoff rules;
- model limitation;
- platform or provider error.

## Language decision

The system prompt (`app/chat/prompts.py`, `reply_language`) answers Bengali script in Bengali,
Banglish in Banglish, and everything else in English. So the Spanish cases expect a polite
answer in English. A Spanish reply is accepted too, but not a refusal.

## Adding a case

Add an entry to `cases.yaml`:

```yaml
- id: normal-borhani-price            # unique, kebab-case
  category: normal                    # one of harness.CATEGORIES
  turns: ["How much is the Borhani?"] # customer messages in order; several = multi-turn
  expect:
    must_contain: ["90"]              # regexes, case-insensitive, on the final reply
    cite: true
  rubric: >-
    Borhani costs 90 BDT.             # what the judge checks; name the facts from the documents
```

The `expect` keys:

| Key | Meaning |
|---|---|
| `must_contain` | Regexes the final reply must match |
| `must_not_contain` | Regexes no reply in the conversation may match |
| `tool` | A tool, or a list of tools (any of them), that must be called |
| `tool_args` | `{field: regex}` on that tool's arguments; for bookings, the call that booked |
| `tools_not_called` | Tools that must not be called |
| `should_handoff` | Default `false`. `true` means a handoff must happen; `null` means either is fine |
| `should_say_dont_know` | The reply says it doesn't know and offers the contact (or the outcome is `no_answer`) |
| `cite` | The final reply is `answered` with a citation |
| `booked` | Exactly one reservation is made in this conversation |
| `script` | `bn` (Bengali script) or `latin` |
| `max_chars` | Longest acceptable reply (default 1,200) |
| `http_status` | The API must reject the message with this status, without calling the model |

Dates are resolved at run time in Asia/Dhaka, so bookings stay valid on any day:

| Placeholder | Resolves to |
|---|---|
| `{d+2}` | A long date two days from today ("Friday 6 November") |
| `{iso+2}` | The same date as YYYY-MM-DD, for `tool_args` |
| `{d-2}` | Two days ago, for past-date cases |
| `{wd:fri}` / `{iso_wd:fri}` | The next Friday after today, long or as YYYY-MM-DD |

Base expectations on the demo documents (`demo/restaurant/`) and the tenant's settings in
`backend/app/cli.py` (`DEMO_TENANTS`), never on what the model happened to say. When a case
fails, fix the cause (prompt, retrieval, tool rules), not the expectation. Change an
expectation only when it is itself wrong, and say why in the commit.

Run `make agent-eval-check` after editing. It validates ids, categories, keys, regexes and
placeholders, and CI runs the same test.

## CI

CI runs only the offline part (`backend/tests/test_agent_eval.py`): the case file and the
grader. The repository's rules (AGENTS.md) forbid tests and CI from calling a real model. So
the live run is the manual `make agent-eval`, to repeat whenever you change prompts, models or
documents.

## Files

| File | What it is |
|---|---|
| `cases.yaml` | The cases |
| `harness.py` | Case loading, dates, concurrent running, deterministic grading |
| `judge.py` | The LLM judge |
| `run.py` | The command line: filters, budget, keys, run, judge, results |
| `report.py` | `report.html`, the markdown summary and before/after comparison |
| `config.json` | Models, prices (USD per million tokens), estimate, budget and pacing |
