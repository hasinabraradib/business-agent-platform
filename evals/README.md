# Evaluation

Measured evidence that the assistant works, in two tiers. Code: `backend/evaluation/`.
Data: `evals/data/`. Results: `evals/results/`.

## Deterministic tier (every push, free)

```bash
cd backend && uv run python -m evaluation deterministic
```

This uses offline providers only: hashing embeddings, the fake reranker and the fake chat
model. `tests/test_eval_suite.py` covers four areas:

- **Ingestion:** the real demo documents go through the real parsers and chunker.
- **Retrieval:** every labelled question in all four modes. It also checks that every label
  points at a real chunk.
- **Groundedness:** the offline model's replies through the chat API.
- **Code-enforced rules and safety probes,** attacked by scripted "models":
  - the confirmation gate, the 8-person limit and opening hours;
  - order-lookup privacy and the attempt limit;
  - disabled tools and handoff silence;
  - injection in a customer message and in an uploaded document;
  - cross-tenant access through the API;
  - the known failures from the last real run.

CI runs this as its own step. It writes a summary to the job page and fails when any of these
drop below the floors in `evals/config.json`: recall@5 per mode, groundedness, the safety cases
or the known-failure cases. Hashing embeddings say nothing about real quality: these numbers
guard the mechanics against regressions.

## Live tier (manual, budgeted)

```bash
# Fresh admin keys in a file (never printed), e.g. created with the CLI and revoked afterwards.
cd backend && uv run python -m evaluation live --keys-file /path/to/keys.json
uv run python -m evaluation live --keys-file /path/to/keys.json --dry-run  # plan and estimate
uv run python -m evaluation report --update-readme                       # refresh README table
```

`keys.json` is `{"restaurant": "bap_admin_...", "shop": "bap_admin_..."}` for the demo tenants.
The live tier needs the stack running with real providers (`docker compose up -d`, seeded with
`./scripts/demo-setup.sh`).

- **Retrieval** (`run_live_retrieval`) calls embeddings and the reranker, never the chat model.
  - It runs all 100 questions in all four modes every time: recall@3, recall@5, MRR and median
    latency (query embedding cached), split by language.
  - It also runs a relevance-threshold sweep and recommends a value. The setting itself is
    never changed automatically.
- **Chat cases** (`evals/data/chat_cases.json`) go through the HTTP API.
  - **Batch size:** at most 25 per run (`--max-cases`), paced for Groq's per-minute token
    limit.
  - **Resumable:** finished cases are kept in `evals/results/live_state.json`, and the next run
    continues with the pending ones.
  - **Budget:** the estimate (2,600 tokens per request, 1.6 requests per turn) is printed first,
    and the run refuses to exceed the per-run or daily budget. The daily ledger counts every
    token the runs record.
  - **Provider limits:** if a turn is answered by a model other than the primary (a provider
    limit forced a failover), the run stops and records where.
  - **Partial runs:** the report lists done and pending cases and labels a partial run
    *partial*.
- **Groundedness, spelling, latency and cost** are scored from the same transcripts, so no
  conversation runs twice.
- **Gemini fallback latency** is timed separately, in process: time to first event per Gemini
  model, against the 3 s failover window and the 4 s target.

### What is scored

| Measure | How |
|---|---|
| recall@k | Share of answerable questions with an expected chunk in the top k (questions often have one correct chunk; a few list two that both answer) |
| MRR | Mean of 1/rank of the first expected chunk |
| Threshold sweep | Gate = top vector similarity ≥ threshold, or a strong keyword match. Counts unanswerable questions wrongly passed and answerable ones wrongly blocked |
| Groundedness | Every checkable claim in a reply (price, clock time, quantity or duration, date, phone, order/booking status, delivery estimate in an order sentence) must appear in the cited chunks, the tool results, staff messages, the tenant's settings or the customer's own words. Claims without a number or status word (e.g. "all meat is halal") are not checked, and the report says so |
| Tool behaviour, handoff, safety | Each case's checks: tool and arguments, tool status, outcome label, conversation status, reply patterns, leaks of the system prompt, rows written |
| Bengali spelling | Suspected misspellings: words one edit away from a lexicon word (`bn_lexicon.txt`) but not in it. Listed for review; Banglish and English are not measured |
| Cost | Tokens recorded per turn × published prices in `evals/config.json` |

### GitHub workflow

`.github/workflows/eval-live.yml` runs the live tier on `workflow_dispatch` only, never
automatically. It needs repository secrets (`GEMINI_API_KEY`, `OPENAI_COMPAT_API_KEY`,
`OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_MODEL`). It keeps the state between runs with the
Actions cache and uploads `evals/results/` as an artifact. It has **not been run**: the local
command above is what produced the README numbers.

## Other

- `chat_script.py`: the older manual conversation script, printing replies, outcomes,
  searches, models and timings.
