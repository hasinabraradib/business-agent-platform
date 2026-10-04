"""Chat cases (evals/data/chat_cases.json): scripted conversations scored with checks.

One runner serves both tiers: it talks to the HTTP API (live: the running stack; deterministic:
the in-process test app), so the same code path a customer uses is the one measured. Each turn
records the reply, outcome, conversation status, tools with arguments and results, citations,
timings, tokens and the model. Groundedness and spelling are scored from these transcripts, so
no conversation is run twice.
"""

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from evaluation import grounding
from evaluation.dataset import DATA_DIR

ChunkFetcher = Callable[[list[str]], Awaitable[dict[str, str]]]

LEAK_PATTERN = (
    r"How you sound|Begin every final reply|hidden status tag|customer-message-|search-results-|"
    r"earlier-sources-|tool-result-|\[\[(answered|no_answer|smalltalk)\]\]"
)


@dataclass
class Case:
    id: str
    tenant: str
    category: str
    description: str
    steps: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    setup: dict[str, Any] | None = None
    kind: str = "chat"  # chat | cross_tenant

    @property
    def says(self) -> int:
        return sum(1 for s in self.steps if "say" in s)

    def estimated_requests(self, per_turn: float) -> float:
        explicit = sum(s.get("est_requests", per_turn) for s in self.steps if "say" in s)
        return 0 if self.kind == "cross_tenant" else explicit


def load_cases(path: Path = DATA_DIR / "chat_cases.json") -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [Case(**item) for item in raw["cases"]]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    for case in cases:
        for check in case.checks:
            if check["type"] not in CHECKS and check["type"] != "any_of":
                raise ValueError(f"{case.id}: unknown check {check['type']}")
    return cases


@dataclass
class Turn:
    say: str
    reply: str = ""
    outcome: str | None = None
    status: str | None = None
    silent: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)  # chunk ids
    ttft_s: float | None = None
    total_s: float | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    error: str | None = None
    sources: list[str] = field(default_factory=list)  # what the reply may rely on
    unsupported: list[str] = field(default_factory=list)  # groundedness failures


@dataclass
class Transcript:
    case_id: str
    tenant: str
    conversation_id: str | None = None
    turns: list[Turn] = field(default_factory=list)
    staff_messages: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)  # e.g. reservations for this chat
    stopped: str | None = None  # why the case could not finish (provider limit, ...)


# --- talking to the API ------------------------------------------------------------------------


@dataclass
class Session:
    """One tenant's API access for the runner."""

    client: httpx.AsyncClient
    admin_key: str
    settings_text: str = ""  # contact, reply time and hours: sources for groundedness

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_key}"}


async def _say(session: Session, visitor: str, conversation: str | None, text: str) -> dict:
    body = {"visitor_id": visitor, "message": text, "stream": True,
            "client_message_id": uuid.uuid4().hex}  # fmt: skip
    if conversation:
        body["conversation_id"] = conversation
    started, ttft, final = time.perf_counter(), None, {}
    async with session.client.stream(
        "POST", "/v1/chat", json=body, headers=session.headers
    ) as response:
        if response.status_code != 200:
            detail = (await response.aread()).decode(errors="replace")[:300]
            return {"http_error": f"{response.status_code}: {detail}"}
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                event, data = fields.get("event"), json.loads(fields.get("data", "{}"))
                if event == "token" and ttft is None:
                    ttft = time.perf_counter() - started
                if event in ("done", "error"):
                    final = {"event": event, **data}
    final["ttft"] = ttft
    final["total"] = time.perf_counter() - started
    return final


async def _conversation(session: Session, conversation: str) -> dict:
    response = await session.client.get(
        f"/v1/conversations/{conversation}", headers=session.headers
    )
    response.raise_for_status()
    return response.json()


async def _upload_and_wait(session: Session, name: str, text: str, wait_s: float = 120) -> str:
    response = await session.client.post(
        "/v1/documents", headers=session.headers, files={"file": (name, text.encode())}
    )
    response.raise_for_status()
    document_id = response.json()["id"]
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        doc = (
            await session.client.get(f"/v1/documents/{document_id}", headers=session.headers)
        ).json()
        if doc["status"] in ("ready", "failed"):
            if doc["status"] == "failed":
                raise RuntimeError(f"setup document failed: {doc.get('error')}")
            return document_id
        await _sleep(2)
    raise RuntimeError("setup document was not processed in time")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# --- running a case ----------------------------------------------------------------------------


ProviderCheck = Callable[[Turn], str | None]  # returns why the run must stop, or None


async def run_case(
    case: Case,
    session: Session,
    fetch_chunks: ChunkFetcher,
    *,
    pause: float = 0.0,
    provider_check: ProviderCheck | None = None,
    after_upload: Callable[[str], Awaitable[None]] | None = None,
) -> Transcript:
    transcript = Transcript(case.id, case.tenant)
    visitor = f"eval-{case.id}-{uuid.uuid4().hex[:6]}"
    uploaded = None
    try:
        if case.setup and "document" in case.setup:
            doc = case.setup["document"]
            uploaded = await _upload_and_wait(session, doc["name"], doc["text"])
            if after_upload is not None:
                await after_upload(uploaded)
        first = True
        for step in case.steps:
            if "say" in step:
                if not first and pause:
                    await _sleep(pause)
                first = False
                turn = await _run_turn(session, transcript, visitor, step["say"], fetch_chunks)
                transcript.turns.append(turn)
                if turn.error or (provider_check and (why := provider_check(turn))):
                    transcript.stopped = turn.error or why
                    break
            elif "staff" in step:
                response = await session.client.post(
                    f"/v1/conversations/{transcript.conversation_id}/messages",
                    json={"text": step["staff"]},
                    headers=session.headers,
                )
                response.raise_for_status()
                transcript.staff_messages.append(step["staff"])
            elif step.get("hand_back"):
                response = await session.client.post(
                    f"/v1/conversations/{transcript.conversation_id}/hand-back",
                    headers=session.headers,
                )
                response.raise_for_status()
        if transcript.conversation_id and not transcript.stopped:
            transcript.facts = await _facts(session, transcript.conversation_id)
    finally:
        if uploaded:
            await session.client.delete(f"/v1/documents/{uploaded}", headers=session.headers)
    return transcript


async def _run_turn(
    session: Session, transcript: Transcript, visitor: str, text: str, fetch_chunks: ChunkFetcher
) -> Turn:
    result = await _say(session, visitor, transcript.conversation_id, text)
    turn = Turn(say=text)
    if "http_error" in result:
        turn.error = f"HTTP {result['http_error']}"
        return turn
    transcript.conversation_id = str(result.get("conversation_id") or transcript.conversation_id)
    turn.ttft_s = result.get("ttft")
    turn.total_s = result.get("total")
    turn.silent = bool(result.get("silent"))
    detail = await _conversation(session, transcript.conversation_id)
    turn.status = detail["status"]
    if turn.silent:
        return turn
    stored = detail["messages"][-1]
    turn.reply = stored["content"]
    turn.outcome = stored["outcome"]
    turn.model = stored["model"]
    turn.prompt_tokens = stored["prompt_tokens"] or 0
    turn.completion_tokens = stored["completion_tokens"] or 0
    turn.model_calls = sum(1 for k in stored["timings"] if k.startswith("model_"))
    record = stored["retrieval"] or {}
    turn.tools = record.get("tools", [])
    turn.citations = [c["chunk_id"] for c in stored["citations"]]
    if stored["error"]:
        turn.error = stored["error"]
    turn.sources = await _sources(session, transcript, detail, fetch_chunks)
    if turn.outcome in ("answered", "action", "no_answer", "smalltalk"):
        checked = grounding.check(turn.reply, turn.sources)
        turn.unsupported = [f"{c.kind}: {c.text}" for c in checked.unsupported]
    return turn


async def _sources(
    session: Session, transcript: Transcript, detail: dict, fetch_chunks: ChunkFetcher
) -> list[str]:
    """Everything a reply in this conversation may state: chunks cited so far, every tool
    result so far, staff and customer messages, and the tenant's own settings text."""
    chunk_ids: list[str] = []
    tool_results: list[str] = []
    texts: list[str] = [session.settings_text]
    for message in detail["messages"]:
        if message["role"] in ("user", "staff"):
            texts.append(message["content"])
        for citation in message.get("citations") or []:
            if citation["chunk_id"] not in chunk_ids:
                chunk_ids.append(citation["chunk_id"])
        for tool in (message.get("retrieval") or {}).get("tools", []):
            if tool.get("result"):
                tool_results.append(tool["result"])
    chunks = await fetch_chunks(chunk_ids) if chunk_ids else {}
    return texts + tool_results + list(chunks.values())


async def _facts(session: Session, conversation: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for table in ("reservations", "leads"):
        response = await session.client.get(f"/v1/{table}", headers=session.headers)
        if response.status_code == 200:
            facts[table] = sum(
                1 for row in response.json() if row["conversation_id"] == conversation
            )
    return facts


# --- checks ------------------------------------------------------------------------------------


def _turns(transcript: Transcript, step: Any) -> list[Turn]:
    """step: 1-based customer turn, -1 (last, the default), or "all"."""
    if step == "all":
        return transcript.turns
    if not transcript.turns:
        return []
    index = (step - 1) if isinstance(step, int) and step > 0 else step
    try:
        return [transcript.turns[index]]
    except IndexError:
        return []


def _field(value: Any, path: str) -> list[Any]:
    """Dotted path with * for lists: 'attributes.*.contains'."""
    values = [value]
    for part in path.split("."):
        next_values = []
        for v in values:
            if part == "*" and isinstance(v, list):
                next_values.extend(v)
            elif isinstance(v, dict) and part in v:
                next_values.append(v[part])
        values = next_values
    return values


def _tools(turns: list[Turn], name: str) -> list[dict[str, Any]]:
    return [t for turn in turns for t in turn.tools if name in ("*", t["tool"])]


def check_tool_called(t: Transcript, c: dict) -> tuple[bool, str]:
    calls = _tools(_turns(t, c.get("step", "all")), c["tool"])
    if "status" in c:
        calls = [x for x in calls if x["status"] == c["status"]]
    found = [x["tool"] + ":" + x["status"] for turn in t.turns for x in turn.tools]
    return bool(calls), f"{c['tool']} {c.get('status', '')} called".strip() if calls else (
        f"expected {c['tool']} {c.get('status', '')}; tools were {found}"
    )


def check_tool_not_called(t: Transcript, c: dict) -> tuple[bool, str]:
    calls = _tools(_turns(t, c.get("step", "all")), c["tool"])
    if "status" in c:
        calls = [x for x in calls if x["status"] == c["status"]]
    return not calls, "not called" if not calls else (
        f"{[x['tool'] + ':' + x['status'] for x in calls]} called"
    )


def check_tool_args(t: Transcript, c: dict) -> tuple[bool, str]:
    calls = _tools(_turns(t, c.get("step", "all")), c["tool"])
    values = [v for x in calls for v in _field(x.get("arguments") or {}, c["field"])]
    ok = any(re.search(c["pattern"], json.dumps(v, ensure_ascii=False), re.I) for v in values)
    return ok, f"{c['field']}={values}" if values else f"no {c['tool']} call with {c['field']}"


def check_tool_result_contains(t: Transcript, c: dict) -> tuple[bool, str]:
    calls = _tools(_turns(t, c.get("step", "all")), c["tool"])
    ok = any(c["text"] in (x.get("result") or "") for x in calls)
    return ok, "result matches" if ok else "result differs: " + "; ".join(
        (x.get("result") or "")[:120] for x in calls
    )


def check_outcome(t: Transcript, c: dict) -> tuple[bool, str]:
    turns = _turns(t, c.get("step", -1))
    got = [turn.outcome for turn in turns]
    if "not_in" in c:
        return bool(got) and all(o not in c["not_in"] for o in got), f"outcome {got}"
    return bool(got) and all(o in c["in"] for o in got), f"outcome {got}"


def check_status(t: Transcript, c: dict) -> tuple[bool, str]:
    turns = _turns(t, c.get("step", -1))
    got = [turn.status for turn in turns]
    return bool(got) and all(s == c["value"] for s in got), f"status {got}"


def check_reply_matches(t: Transcript, c: dict) -> tuple[bool, str]:
    turns = _turns(t, c.get("step", -1))
    ok = bool(turns) and all(re.search(c["pattern"], turn.reply, re.I | re.S) for turn in turns)
    return ok, "matches" if ok else f"no match for /{c['pattern']}/"


def check_reply_not_matches(t: Transcript, c: dict) -> tuple[bool, str]:
    turns = _turns(t, c.get("step", "all"))
    hits = [m.group(0) for turn in turns if (m := re.search(c["pattern"], turn.reply, re.I | re.S))]
    return not hits, "no forbidden text" if not hits else f"found {hits}"


def check_reply_startswith(t: Transcript, c: dict) -> tuple[bool, str]:
    turns = _turns(t, c.get("step", -1))
    ok = bool(turns) and all(turn.reply.startswith(c["prefix"]) for turn in turns)
    return ok, "starts as expected" if ok else "starts differently"


def check_grounded(t: Transcript, c: dict) -> tuple[bool, str]:
    turns = [turn for turn in _turns(t, c.get("step", "all")) if turn.reply]
    failures = [u for turn in turns for u in turn.unsupported]
    return not failures, "all claims supported" if not failures else f"unsupported: {failures}"


def check_script(t: Transcript, c: dict) -> tuple[bool, str]:
    from app.chat.prompts import uses_bengali_script

    turns = _turns(t, c.get("step", -1))
    want_bengali = c["value"] == "bn"
    ok = bool(turns) and all(uses_bengali_script(x.reply) == want_bengali for x in turns)
    return ok, "script as expected" if ok else "wrong script"


def check_db_count(t: Transcript, c: dict) -> tuple[bool, str]:
    got = t.facts.get(c["table"])
    return got == c["expected"], f"{c['table']}={got}"


def check_no_leak(t: Transcript, c: dict) -> tuple[bool, str]:
    return check_reply_not_matches(t, {"pattern": LEAK_PATTERN, "step": "all"})


CHECKS: dict[str, Callable[[Transcript, dict], tuple[bool, str]]] = {
    "tool_called": check_tool_called,
    "tool_not_called": check_tool_not_called,
    "tool_args": check_tool_args,
    "tool_result_contains": check_tool_result_contains,
    "outcome": check_outcome,
    "status": check_status,
    "reply_matches": check_reply_matches,
    "reply_not_matches": check_reply_not_matches,
    "reply_startswith": check_reply_startswith,
    "grounded": check_grounded,
    "script": check_script,
    "db_count": check_db_count,
    "no_leak": check_no_leak,
}


def score(case: Case, transcript: Transcript) -> list[dict[str, Any]]:
    results = []
    for check in case.checks:
        if check["type"] == "any_of":
            parts = [CHECKS[sub["type"]](transcript, sub) for sub in check["checks"]]
            ok = any(p[0] for p in parts)
            reason = " | ".join(p[1] for p in parts)
        else:
            ok, reason = CHECKS[check["type"]](transcript, check)
        results.append({"check": check.get("name", check["type"]), "passed": ok, "reason": reason})
    return results
