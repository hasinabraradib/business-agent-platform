# AGENTS.md

Conventions for coding sessions (human or AI) working in this repository.

## Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.0 async (asyncpg), Alembic, pydantic-settings
- Ingestion: arq worker on Redis, pypdf, BeautifulSoup, httpx; embeddings from an API (Gemini)
- Data: PostgreSQL 16 with pgvector, Redis 7
- Tooling: uv (dependencies, `backend/uv.lock`), Ruff (lint + format), pytest + httpx
- Frontend: Next.js (App Router) + TypeScript + Tailwind dashboard in `web/dashboard/`; the
  widget in `web/widget/` (TypeScript, no framework)
- Local run: Docker Compose; CI: GitHub Actions (`.github/workflows/ci.yml`)

## Layout

```
backend/app/          FastAPI app: main.py (create_app), config.py (Settings), db.py, routers/
  models.py           SQLAlchemy models; tenant-owned models use the TenantOwned mixin
  auth.py             Bearer API-key auth (AuthContext, AdminAuth)
  tenancy.py          TenantDB: tenant-scoped queries for tenant-owned tables
  security.py         API key generation and hashing
  tenants.py, cli.py  tenant provisioning and the platform CLI (owner role)
  db_roles.py         the non-superuser application role (bap_app)
  ingestion/          parsers, chunking, SSRF-safe fetch, storage, queue, pipeline
  embeddings/         EmbeddingProvider interface, Gemini and fake providers, registry
  retrieval/          Retriever (vector/keyword/hybrid/hybrid_rerank), RRF, rerankers, cache
  llm/                ChatProvider interface (tool calls), Gemini, OpenAI-compatible, fake,
                      and ChatChain (fast failover across models/providers)
  chat/               tool loop (service.py), ToolRegistry, WriteTool + search_knowledge
                      (tools.py), prompts, stream filters, outcomes, rate limits, confirm.py
  chat/actions/       query_catalog, create_reservation, lookup_order, capture_lead,
                      request_human
  handoff.py          conversation states, the handoff acknowledgement, staff actions
  channels/           pipeline.py (every channel's customer turn), outbound.py (staff
                      messages to the customer's channel), telegram.py, alerts.py
  secret_box.py       AES-GCM encryption for stored secrets (SECRETS_ENCRYPTION_KEY)
  webhooks/           event recording, HMAC signing, worker delivery with retries
  worker.py           arq WorkerSettings (`arq app.worker.WorkerSettings`)
backend/alembic/      migrations (async env.py; runs as OWNER_DATABASE_URL)
backend/tests/        pytest suite (conftest.py sets up the test database)
web/widget/           embeddable chat widget (TypeScript, esbuild, no UI framework)
  src/tokens.ts       design tokens: the single source for colours, radii, spacing, shadows
web/demo/             two static demo sites embedding the widget (served on :8080)
web/dashboard/        owner/staff dashboard (Next.js, served on :3001)
  src/lib/session.ts  sealed httpOnly session cookie holding the admin key (server only)
  src/lib/proxy.ts    allowlist + CSRF header for the /api/v1/[...path] proxy to the API
  src/app/tokens.css  generated from web/widget/src/tokens.ts (`npm run tokens`)
  e2e/                Playwright flows, axe checks, README screenshots (against bap-e2e)
scripts/e2e-stack.sh  offline e2e Compose project `bap-e2e` (own ports/volumes, fake providers)
scripts/demo-setup.sh builds the widget and writes the git-ignored demo page config
backend/evaluation/   evaluation suite: retrieval metrics, groundedness, spelling, chat cases,
                      resumable live runner, report (`python -m evaluation ...`)
evals/                eval data (data/: labelled questions, chat cases, lexicon), config.json
                      (CI floors, budgets, prices), results/ (live runs); chat_script.py
integrations/n8n/     n8n workflow for webhook events (untested end to end)
demo/                 fictional demo knowledge ingested by `seed-demo`
```

## Commands (run from `backend/` unless noted)

```bash
docker compose up --build              # repo root: full stack on :8000
docker compose up -d postgres redis    # repo root: what the tests need
uv sync                                # install dependencies
uv run ruff check . && uv run ruff format --check .
uv run pytest                          # uses database TEST_DATABASE_NAME, never the dev DB
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"   # then add RLS/grants by hand
uv run python -m app.cli seed-demo     # or create-tenant / create-key --tenant ... --kind ...
uv run arq app.worker.WorkerSettings   # run the ingestion worker outside Docker
docker compose run --rm migrate python -m app.cli seed-demo   # repo root, inside Compose
cd web/widget && npm ci && npm run check   # widget: types, lint, tests, build + gzip budget
./scripts/demo-setup.sh                 # repo root: widget build + demo page config
cd web/dashboard && npm ci && npm run check   # dashboard: types, lint, Vitest, build
./scripts/e2e-stack.sh up && (cd web/dashboard && npx playwright test --project=e2e)  # repo root
```

## Database roles

- `DATABASE_URL` connects as `bap_app`, a non-superuser role with no ownership and only explicit
  grants. The API always uses it, so RLS applies. Never point the API at the owner role.
- `OWNER_DATABASE_URL` connects as the owner. It is for migrations and the CLI only. The owner
  must be a superuser or have BYPASSRLS, because `resolve_api_key()` (SECURITY DEFINER) relies
  on it.

## Rules

- **Every change ships with tests.** New endpoints, models, and bug fixes get pytest coverage;
  CI (ruff check, ruff format --check, pytest) must stay green.
- **Never commit secrets.** Configuration comes from environment variables via
  `app.config.Settings`. Add every new variable to `.env.example` with a safe placeholder.
  `.env` is git-ignored.
- **Small commits with clear messages.** One logical change per commit; imperative subject line,
  body explaining why when it isn't obvious.
- Schema changes go through Alembic migrations only; never edit an applied migration.
- **Every tenant-owned table gets a `tenant_id`, an RLS policy and an isolation test.** In
  practice: the `TenantOwned` mixin on the model; in the migration, `ENABLE` + `FORCE ROW LEVEL
  SECURITY`, a `tenant_isolation` policy on `app.current_tenant`, and explicit grants to
  `bap_app`; a test showing tenant A cannot see tenant B's rows.
  (`tests/test_rls.py` fails if any table with a `tenant_id` column lacks forced RLS and a policy.)
- Tenant routes take `AdminAuth` (or a future widget dependency) and query only through
  `auth.db` (`TenantDB`), never a raw session. `SessionDep` is for tenant-less routes like
  `/health`.
- Never log, store or return a full API key after creation. Store only its sha256 hash.
- **Tests never call a real AI API or the network.** `conftest.py` forces the fake embedding
  provider and blocks DNS for non-loopback hosts; fake external services with
  `httpx.MockTransport` and injected resolvers. The queue and file storage are replaced per
  test; only `test_worker_redis.py` uses the real Redis queue (database 15).
- Code outside a request (worker, CLI ingestion) also connects as `bap_app` and uses
  `tenancy.tenant_db(...)`, so RLS applies everywhere tenant data is touched.
- Raw SQL on tenant-owned tables (vector and full-text search) goes through
  `TenantDB.execute_sql`, which requires a `:tenant_id` filter. Retrieval opens its own
  `tenant_db(...)` sessions so vector and keyword search can run concurrently.
- Keep all four retrieval modes working; later evals compare them. Rerankers must fail safe
  (the Retriever falls back to fused order), and LLM output is validated, never trusted.
- Chat: untrusted text (customer messages, search results, earlier sources) only ever goes
  inside the nonce-delimited tags built in `app/chat/prompts.py`. `answered` requires a valid
  citation to a source found in the conversation; never relax `decide_outcome` without a test.
  Every chat run must end with exactly one done or error event. New capabilities are tools:
  subclass `Tool` with a Pydantic `ToolArgs` model and register it in `ToolRegistry` (wired in
  `app/chat/deps.py`); tenants opt in through `enabled_tools`. Anything that writes on a
  customer's behalf subclasses `WriteTool`, which enforces the confirmation turn and
  idempotency in code; never write directly from `run()`. Tool results go back to the model
  through `tool_result_block` (nonce-tagged) and must not leak data the customer hasn't proved
  they may see (see `lookup_order`). Tests use `FakeChatProvider` (scripted tool calls);
  `conftest.py` forces `CHAT_PROVIDER=fake`.
- Webhook events are recorded with `record_event` inside the write's transaction and enqueued
  after commit. Outbound HTTP goes through `post_json`/`fetch_url` (SSRF checks, pinned IP).
- Channels: every customer message, from any channel, goes through
  `app.channels.pipeline.accept()`. It alone decides whether the assistant may answer. While a
  conversation is `waiting_human` or `human`, no model is called; never move that check into
  a prompt or a single channel. A new channel is an adapter that calls `accept()`, runs the
  returned turn through `ChatService`, and formats the reply for its medium.
- The handoff acknowledgement and anything else promising times or availability is written by
  code from tenant settings (`app/handoff.py`), never by the model.
- Secrets the platform must read back (bot tokens) are stored with `app.secret_box` (bound to the
  tenant id). Secrets it only checks (webhook secret tokens) are stored as hashes and compared
  with `hmac.compare_digest`. Never log or return either. Public endpoints that resolve a tenant
  (the Telegram webhook) answer every failure the same way, so they never reveal which tenants
  exist.
- Telegram in tests: swap `app.channels.telegram.CLIENT_FACTORY` for a client over
  `httpx.MockTransport` (see `tests/test_telegram.py`); never call the real Bot API.
- Evaluation: a failure found with a real model becomes a permanent eval case first (confirm it
  fails on the current code), then the fix. The deterministic tier (`tests/test_eval_suite.py`)
  must stay green and must never call a real provider; raise the floors in `evals/config.json`
  when the system improves, never lower them to make CI pass. The live tier never runs
  automatically, keeps to the budgets in `evals/config.json`, and a partial run is always
  labelled partial. Retrieval metrics never call the chat model. Keep question labels honest:
  label the chunk that really answers the question, never tune questions to the retriever.
- New chat providers go in `app/llm/` only (subclass + registry entry).
- Widget: never use `innerHTML`/`insertAdjacentHTML`; build DOM with `createElement` and
  `textContent` (`src/render.ts` for any server or model text, http/https links only). Colours,
  radii, spacing, shadows and motion come from `src/tokens.ts`, never hard-coded in styles. Keep
  `widget.js` under its gzip budget (enforced by `npm run build`). Test the widget with
  `node --test` and happy-dom (Playwright is for the dashboard only).
- Dashboard: the admin key never reaches browser JS or storage; it lives sealed in the httpOnly
  session cookie and is added by the server-side proxy. A new API call from the browser needs
  an entry in `src/lib/proxy.ts` RULES (with a test in `tests/proxy.test.ts`); server
  components call `api()` from `src/lib/server.ts`. Render customer and model text as React
  text, never `dangerouslySetInnerHTML`. Colours, radii, spacing and shadows come from the
  generated `tokens.css` (edit `web/widget/src/tokens.ts`, then `npm run tokens`). Every
  screen has loading, empty and error states and passes axe in `e2e/a11y.spec.ts`. E2E tests
  run only against `scripts/e2e-stack.sh` (offline providers), create the data they change,
  and clean up documents they add, so they can rerun on the same stack. Screenshots must show
  only invented demo data and never a full key.
- Never commit widget keys: demo pages read the git-ignored `web/demo/config.local.js`.
- New embedding providers go in `app/embeddings/` only (subclass + registry entry); vectors must
  be `EMBEDDING_DIMENSIONS` long, and each chunk records its `embedding_model`.
- Keep dependencies light and permissively licensed (MIT/BSD/Apache-2.0): no PyTorch or local
  models; disk space is limited.
- `/live` must never touch the database; `/health` is the database-backed readiness check.
