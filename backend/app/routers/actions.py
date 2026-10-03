"""Admin endpoints for what the assistant's tools create: reservations, leads, orders,
catalogue items, and the outbound webhook (URL, secret, delivery log)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import AdminAuth
from app.ingestion.fetch import UnsafeURLError, validate_url
from app.models import CatalogItem, Lead, Order, Reservation, WebhookDelivery, WebhookEndpoint
from app.routers.documents import ResolverDep
from app.schemas import ORMModel
from app.webhooks.delivery import new_secret

router = APIRouter(tags=["actions"])
Limit = Annotated[int, Query(ge=1, le=200)]


class ReservationOut(ORMModel):
    id: uuid.UUID
    reference: str
    starts_at: datetime
    local_date: Any
    local_time: Any
    party_size: int
    name: str
    phone: str
    notes: str
    status: str
    conversation_id: uuid.UUID | None
    created_at: datetime


class ReservationUpdate(BaseModel):
    status: Literal["confirmed", "cancelled", "completed", "no_show"]


class LeadOut(ORMModel):
    id: uuid.UUID
    name: str
    contact: str
    interest: str
    status: str
    conversation_id: uuid.UUID | None
    created_at: datetime


class OrderOut(ORMModel):
    id: uuid.UUID
    order_number: str
    status: str
    items: list[dict[str, Any]]
    total: Decimal
    currency: str
    phone: str
    placed_at: datetime
    shipped_at: datetime | None
    delivered_at: datetime | None
    courier: str | None


class CatalogItemOut(ORMModel):
    id: uuid.UUID
    document_id: uuid.UUID
    row: int
    name: str
    alt_name: str | None
    category: str | None
    price: Decimal | None
    currency: str
    in_stock: bool | None
    attributes: dict[str, Any]


class WebhookEndpointIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True


class WebhookEndpointOut(BaseModel):
    url: str
    enabled: bool
    secret_hint: str  # first characters only
    secret: str | None = None  # returned once: when created or rotated


class WebhookDeliveryOut(ORMModel):
    id: uuid.UUID
    event_type: str
    url: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    attempt_log: list[dict[str, Any]]
    next_attempt_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime


@router.get("/reservations", response_model=list[ReservationOut])
async def list_reservations(auth: AdminAuth, limit: Limit = 100):
    return await auth.db.scalars(
        auth.db.select(Reservation).order_by(Reservation.starts_at.desc()).limit(limit)
    )


@router.patch("/reservations/{reservation_id}", response_model=ReservationOut)
async def update_reservation(reservation_id: uuid.UUID, body: ReservationUpdate, auth: AdminAuth):
    reservation = await auth.db.get(Reservation, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found")
    reservation.status = body.status
    await auth.db.flush()
    response = ReservationOut.model_validate(reservation)
    await auth.db.commit()
    return response


@router.get("/leads", response_model=list[LeadOut])
async def list_leads(auth: AdminAuth, limit: Limit = 100):
    return await auth.db.scalars(auth.db.select(Lead).order_by(Lead.created_at.desc()).limit(limit))


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(auth: AdminAuth, limit: Limit = 100):
    return await auth.db.scalars(
        auth.db.select(Order).order_by(Order.placed_at.desc()).limit(limit)
    )


@router.get("/catalog", response_model=list[CatalogItemOut])
async def list_catalog(auth: AdminAuth, limit: Annotated[int, Query(ge=1, le=1000)] = 200):
    return await auth.db.scalars(
        auth.db.select(CatalogItem).order_by(CatalogItem.document_id, CatalogItem.row).limit(limit)
    )


def _endpoint_out(endpoint: WebhookEndpoint, reveal: bool) -> WebhookEndpointOut:
    return WebhookEndpointOut(
        url=endpoint.url,
        enabled=endpoint.enabled,
        secret_hint=endpoint.secret[:10] + "…",
        secret=endpoint.secret if reveal else None,
    )


@router.get("/webhooks/endpoint", response_model=WebhookEndpointOut)
async def get_webhook_endpoint(auth: AdminAuth):
    endpoint = await auth.db.scalar(auth.db.select(WebhookEndpoint))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No webhook configured")
    return _endpoint_out(endpoint, reveal=False)


@router.put("/webhooks/endpoint", response_model=WebhookEndpointOut)
async def set_webhook_endpoint(body: WebhookEndpointIn, auth: AdminAuth, resolver: ResolverDep):
    """Set the webhook URL (SSRF-checked now, and again on every delivery). The signing secret
    is generated on first setup and returned only then."""
    try:
        target = await validate_url(body.url, resolver)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    endpoint = await auth.db.scalar(auth.db.select(WebhookEndpoint))
    created = endpoint is None
    if endpoint is None:
        endpoint = WebhookEndpoint(url=str(target.url), secret=new_secret(), enabled=body.enabled)
        auth.db.add(endpoint)
    else:
        endpoint.url = str(target.url)
        endpoint.enabled = body.enabled
    await auth.db.flush()
    response = _endpoint_out(endpoint, reveal=created)
    await auth.db.commit()
    return response


@router.post("/webhooks/endpoint/rotate-secret", response_model=WebhookEndpointOut)
async def rotate_webhook_secret(auth: AdminAuth):
    endpoint = await auth.db.scalar(auth.db.select(WebhookEndpoint))
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No webhook configured")
    endpoint.secret = new_secret()
    await auth.db.flush()
    response = _endpoint_out(endpoint, reveal=True)
    await auth.db.commit()
    return response


@router.get("/webhooks/deliveries", response_model=list[WebhookDeliveryOut])
async def list_webhook_deliveries(auth: AdminAuth, limit: Limit = 100):
    return await auth.db.scalars(
        auth.db.select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)
    )
