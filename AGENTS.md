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
backend/alembic/      migrations (async env.py; URL from DATABASE_URL)
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
uv run pytest                          # uses TEST_DATABASE_URL, never DATABASE_URL
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"
```

## Rules

- **Every change ships with tests.** New endpoints, models, and bug fixes get pytest coverage;
  CI (ruff check, ruff format --check, pytest) must stay green.
- **Never commit secrets.** Configuration comes from environment variables via
  `app.config.Settings`. Add every new variable to `.env.example` with a safe placeholder.
  `.env` is git-ignored.
- **Small commits with clear messages.** One logical change per commit; imperative subject line,
  body explaining why when it isn't obvious.
- Schema changes go through Alembic migrations only; never edit an applied migration.
- Use the `SessionDep` dependency from `app.db` for database access in routes.
- `/live` must never touch the database; `/health` is the database-backed readiness check.
