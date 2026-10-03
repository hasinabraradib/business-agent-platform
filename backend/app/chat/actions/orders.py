import re

from pydantic import Field

from app.chat.tools import Tool, ToolArgs, ToolResult, TurnContext
from app.models import Order

NOT_FOUND = (
    "No order matches that order number and phone number. Ask the customer to check both "
    "(the order number is in the confirmation SMS or email)."
)


class LookupOrderArgs(ToolArgs):
    order_number: str = Field(pattern=r"^[A-Za-z0-9-]{3,32}$", description="e.g. JL-10234")
    phone_last4: str = Field(
        pattern=r"^\d{4}$", description="Last 4 digits of the phone number on the order"
    )


class LookupOrderTool(Tool):
    """Order status for a customer who proves they know the order: order number plus the last 4
    digits of its phone. Wrong digits and unknown orders give the same answer, and attempts are
    limited per visitor, so it cannot be used to discover orders."""

    name = "lookup_order"
    args_model = LookupOrderArgs
    rate_limit = (5, 3600)

    def description(self, settings) -> str:
        return (
            "Look up an order's status. Needs the order number and the last 4 digits of the "
            "phone number used for the order."
        )

    def guidance(self, settings) -> str:
        return (
            "- Order status: ask for the order number and the last 4 digits of the phone number "
            "on the order, then call lookup_order. Never reveal more of the phone number."
        )

    async def run(self, context: TurnContext, args: LookupOrderArgs) -> ToolResult:
        number = args.order_number.strip().upper()
        async with context.db() as db:
            order = await db.scalar(db.select(Order).where(Order.order_number == number))
        if order is None or not re.sub(r"\D", "", order.phone).endswith(args.phone_last4):
            body, summary = NOT_FOUND, "not found"
        else:
            context.lookups += 1
            items = ", ".join(f"{i.get('qty', 1)} x {i.get('name')}" for i in order.items)
            dates = [f"placed {order.placed_at:%d %b %Y}"]
            if order.shipped_at:
                dates.append(f"shipped {order.shipped_at:%d %b %Y}")
            if order.delivered_at:
                dates.append(f"delivered {order.delivered_at:%d %b %Y}")
            courier = f"; courier: {order.courier}" if order.courier else ""
            body = (
                f"Order {order.order_number}: status {order.status}; items: {items}; total "
                f"{order.currency} {order.total:g}; {', '.join(dates)}{courier}."
            )
            summary = f"found ({order.status})"
        return ToolResult(f"<tool-result-{context.nonce}>\n{body}\n</tool-result-{context.nonce}>",
                          summary=summary)  # fmt: skip
