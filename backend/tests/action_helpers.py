"""Helpers for action-tool tests: configured tenants, catalogue uploads, direct tool calls."""

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.chat.actions import (
    CaptureLeadTool,
    CreateReservationTool,
    LookupOrderTool,
    QueryCatalogTool,
    RequestHumanTool,
)
from app.chat.settings import TenantChatSettings
from app.chat.tools import SearchKnowledgeTool, ToolRegistry, TurnContext
from app.llm import ToolCall

NOW = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)  # Saturday 19:30 in Dhaka
CONTACT = "call 01700-000000"
RESTAURANT_TOOLS = ["search_knowledge", "query_catalog", "create_reservation", "capture_lead"]
SHOP_TOOLS = ["search_knowledge", "query_catalog", "lookup_order", "capture_lead"]
HOURS = {
    **{day: [["12:00", "23:00"]] for day in ("sat", "sun", "mon", "tue", "wed", "thu")},
    "fri": [["14:30", "23:00"]],
}
MENU_CSV = (
    "dish_en,dish_bn,category,price_bdt,spice_level,allergens\n"
    'Kacchi Biryani,কাচ্চি বিরিয়ানি,Rice,480,Medium,"Dairy, Nuts"\n'
    'Morog Polao,মোরগ পোলাও,Rice,380,Mild,"Dairy, Nuts"\n'
    "Begun Bharta,বেগুন ভর্তা,Vegetables,140,Medium,Mustard\n"
    "Borhani,বোরহানি,Drinks,90,Mild,Dairy\n"
    'Firni,ফিরনি,Desserts,110,None,"Dairy, Nuts"\n'
)
PRODUCTS_CSV = (
    "sku,name,category,price_bdt,stock_status,material\n"
    "JL-SAR-001,Dhakai Jamdani Saree,Sarees,18500,In stock,Cotton jamdani\n"
    "JL-SAR-003,Tangail Taant Saree,Sarees,3200,Low stock,Cotton\n"
    "JL-SAR-004,Rajshahi Silk Saree,Sarees,9800,Out of stock,Mulberry silk\n"
    "JL-JUT-001,Jute Tote Bag,Bags,850,In stock,Jute\n"
)


def restaurant_settings(**overrides) -> dict:
    return {
        "assistant_name": "Nodi",
        "business_name": "Cafe Test",
        "fallback_contact": CONTACT,
        "allowed_origins": ["https://cafe.example"],
        "timezone": "Asia/Dhaka",
        "enabled_tools": RESTAURANT_TOOLS,
        "opening_hours": HOURS,
        "max_online_party_size": 8,
        **overrides,
    }


async def set_settings(owner_engine: AsyncEngine, tenant_id: uuid.UUID, settings: dict) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
            {"s": json.dumps(settings), "t": tenant_id},
        )


def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            SearchKnowledgeTool(retriever=None),
            QueryCatalogTool(),
            CreateReservationTool(),
            LookupOrderTool(),
            CaptureLeadTool(),
            RequestHumanTool(),
        ]
    )


def make_context(app_engine, tenant_id, settings: dict, **kwargs) -> TurnContext:
    kwargs.setdefault("user_message_id", uuid.uuid4())  # each context is a new customer turn
    return TurnContext(
        tenant_id=tenant_id,
        settings=TenantChatSettings.from_tenant("Cafe Test", settings),
        nonce="n0nce123",
        now=NOW,
        sessionmaker=async_sessionmaker(app_engine, expire_on_commit=False),
        **kwargs,
    )


async def call(context: TurnContext, name: str, arguments: dict, tools: ToolRegistry | None = None):
    """Run one tool call through the registry (validation, gating, recording)."""
    message = await (tools or registry()).execute(
        context, ToolCall(uuid.uuid4().hex, name, arguments)
    )
    return message.text, context.records[-1]
