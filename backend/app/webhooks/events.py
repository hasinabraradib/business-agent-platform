"""Recording webhook events. Called inside the transaction that makes the change, so an event
exists if and only if the change was committed; the worker delivers it afterwards."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models import WebhookDelivery, WebhookEndpoint
from app.tenancy import TenantDB

EVENT_TYPES = ("reservation.created", "lead.created")


async def record_event(db: TenantDB, event_type: str, data: dict[str, Any]) -> uuid.UUID | None:
    """Queue a delivery to the tenant's webhook, if one is configured and enabled."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    endpoint = await db.scalar(db.select(WebhookEndpoint).where(WebhookEndpoint.enabled.is_(True)))
    if endpoint is None:
        return None
    delivery = WebhookDelivery(event_type=event_type, payload={}, url=endpoint.url)
    db.add(delivery)
    await db.flush()
    delivery.payload = {
        "id": str(delivery.id),
        "type": event_type,
        "created_at": datetime.now(UTC).isoformat(),
        "tenant_id": str(db.tenant_id),
        "data": data,
    }
    return delivery.id
