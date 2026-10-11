import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.common import ORMModel
from app.services.ticket_rules import MAX_NAME_LENGTH, MAX_PRICE_PAISE
from app.schemas.events import invalid

IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,128}$")]
Paise = Annotated[int, Field(ge=0, le=9_007_199_254_740_991, strict=True)]
Moment = Annotated[datetime, Field(description="With a timezone, e.g. 2026-11-01T10:00:00+05:30")]


class _Strict(BaseModel):
    # Unknown fields are refused, so a request cannot smuggle in a price, a user or a status.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def _moments_have_timezones(self):
        for name, value in self:
            if isinstance(value, datetime) and value.utcoffset() is None:
                raise invalid(f"{name} must include a timezone")
        return self


# --- Categories ------------------------------------------------------------------------


# A ticket price: whole paise, up to ₹1,00,000 per ticket.
PricePaise = Annotated[int, Field(ge=0, le=MAX_PRICE_PAISE, strict=True)]


class CategoryCreate(_Strict):
    """One more category for an event whose ticket settings already exist. Goes through the
    same writer as the organizer page, so while sales are on, the categories must still add up
    to the event's capacity exactly."""

    event_id: uuid.UUID
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=300)
    base_price_paise: PricePaise
    capacity: int = Field(gt=0, le=100_000, strict=True)
    # Optional narrower window inside the event's booking window. Both or neither.
    sales_open_at: Moment | None = None
    sales_close_at: Moment | None = None
    member_discount_eligible: bool = True
    active: bool = True


class CategoryUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=300)
    base_price_paise: PricePaise | None = None
    capacity: int | None = Field(default=None, gt=0, le=100_000, strict=True)
    sales_open_at: Moment | None = None
    sales_close_at: Moment | None = None
    member_discount_eligible: bool | None = None
    active: bool | None = None


class CategoryOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    name: str
    description: str | None = None
    base_price_paise: int
    pricing_version: int
    member_discount_eligible: bool = True
    sort_order: int = 0
    capacity: int
    occupied: int = Field(description="Confirmed seats plus seats on an unexpired hold")
    available: int
    sales_open_at: datetime | None = Field(description="When this category can be booked from: its own window inside the event's")
    sales_close_at: datetime | None
    active: bool
    on_sale: bool = Field(description="Bookable right now: event published, category active, inside the window")


# --- Quotes and bookings ---------------------------------------------------------------


class QuoteRequest(_Strict):
    event_id: uuid.UUID
    ticket_category_id: uuid.UUID
    quantity: int = Field(gt=0, le=1000, strict=True)


class QuoteOut(ORMModel):
    """The server's price. Send `id` back as `accepted_quote_id`; the amounts are not read from the request."""

    id: uuid.UUID
    event_id: uuid.UUID
    ticket_category_id: uuid.UUID
    quantity: int
    base_unit_price_paise: int
    unit_price_paise: int
    total_price_paise: int
    member_eligible: bool
    applied_discount_bps: int
    discount_applied: bool
    discount_reason: str
    discount_explanation: str
    calculator_version: str
    category_pricing_version: int
    issued_at: datetime
    expires_at: datetime


class ReservationRequest(_Strict):
    """Browser input. Identity, club, price, membership, capacity and payment are not accepted."""

    event_id: uuid.UUID
    ticket_category_id: uuid.UUID
    quantity: int = Field(gt=0, le=1000, strict=True)
    idempotency_key: IdempotencyKey
    accepted_quote_id: uuid.UUID


class TicketOut(ORMModel):
    id: uuid.UUID
    seat_number: int
    status: Literal["pending_issue", "issued", "revoked", "checked_in"]


class BookingOut(BaseModel):
    kind: Literal["reserved"] = "reserved"
    replayed: bool = Field(default=False, description="True when this is the answer to a repeated request")
    booking_id: uuid.UUID
    event_id: uuid.UUID
    event_status: str
    ticket_category_id: uuid.UUID
    purchaser_user_id: uuid.UUID
    status: Literal["pending_payment", "confirmed", "cancelled", "expired"]
    payment_status: Literal["pending", "failed", "paid", "not_required", "refunded"]
    quantity: int
    unit_price_paise: int
    total_price_paise: int
    currency: str
    discount_applied: bool
    discount_explanation: str
    hold_expires_at: datetime | None
    paid_at: datetime | None = None
    refund_status: Literal["required", "pending", "succeeded", "failed"] | None = None
    refund_reason: str | None = None
    tickets: list[TicketOut] = []
    created_at: datetime


# --- Ticket payments -------------------------------------------------------------------


class TicketCheckoutRequest(_Strict):
    """Only which booking to pay for. The amount comes from the booking's stored price."""

    booking_id: uuid.UUID


class TicketCheckoutOut(BaseModel):
    booking_id: uuid.UUID
    provider: Literal["stripe", "mock"]
    is_mock: bool
    session_id: str
    checkout_url: str | None = Field(description="Stripe's hosted checkout page. Null with the mock provider")
    amount: int = Field(description="In paise, from the booking")
    currency: str
    hold_expires_at: datetime


class TicketVerifyRequest(_Strict):
    session_id: str = Field(min_length=5, max_length=200)


class TicketPaymentOut(BaseModel):
    """What happened to a payment. `outcome` is the thing to act on."""

    outcome: Literal["confirmed", "refund_required", "refund_pending", "refunded", "refund_failed"]
    message: str
    booking: BookingOut


class TicketMockSimulate(_Strict):
    session_id: str = Field(min_length=5, max_length=200)
    outcome: Literal["success", "failure"] = "success"


class TicketWebhookAck(BaseModel):
    status: Literal["processed", "ignored", "duplicate"]
    detail: str | None = None
