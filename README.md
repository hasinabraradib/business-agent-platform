# Business Agent Platform

A multi-tenant AI assistant platform for small businesses. Each business (tenant) uploads its
documents, and the assistant answers customer questions from them with citations, takes actions
through tools, and hands off to a human when needed. This repository currently contains the
foundation: a FastAPI backend on PostgreSQL 16 (pgvector) and Redis 7 with tenants, API-key
authentication, tenant data isolation and document ingestion, run locally with Docker Compose.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # placeholder values; edit if you like
docker compose up --build     # postgres, redis, migrate (role + migrations), api, worker
curl localhost:8000/health    # {"status":"ok","database":"up"}

# Demo tenants with ingested demo/ knowledge (prints keys once; store them)
docker compose run --rm migrate python -m app.cli seed-demo
docker compose run --rm migrate python -m app.cli create-tenant --name "Rosa's Pizzeria" --slug rosas
docker compose run --rm migrate python -m app.cli create-key --tenant rosas --kind widget

curl -H "Authorization: Bearer bap_admin_..." localhost:8000/v1/documents
curl -H "Authorization: Bearer bap_admin_..." -F file=@menu.csv localhost:8000/v1/documents
```

Set `GEMINI_API_KEY` in `.env` for real embeddings; without it the stack uses deterministic
fake embeddings (fine for development, useless for search quality).

API docs: http://localhost:8000/docs. If you have a database volume from before the app role
existed, recreate it with `docker compose down -v`.

## Run the tests

Tests need the Compose Postgres and Redis running. They use a separate database
(`TEST_DATABASE_NAME`, default `app_test`) and Redis database 15, always use fake embeddings, and
cannot reach the network.

```bash
docker compose up -d postgres redis
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

## Ingestion

A tenant adds knowledge with `POST /v1/documents` (file upload) or `POST /v1/documents/url`.
The API stores the source, creates the document as `pending` and returns 202 at once; a
background worker (`arq` on Redis, the `worker` service) parses, chunks, embeds and stores it,
then sets `ready` or `failed` with an error message. Uploading identical content again returns
the existing document. `POST /v1/documents/{id}/reingest` rebuilds a document's chunks, and the
new chunks replace the old ones in a single transaction, so readers never see a mix. `DELETE`
removes the document, its chunks and its stored file.

**Sources:** PDF, Markdown, plain text and CSV uploads (UTF-8, up to 10 MB), and public web pages
(HTML main text, plain text, Markdown or PDF).

**Chunking:** chunks follow the document's structure instead of fixed-size windows, because a
chunk that mixes two topics, or stops mid-sentence, retrieves badly and reads badly when quoted.
Prose is split by heading (a chunk never crosses into the next section), then packed from whole
sentences (English `. ! ?` and the Bengali `।`) to about 400 tokens, preferring paragraph breaks,
with a small overlap of whole sentences. CSV files become one chunk per row, rendered as
`column: value` lines, so a menu item or product stays intact with its price and attributes.
Each chunk records its section heading, page or row number and source. The embedded text is
prefixed with the document title and section heading so a chunk makes sense on its own; the
original text is stored unchanged. Keyword search uses Postgres's `simple` configuration
(content is English and Bengali, and Postgres has no Bengali stemmer).

**Embeddings:** `gemini-embedding-2` at 768 dimensions (`EMBEDDING_PROVIDER`, `GEMINI_API_KEY`),
batched, with retries and exponential backoff on rate limits and transient errors. Providers
live behind one interface in `backend/app/embeddings/`.

**SSRF protection for URLs:** only `http`/`https` without credentials; the host is resolved and
every address must be public (private, loopback, link-local, CGNAT, reserved and multicast
ranges are refused, including IPv4-mapped IPv6); the connection is pinned to the checked address
so DNS cannot change between check and connect; redirects are followed manually and each hop is
checked again; responses are capped at 5 MB and 20 seconds; proxy environment variables are
ignored. The API checks the URL when it is submitted and the worker checks it again on fetch.

**Known limitation:** there is no OCR. A scanned PDF (images without a text layer) ends as
`failed` with "no extractable text; OCR is not supported yet".
