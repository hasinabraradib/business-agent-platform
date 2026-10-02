"""Helpers for chat tests: a prompt-aware fake responder, SSE parsing and a demo corpus."""

import json
import re
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.llm import ChatRequest, FakeChatProvider
from tests.retrieval_helpers import add_document

GREETINGS = {"hi", "hello", "thanks", "thank you", "হ্যালো", "assalamualaikum"}
FALLBACK = "call 01700-000000"
ORIGIN = "https://cafe.example"


def customer_message(request: ChatRequest) -> str:
    match = re.search(
        r"<customer-message-(\w+)>\n(.*)\n</customer-message-\1>", request.turns[-1].text, re.S
    )
    return match.group(2) if match else ""


def provided_markers(request: ChatRequest) -> list[int]:
    return [int(m) for m in re.findall(r"^\[(\d+)\] ", request.turns[-1].text, re.M)]


def first_passage_line(request: ChatRequest) -> str:
    match = re.search(r"^\[1\] [^\n]*\n([^\n]+)", request.turns[-1].text, re.M)
    return match.group(1) if match else ""


def smart_responder(request: ChatRequest, model: str) -> str:
    """Greets, answers from passage [1] with a citation, or says it does not know."""
    if model == FakeChatProvider.helper_model:
        return customer_message(request)
    message = customer_message(request).strip().lower().rstrip("!.")
    if message in GREETINGS:
        return "[[smalltalk]]\nHello! How can I help you today?"
    if provided_markers(request):
        return f"[[answered]]\n{first_passage_line(request)} [1]"
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
