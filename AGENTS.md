# AGENTS.md

Conventions for coding sessions (human or AI) working in this repository.

## Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.0 async (asyncpg), Alembic, pydantic-settings
- Data: PostgreSQL 16 with pgvector, Redis 7
- Tooling: uv (dependencies, `backend/uv.lock`), Ruff (lint + format), pytest + httpx
- Frontend (later): Next.js + TypeScript in `web/`
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
backend/alembic/      migrations (async env.py; runs as OWNER_DATABASE_URL)
backend/tests/        pytest suite (conftest.py sets up the test database)
web/                  frontend (placeholder)
evals/                assistant evaluations (placeholder)
```

## Commands (run from `backend/` unless noted)

```bash
docker compose up --build              # repo root: full stack on :8000
docker compose up -d postgres          # repo root: just the DB, for tests
uv sync                                # install dependencies
uv run ruff check . && uv run ruff format --check .
uv run pytest                          # uses database TEST_DATABASE_NAME, never the dev DB
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"   # then add RLS/grants by hand
uv run python -m app.cli seed-demo     # or create-tenant --name ... --slug ...
docker compose run --rm migrate python -m app.cli seed-demo   # repo root, inside Compose
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
- `/live` must never touch the database; `/health` is the database-backed readiness check.
