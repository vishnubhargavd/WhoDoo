"""FairSeat event-side contracts (Member 2): organizer ticket settings and event discovery.

Money is integer paise. Times are UTC on the way out; inputs without an offset are IST.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from app.schemas.events import as_aware, invalid
from app.services.ticket_rules import (
    MAX_CAPACITY,
    MAX_CATEGORIES,
    MAX_NAME_LENGTH,
    MAX_PER_BOOKING,
    MAX_PRICE_PAISE,
)

# Strict: "100", 99.5 and true are rejected rather than coerced into a price or a seat count.
Paise = Annotated[StrictInt, Field(ge=0, le=MAX_PRICE_PAISE)]
Seats = Annotated[StrictInt, Field(ge=1, le=MAX_CAPACITY)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TicketCategoryIn(_Input):
    id: uuid.UUID | None = Field(None, description="An existing category of this event to keep; omit for a new one")
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(None, max_length=300)
    price_paise: Paise = Field(description="Standard (non-member) price per ticket in paise")
    capacity: Seats
    member_discount_eligible: StrictBool = Field(True, description="Whether MemberCore's member discount applies")
    is_visible: StrictBool = True
    # Optional narrower window for this category, inside the event's. Both or neither. Leaving
    # them out keeps what the category already has; null clears them back to the event's window.
    sales_open_at: datetime | None = None
    sales_close_at: datetime | None = None

    @field_validator("description")
    @classmethod
    def _blank_is_none(cls, value):
        return value or None

    @field_validator("sales_open_at", "sales_close_at")
    @classmethod
    def _aware(cls, value):
        return as_aware(value) if value is not None else None

    @model_validator(mode="after")
    def _window_pair(self):
        if (self.sales_open_at is None) != (self.sales_close_at is None):
            raise invalid("Give a category both a sales open and a sales close time, or neither")
        if self.sales_open_at is not None and self.sales_close_at <= self.sales_open_at:
            raise invalid("A category's sales must close after they open")
        return self


class TicketConfigIn(_Input):
    """The whole configuration, replaced at once so the capacity partition is checked as a unit.
    Existing categories left out are removed (refused once sales have started)."""

    sales_enabled: StrictBool
    capacity: Seats
    opens_at: datetime
    closes_at: datetime
    max_per_booking: Annotated[StrictInt, Field(ge=1, le=MAX_PER_BOOKING)] = MAX_PER_BOOKING
    categories: list[TicketCategoryIn] = Field(default_factory=list, max_length=MAX_CATEGORIES)

    @field_validator("opens_at", "closes_at")
    @classmethod
    def _aware(cls, value):
        return as_aware(value)

    @model_validator(mode="after")
    def _consistent(self):
        if self.closes_at <= self.opens_at:
            raise invalid("Booking must close after it opens")
        names = [c.name.casefold() for c in self.categories]
        if len(set(names)) != len(names):
            raise invalid("Two ticket categories have the same name")
        ids = [c.id for c in self.categories if c.id]
        if len(set(ids)) != len(ids):
            raise invalid("A ticket category is listed twice")
        return self


class ProblemOut(BaseModel):
    code: str
    message: str


class TicketCategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    price_paise: int
    capacity: int
    member_discount_eligible: bool
    is_visible: bool
    sort_order: int
    sales_open_at: datetime | None = Field(None, description="The category's own window; null = the event's")
    sales_close_at: datetime | None = None
    seats_committed: int | None = Field(None, description="Confirmed seats plus unexpired holds")


SaleState = Literal["not_configured", "off", "unpublished", "not_open", "open", "closed"]


class TicketConfigOut(BaseModel):
    """Organizer view of an event's ticket settings."""

    event_id: uuid.UUID
    club_id: uuid.UUID
    configured: bool
    sales_enabled: bool = False
    capacity: int | None = None
    allocated: int = 0
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_per_booking: int | None = None
    sales_activated_at: datetime | None = None
    sale_state: SaleState
    locked: bool = False
    categories: list[TicketCategoryOut] = []
    warnings: list[ProblemOut] = []
    seats_committed: int | None = Field(
        None, description="Seats held or sold, from the reservation service. null = not connected yet"
    )
    can_manage: bool = False
    updated_at: datetime | None = None
    updated_by: uuid.UUID | None = None


# --- Discovery (read-only, for FairSeat) ------------------------------------------------


class DiscoveryCategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    price_paise: int
    currency: Literal["INR"] = "INR"
    member_discount_eligible: bool
    capacity: int
    max_per_booking: int = Field(description="Most tickets of this category one booking may ask for")
    seats_remaining: int | None = Field(None, description="Capacity less confirmed seats and unexpired holds")
    sale_state: Literal["not_open", "open", "closed"] = Field(
        "open", description="This category's own state, after its window is applied inside the event's")
    booking_opens_at: datetime | None = None
    booking_closes_at: datetime | None = None


class DiscoveryEventOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    name: str
    description: str | None
    venue: str | None
    starts_at: datetime
    ends_at: datetime
    status: Literal["published"]
    booking_opens_at: datetime
    booking_closes_at: datetime
    sale_state: Literal["not_open", "open", "closed"]
    categories: list[DiscoveryCategoryOut]
    availability: Literal["unavailable", "live"] = Field(
        "unavailable", description="'live' when seats_remaining comes from the reservation service"
    )
