"""Helpers for chat tests: a prompt-aware fake responder, SSE parsing and a demo corpus."""

import json
import re
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.llm import ChatRequest, Scripted
from tests.retrieval_helpers import add_document

GREETINGS = {"hi", "hello", "thanks", "thank you", "হ্যালো", "assalamualaikum"}
FALLBACK = "call 01700-000000"
ORIGIN = "https://cafe.example"


def customer_message(request: ChatRequest) -> str:
    """The current customer message (the last user message's customer-message block)."""
    for message in reversed(request.messages):
        if message.role == "user":
            match = re.search(
                r"<customer-message-(\w+)>\n(.*)\n</customer-message-\1>", message.text, re.S
            )
            return match.group(2) if match else message.text
    return ""


def earlier_sources(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            match = re.search(r"<earlier-sources-\w+>(.*?)</earlier-sources", message.text, re.S)
            return match.group(1) if match else ""
    return ""


def tool_results(request: ChatRequest) -> list[str]:
    return [m.text for m in request.messages if m.role == "tool"]


def searched(request: ChatRequest) -> bool:
    return request.messages[-1].role == "tool"


def first_passage(text: str) -> tuple[str, str] | None:
    match = re.search(r"^\[(\d+)\] [^\n]*\n([^\n]+)", text, re.M)
    return (match.group(1), match.group(2)) if match else None


def smart_responder(request: ChatRequest, model: str) -> "str | Scripted":
    """Greets and thanks without searching; answers a follow-up about spice from the earlier
    sources; otherwise searches once (query = the message) and cites the first passage."""
    if searched(request):
        passage = first_passage(request.messages[-1].text)
        if passage:
            return f"[[answered]]\n{passage[1]} [{passage[0]}]"
        return "[[no_answer]]\nSorry, I don't have that information."
    message = customer_message(request).strip().lower().rstrip("!.")
    if message in GREETINGS:
        return "[[smalltalk]]\nHello! How can I help you today?"
    if message.startswith("thanks"):
        return "[[smalltalk]]\nAnytime!"
    earlier = earlier_sources(request)
    if "spicy" in message and "spice_level" in earlier:
        marker = re.search(r"^\[(\d+)\] ", earlier, re.M).group(1)
        return f"[[answered]]\nIt's medium spicy [{marker}]."
    if request.tools:
        return Scripted(tool_calls=[("search_knowledge", {"query": customer_message(request)})])
    return "[[no_answer]]\nSorry, I don't have that information."


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def configure_tenant(owner_engine: AsyncEngine, tenant_id: uuid.UUID, **settings) -> None:
    values = {
        "assistant_name": "Nodi",
        "business_name": "Cafe Test",
        "fallback_contact": FALLBACK,
        "allowed_origins": [ORIGIN],
        **settings,
    }
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
            {"s": json.dumps(values), "t": tenant_id},
        )


CAFE_MENU = [
    ("dish: Kacchi Biryani\nprice_bdt: 480\nspice_level: Medium\nallergens: Nuts", {"row": 1}),
    ("dish: Borhani\nprice_bdt: 90\nspice_level: Mild", {"row": 2}),
    ("sku: JL-SAR-001\nname: Dhakai Jamdani Saree\nprice_bdt: 18500", {"row": 3}),
]
CAFE_ABOUT = [
    ("We are open every day from noon to 11 pm.", {"section": "Opening hours"}),
    ("Parking for six cars is available after 6 pm.", {"section": "Parking"}),
]


async def add_cafe_corpus(owner_engine: AsyncEngine, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    ids = await add_document(owner_engine, tenant_id, "Menu", CAFE_MENU)
    ids += await add_document(owner_engine, tenant_id, "About", CAFE_ABOUT)
    return ids


async def chat(
    client: httpx.AsyncClient, key: str, message: str, *, origin: str | None = None, **body
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"}
    if origin:
        headers["Origin"] = origin
    payload = {"visitor_id": "visitor-1", "message": message, "stream": False, **body}
    return await client.post("/v1/chat", json=payload, headers=headers)
