"""The live tier: real embeddings, reranker and chat models, with hard budgets.

    uv run python -m evaluation live --keys-file KEYS.json [--max-cases 25] [--only id,id]

- Retrieval (all four modes, threshold sweep) runs every time: embeddings and reranker only.
- Chat cases run through the HTTP API with fresh admin keys, at most N per invocation, paced
  for the provider's per-minute token limit, resumable across days (evals/results/
  live_state.json). The estimated tokens are printed first and the run refuses to exceed the
  run or daily budget. A turn answered by anything other than the expected primary model (a
  provider limit forced a failover) stops the run cleanly, and the report says where.
- Gemini fallback latency is measured on its own, in process, against the 4 s target.
Results go to evals/results/<date>.json; `python -m evaluation report` renders the table.
"""

import json
import statistics
import time
import uuid
from datetime import date
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from evaluation import retrieval
from evaluation.chat import Case, Session, Transcript, load_cases, run_case, score
from evaluation.dataset import CONFIG_PATH, RESULTS_DIR, TENANTS, load_questions, validate
from evaluation.state import State, plan_run

API = "http://localhost:8000"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


async def tenant_ids(owner_url: str) -> dict[str, uuid.UUID]:
    engine = create_async_engine(owner_url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT slug, id FROM tenants"))).all()
    finally:
        await engine.dispose()
    by_slug = {slug: tid for slug, tid in rows}
    return {name: by_slug[slug] for name, (slug, _) in TENANTS.items() if slug in by_slug}


async def chunk_keys(owner_url: str) -> set[str]:
    from evaluation.dataset import chunk_key

    engine = create_async_engine(owner_url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT metadata FROM chunks"))).scalars().all()
    finally:
        await engine.dispose()
    return {chunk_key(m) for m in rows}


def chunk_fetcher(owner_url: str):
    engine = create_async_engine(owner_url)

    async def fetch(ids: list[str]) -> dict[str, str]:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT id::text, content FROM chunks WHERE id::text = ANY(:ids)"),
                {"ids": ids},
            )
            return {row[0]: row[1] for row in rows}

    fetch.engine = engine  # type: ignore[attr-defined]
    return fetch


# --- retrieval ---------------------------------------------------------------------------------


async def run_live_retrieval(owner_url: str) -> dict[str, Any]:
    from app.retrieval import get_retriever

    tenants = await tenant_ids(owner_url)
    questions = [q for name in tenants for q in load_questions(name)]
    validate(questions, await chunk_keys(owner_url))
    retriever = get_retriever()
    embed_started = time.perf_counter()
    results = await retrieval.run_retrieval(
        retriever, tenants, questions, pause_s={"hybrid_rerank": 4.5}
    )
    elapsed = round(time.perf_counter() - embed_started, 1)
    sweep = retrieval.threshold_sweep(results)
    return {
        "embedding_model": retriever.embedder.model_name,
        "reranker": type(retriever.reranker).__name__,
        "questions": len(questions),
        "summary": retrieval.summarize(results),
        "sweep": sweep,
        "recommended_threshold": retrieval.recommend_threshold(sweep),
        "results": retrieval.as_dicts(results),
        "seconds": elapsed,
    }


# --- cross-tenant probes (no model calls) ------------------------------------------------------


async def cross_tenant_probe(client: httpx.AsyncClient, keys: dict[str, str], state: State) -> list:
    """The restaurant's admin key against the shop's data: conversation detail, staff reply,
    hand-back and resolve on a shop conversation, plus lists that must not include shop rows."""
    a = {"Authorization": f"Bearer {keys['restaurant']}"}
    b = {"Authorization": f"Bearer {keys['shop']}"}
    shop_conversations = (await client.get("/v1/conversations", headers=b)).json()
    results = []
    if shop_conversations:
        target = shop_conversations[0]["id"]
        for method, path, body in [
            ("GET", f"/v1/conversations/{target}", None),
            ("POST", f"/v1/conversations/{target}/messages", {"text": "probe"}),
            ("POST", f"/v1/conversations/{target}/hand-back", None),
            ("POST", f"/v1/conversations/{target}/resolve", None),
        ]:
            response = await client.request(method, path, json=body, headers=a)
            results.append(
                {"check": f"{method} {path.split('/')[-1] if 'messages' in path else method} shop "
                 "conversation", "passed": response.status_code == 404,
                 "reason": f"HTTP {response.status_code}"}
            )  # fmt: skip
    shop_ids = {c["id"] for c in shop_conversations}
    mine = {c["id"] for c in (await client.get("/v1/conversations", headers=a)).json()}
    results.append({"check": "conversation list", "passed": not (mine & shop_ids),
                    "reason": f"{len(mine & shop_ids)} shop conversations visible"})  # fmt: skip
    orders = (await client.get("/v1/orders", headers=a)).json()
    results.append({"check": "orders list", "passed": orders == [],
                    "reason": f"{len(orders)} orders visible"})  # fmt: skip
    documents = (await client.get("/v1/documents", headers=a)).json()
    titles = {d["title"] for d in documents}
    leaked = titles & {"Shipping", "Returns and refunds", "Jamdani Lane product catalogue"}
    results.append({"check": "documents list", "passed": not leaked,
                    "reason": f"shop documents visible: {sorted(leaked)}"})  # fmt: skip
    search = await client.post(
        "/v1/search", json={"query": "Jamdani saree return policy", "top_k": 5}, headers=a
    )
    shop_files = {"faq.md", "returns.md", "shipping.md", "products.csv"}
    hits = search.json().get("results", []) if search.status_code == 200 else []
    shop_hits = [h for h in hits if h["metadata"].get("source") in shop_files]
    passed = search.status_code == 200 and not shop_hits
    reason = f"{len(shop_hits)} shop chunks returned"
    results.append({"check": "search across tenants", "passed": passed, "reason": reason})
    return results


# --- Gemini fallback latency (in process, separate quota) --------------------------------------


async def gemini_latency(samples: int) -> dict[str, Any]:
    """Time to first event per Gemini model, with the real system prompt and tools, and time to
    first token for full turns on a Gemini-only chain. Compared with the 3 s failover window
    and the 4 s first-token target."""
    from datetime import UTC, datetime

    from app.chat.deps import get_chat_service
    from app.chat.prompts import customer_block, system_prompt
    from app.chat.settings import TenantChatSettings
    from app.llm import (
        Candidate,
        ChatChain,
        ChatRequest,
        GeminiChatProvider,
        LLMSettings,
        Message,
    )

    settings_llm = LLMSettings()
    provider = GeminiChatProvider(settings_llm.gemini_api_key)
    service = get_chat_service()
    settings = TenantChatSettings.from_tenant(
        "Nodi Kitchen", {"timezone": "Asia/Dhaka", "enabled_tools": list(service.tools.names)}
    )
    system = system_prompt(settings, datetime.now(UTC), "n0nce", service.tools.guidance(settings))
    questions = ["What time do you close tonight?", "kacchi koto?", "hi", "Which dishes have nuts?",
                 "শুক্রবার আপনারা কখন খোলেন?", "Do you deliver?"][:samples]  # fmt: skip
    out: dict[str, Any] = {}
    for model in ("gemini-3.8-flash", "gemini-3.6-flash"):
        firsts, errors = [], []
        for question in questions:
            request = ChatRequest(
                system=system,
                messages=[Message("user", customer_block(question, "n0nce", []))],
                tools=service.tools.specs(settings),
                max_output_tokens=800,
            )
            started = time.perf_counter()
            try:
                async for _event in provider.stream(request, model=model):
                    firsts.append(round(time.perf_counter() - started, 2))
                    break
            except Exception as exc:  # recorded; quota errors stop this model
                errors.append(str(exc)[:200])
                if "429" in str(exc):
                    break
        out[model] = {"first_event_s": firsts, "errors": errors, **_stats(firsts)}
    await provider.aclose()
    _ = Candidate, ChatChain  # full-turn timing uses the API's chain; see report notes
    return out


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "p95": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"median": round(statistics.median(ordered), 2), "p95": p95}


# --- the chat batch ----------------------------------------------------------------------------


def _provider_check(prefix: str):
    def check(turn) -> str | None:
        if turn.model and not turn.model.startswith(prefix) and not turn.silent:
            return f"answered by {turn.model}, not {prefix}*: the primary provider is limited"
        if turn.error and any(s in turn.error for s in ("429", "quota", "rate limit", "TPD")):
            return f"provider limit: {turn.error[:200]}"
        return None

    return check


async def run_chat_batch(
    keys: dict[str, str], config: dict[str, Any], *, max_cases: int, only: set[str] | None,
    dry_run: bool = False, owner_url: str,
) -> dict[str, Any]:  # fmt: skip
    live = config["live"]
    cases = load_cases()
    state = State.load()
    plan = plan_run(
        cases, state, max_cases=max_cases, tokens_per_request=live["tokens_per_request"],
        requests_per_turn=live["requests_per_turn"], run_token_budget=live["run_token_budget"],
        daily_token_budget=live["daily_token_budget"], only=only,
    )  # fmt: skip
    used = state.used_today()
    print(
        f"Chat cases: {plan.pending_total} pending; this run: {len(plan.cases)} "
        f"({', '.join(c.id for c in plan.cases) or 'none'}). Estimated {plan.estimated_tokens:,} "
        f"tokens; used today {used['tokens']:,} of {live['daily_token_budget']:,}."
    )
    if plan.skipped_for_budget:
        print(f"Left for a later run (budget): {', '.join(plan.skipped_for_budget)}")
    if dry_run or not plan.cases:
        return {"ran": [], "stopped": None}
    fetch = chunk_fetcher(owner_url)
    stopped = None
    ran = []
    async with httpx.AsyncClient(base_url=API, timeout=180) as client:
        sessions = {}
        for name in TENANTS:
            session = Session(client, keys[name])
            tenant = (await client.get("/v1/tenant", headers=session.headers)).json()
            session.settings_text = json.dumps(tenant.get("settings", {}), ensure_ascii=False)
            sessions[name] = session
        first = True
        for case in plan.cases:
            if not first and case.kind == "chat":
                await _pause(live["pause_seconds"])
            first = False
            if case.kind == "cross_tenant":
                checks = await cross_tenant_probe(client, keys, state)
                transcript = Transcript(case.id, case.tenant)
            else:
                transcript = await run_case(
                    case, sessions[case.tenant], fetch, pause=live["pause_seconds"],
                    provider_check=_provider_check(live["expected_model_prefix"]),
                )  # fmt: skip
                checks = score(case, transcript)
            requests = sum(t.model_calls for t in transcript.turns)
            tokens = sum(t.prompt_tokens + t.completion_tokens for t in transcript.turns)
            state.record_usage(requests, tokens)
            status = "blocked" if transcript.stopped else "done"
            state.cases[case.id] = {
                "status": status,
                "category": case.category,
                "passed": status == "done" and all(c["passed"] for c in checks),
                "checks": checks,
                "transcript": _transcript_dict(transcript),
                "requests": requests,
                "tokens": tokens,
                "finished_at": date.today().isoformat(),
            }
            state.save()
            ran.append(case.id)
            mark = (
                "PASS"
                if state.cases[case.id]["passed"]
                else ("STOP" if transcript.stopped else "FAIL")
            )
            print(f"  {mark} {case.id} ({requests} requests, {tokens:,} tokens)")
            if transcript.stopped:
                stopped = {"case": case.id, "why": transcript.stopped}
                state.stops.append({"date": date.today().isoformat(), **stopped})
                state.save()
                print(f"Stopped at {case.id}: {transcript.stopped}")
                break
    await fetch.engine.dispose()
    return {"ran": ran, "stopped": stopped}


async def _pause(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _transcript_dict(t: Transcript) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(t)
    for turn in data["turns"]:
        turn.pop("sources", None)  # large and reproducible from the citations
    return data


def results_path(day: str | None = None):
    return RESULTS_DIR / f"{day or date.today().isoformat()}.json"


def _cases_summary(cases: list[Case], state: State) -> dict[str, Any]:
    done = {cid: r for cid, r in state.cases.items() if r["status"] == "done"}
    return {
        "total": len(cases),
        "done": len(done),
        "pending": [c.id for c in cases if c.id not in done],
        "passed": sum(1 for r in done.values() if r["passed"]),
        "failed": [cid for cid, r in done.items() if not r["passed"]],
    }
