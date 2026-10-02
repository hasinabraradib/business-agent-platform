# Business Agent Platform

A multi-tenant AI assistant platform for small businesses. Each business (tenant) uploads its
documents, and the assistant answers customer questions from them with citations, takes actions
through tools, and hands off to a human when needed. This repository currently contains the
foundation: a FastAPI backend on PostgreSQL 16 (pgvector) and Redis 7, run locally with Docker
Compose.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # placeholder values; edit if you like
docker compose up --build     # postgres, redis, api (migrations run on start)
curl localhost:8000/live      # {"status":"ok"}
curl localhost:8000/health    # {"status":"ok","database":"up"}
```

## Run the tests

Tests need the Compose Postgres running; they use a separate database (`TEST_DATABASE_URL`,
default `app_test`), created and migrated automatically.

```bash
docker compose up -d postgres
cd backend
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Apply migrations by hand with `uv run alembic upgrade head` (from `backend/`).
