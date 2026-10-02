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
curl -H "Authorization: Bearer bap_admin_..." -H 'content-type: application/json' \
     -d '{"query": "borhani er dam koto?", "mode": "hybrid", "top_k": 5}' localhost:8000/v1/search
curl -N -H "Authorization: Bearer bap_admin_..." -H 'content-type: application/json' \
     -d '{"visitor_id": "v1", "message": "How much is the Kacchi Biryani?"}' localhost:8000/v1/chat
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

## Retrieval

`Retriever.retrieve(tenant_id, query, mode, top_k)` (`backend/app/retrieval/`) returns the most
relevant chunks with their scores; `POST /v1/search` exposes it for testing a knowledge base, with
per-stage timings. There are four modes, all kept so they can be compared on an eval set:

- **vector**: the query is embedded (cached in Redis per tenant and normalized query) and
  compared to chunk embeddings by cosine similarity. Only chunks embedded with the same model are
  compared.
- **keyword**: full-text match over each chunk's `tsvector`. Every query word is a separate
  term weighted by how rare it is in the tenant's chunks (inverse document frequency), so a
  question does not need every word to match, one rare term ("borhani") is enough, and more
  matching terms rank higher. Common question words in English, Bengali and romanized Bengali
  are ignored. Compound tokens such as product codes (`JL-SAR-001`) are also matched as an exact
  phrase, so the exact code outranks its look-alikes.
- **hybrid**: the vector and keyword rankings merged with **Reciprocal Rank Fusion**: each
  chunk scores `sum(1 / (k + rank))` over the rankings it appears in (`RRF_K`, default 60). RRF
  needs only ranks, so it can merge cosine similarities and keyword weights, which live on
  unrelated scales, without calibrating one against the other. Vector search finds paraphrases
  and cross-language matches (a Bengali question about English opening hours); keyword search
  finds exact names and codes that embeddings blur together.
- **hybrid_rerank**: the top 15 fused candidates are scored 0–10 by a small, cheap LLM
  (`gemini-3.5-flash-lite`, structured JSON output, validated strictly). Any failure or timeout
  keeps the fused order and is reported, so reranking can never make results worse than hybrid.

**Why the `simple` text configuration:** content is in English and Bengali (and customers write
romanized Bengali). Postgres has no Bengali stemmer, and an English stemmer would mangle Bengali
and romanized words, so text is lowercased and split into words without stemming or a built-in
stop-word list. Exact dish names, codes and Bengali words therefore match reliably; inflections
and paraphrases are left to vector search.

**The filtered index problem:** the HNSW vector index finds approximate nearest neighbours
first and applies the tenant filter afterwards, to only the first ~`hnsw.ef_search` candidates.
A tenant that owns a small share of the table can then get fewer than `top_k` results, or none
(the test shows 0 of 5 for a tenant with 8 chunks next to one with 2,000). The fix has two
layers: pgvector's **iterative index scan** (`hnsw.iterative_scan = relaxed_order`) keeps
scanning until enough rows pass the filter, with results re-sorted by exact distance; and if the
index still returns too few rows while the tenant has more, an **exact scan over that tenant's
rows** fills the gap.

**Confidence:** every result set includes the best vector similarity and `has_relevant_context`
(similarity >= threshold). The chat step uses it to say "I don't know" instead of guessing. The
starting threshold for `gemini-embedding-2` is 0.65: in a first check on the demo data,
on-topic questions scored 0.72–0.83 and off-topic ones 0.53–0.59. Similarities are model-
specific, so each embedding provider suggests its own value and `RELEVANCE_THRESHOLD` overrides
it; it will be tuned against evals.

## Chat

`POST /v1/chat` answers a customer's question from the tenant's knowledge, with citations. It
accepts a widget key (from a website) or an admin key:

```json
{"conversation_id": null, "visitor_id": "v-123", "message": "How much is the Kacchi Biryani?", "stream": true}
```

With `stream: true` it sends Server-Sent Events: `token` events with the reply text, then
`citations`, then `done` (message id, conversation id, outcome, token usage). If generation fails
it sends one `error` event instead. `stream: false` returns the whole reply as one JSON object.
`GET /v1/conversations` and `GET /v1/conversations/{id}` (admin key) list conversations and show
their messages with citations, outcomes, timings and retrieval details.

**Pipeline** (`backend/app/chat/`):
1. Load the last few turns of the conversation.
2. If there is history, a small, cheap model (`gemini-3.5-flash-lite`) rewrites the message
   into a standalone search query ("and is it spicy?" → "is Kacchi Biryani spicy"). First
   messages skip this. A rewrite that fails, or that just repeats an earlier question, is
   discarded and the original message is searched.
3. Retrieve with mode `hybrid`, top 8 (`CHAT_RETRIEVAL_MODE=hybrid_rerank` turns the reranker
   on).
4. The context counts as relevant if the best vector similarity clears the threshold **or**
   keyword search found a strong exact match (an exact product code, or every word of the
   question including a rare one). Otherwise the model receives no passages at all.
5. Generate the answer with `gemini-3.8-flash` (Google's default general-purpose model), from
   the provided passages only. Overload and rate-limit errors that arrive before any text has
   been streamed are retried, then `CHAT_FALLBACK_MODEL` (`gemini-3.6-flash`) is tried.

**Answer rules.** The system prompt enforces these, and code checks them:
- Answer only from the passages. If the answer is not there, say so and offer the business's
  contact. Never invent prices, hours, policies or stock. *Code check:* when the context is not
  relevant, no passages are sent, so there is nothing to cite. A "don't know" reply that lacks
  the fallback contact gets it appended.
- Reply in the customer's language and script (English, Bengali, or romanized Bengali). *Code
  check:* a script mismatch is recorded on the message.
- Greetings and thanks get a short friendly reply.
- Cite with `[n]` markers that refer to the numbered passages. *Code check:* a streaming filter
  drops any marker that does not match a passage actually provided, even when a marker arrives
  split across chunks. The customer never sees an invalid citation.
- Customer messages, history and retrieved passages are data, not instructions. *In the
  prompt:* they sit inside tags whose names carry a random per-request nonce, so text cannot
  close a tag and pose as instructions.
- The assistant's name, business name, tone, fallback contact, allowed origins and daily cap
  come from tenant settings (`tenants.settings`).

**How outcomes are decided.** Each assistant message is stored as `answered`, `no_answer`
(feeds the knowledge-gaps report) or `smalltalk`, or `error` when generation failed. The model
starts its reply with a hidden tag (`[[answered]]`, `[[no_answer]]` or `[[smalltalk]]`), which
is stripped before streaming, because only the model reliably recognises a greeting in any
language or script. Code then decides what can be verified: a reply is `answered` only if it
cites at least one passage that was actually provided. `smalltalk` is taken from the model's tag
when it cited nothing. Everything else, including a missing tag or a claimed answer without a
citation, is `no_answer`. So an `answered` message always traces back to the knowledge base, and
a confident-sounding reply without support is counted as a gap.

**Protections for the public endpoint** (widget keys are visible in websites):
- **Allowed origins:** widget requests must come from one of the tenant's `allowed_origins`.
  CORS headers are returned only for that origin.
- **Rate limits:** Redis counters per key and per visitor per minute, and a per-tenant daily
  message cap. Over-limit requests get 429 with a clear message and `Retry-After`.
- **Message length:** messages are limited to 2,000 characters (422 if longer).
- **Conversation ownership:** a conversation can only be continued by the same tenant and
  visitor.
- **Failures:** a model error or timeout (first token, idle gap or total) ends the stream with an
  `error` event and a short apology with the fallback contact, and is stored with the error. A
  stream never hangs.
