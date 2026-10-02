# Business Agent Platform

A multi-tenant AI assistant platform for small businesses. Each business (tenant) uploads its
documents, and the assistant answers customer questions from them with citations, takes actions
through tools, and hands off to a human when needed. This repository currently contains the
foundation: a FastAPI backend on PostgreSQL 16 (pgvector) and Redis 7 with tenants, API-key
authentication and tenant data isolation, run locally with Docker Compose.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # placeholder values; edit if you like
docker compose up --build     # postgres, redis, migrate (role + migrations), api
curl localhost:8000/health    # {"status":"ok","database":"up"}

# Create tenants (prints admin keys once; store them)
docker compose run --rm migrate python -m app.cli seed-demo
docker compose run --rm migrate python -m app.cli create-tenant --name "Rosa's Pizzeria" --slug rosas
curl -H "Authorization: Bearer bap_admin_..." localhost:8000/v1/tenant
```

API docs: http://localhost:8000/docs. If you have a database volume from before the app role
existed, recreate it with `docker compose down -v`.

## Run the tests

Tests need the Compose Postgres running. They use a separate database (`TEST_DATABASE_NAME`,
default `app_test`) on the same server, created and migrated automatically.

```bash
docker compose up -d postgres
cd backend
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Apply migrations by hand with `uv run alembic upgrade head` (from `backend/`).

## Multi-tenancy

Every request is authenticated with `Authorization: Bearer <key>`. A key belongs to one tenant
and has one of two kinds:

- **admin** (`bap_admin_…`): secret, for the business owner's dashboard; full access to that
  tenant's `/v1` routes.
- **widget** (`bap_widget_…`): public, embedded in a website; will be allowed only on
  customer-facing chat endpoints. Admin routes reject it with 403.

Only the sha256 hash of a key is stored; the full key is shown once, when it is created.
Tenants themselves are created with the platform CLI, not through the API.

Tenant data is isolated in two layers:

1. **Application:** routes access tenant-owned tables only through `TenantDB`
   (`backend/app/tenancy.py`), which adds `WHERE tenant_id = <authenticated tenant>` to every
   query and sets `tenant_id` on every new row.
2. **Database:** every tenant-owned table has Postgres Row-Level Security, forced, with a policy
   requiring `tenant_id = current_setting('app.current_tenant')`. Each request sets that value
   for its own transaction only. A query that forgets its filter, or runs with no tenant set,
   returns no other tenant's rows (or no rows at all).

Postgres skips RLS for superusers and for a table's owner, so if the API connected as the
owner role, the second layer would do nothing. The API therefore connects as a separate,
non-superuser role (`bap_app`, `DATABASE_URL`) that owns nothing and has only the grants it
needs. Migrations and the CLI use the owner role (`OWNER_DATABASE_URL`); in Compose they run in
the one-shot `migrate` service, so the API container never has owner credentials.
