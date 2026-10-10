import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.common import ORMModel

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
                raise ValueError(f"{name} must include a timezone")
        return self


# --- Categories ------------------------------------------------------------------------


class CategoryCreate(_Strict):
    event_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    base_price_paise: Paise
    capacity: int = Field(gt=0, le=100_000, strict=True)
    sales_open_at: Moment
    sales_close_at: Moment
    active: bool = True


class CategoryUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_price_paise: Paise | None = None
    capacity: int | None = Field(default=None, gt=0, le=100_000, strict=True)
    sales_open_at: Moment | None = None
    sales_close_at: Moment | None = None
    active: bool | None = None


class CategoryOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    name: str
    base_price_paise: int
    pricing_version: int
    capacity: int
    occupied: int = Field(description="Confirmed seats plus seats on an unexpired hold")
    available: int
    sales_open_at: datetime
    sales_close_at: datetime
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
    tickets: list[TicketOut] = []
    created_at: datetime
