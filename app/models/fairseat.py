"""FairSeat tables (migrations/pending/020_fairseat.sql). Money is whole paise."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col

BOOKING_STATUSES = ("pending_payment", "confirmed", "cancelled", "expired")
BOOKING_PAYMENT_STATUSES = ("pending", "failed", "paid", "not_required", "refunded")
TICKET_STATUSES = ("pending_issue", "issued", "revoked", "checked_in")


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ref(target: str) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(target))


def _moment(nullable: bool = False):
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class TicketCategory(Base):
    __tablename__ = "ticket_categories"

    id: Mapped[uuid.UUID] = _id()
    event_id: Mapped[uuid.UUID] = _ref("events.id")
    name: Mapped[str] = mapped_column(Text)
    base_price_paise: Mapped[int] = mapped_column(BigInteger)
    pricing_version: Mapped[int] = mapped_column(Integer, default=1)
    capacity: Mapped[int] = mapped_column(Integer)
    # The category's own sales window. None means the event's window (event_ticket_settings).
    sales_open_at: Mapped[datetime | None] = _moment(nullable=True)
    sales_close_at: Mapped[datetime | None] = _moment(nullable=True)
    # "Visible" on the event side: a hidden category is paused, not deleted, and keeps its seats.
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)
    member_discount_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_by: Mapped[uuid.UUID] = _ref("users.id")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class TicketQuote(Base):
    __tablename__ = "ticket_quotes"

    id: Mapped[uuid.UUID] = _id()
    purchaser_user_id: Mapped[uuid.UUID] = _ref("users.id")
    event_id: Mapped[uuid.UUID] = _ref("events.id")
    ticket_category_id: Mapped[uuid.UUID] = _ref("ticket_categories.id")
    quantity: Mapped[int] = mapped_column(Integer)
    base_unit_price_paise: Mapped[int] = mapped_column(BigInteger)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger)
    total_price_paise: Mapped[int] = mapped_column(BigInteger)
    member_eligible: Mapped[bool] = mapped_column(Boolean)
    applied_discount_bps: Mapped[int] = mapped_column(Integer)
    discount_applied: Mapped[bool] = mapped_column(Boolean)
    discount_reason: Mapped[str] = mapped_column(Text)
    discount_explanation: Mapped[str] = mapped_column(Text)
    calculator_version: Mapped[str] = mapped_column(Text)
    category_pricing_version: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = _moment()
    expires_at: Mapped[datetime] = _moment()


class TicketBooking(Base):
    __tablename__ = "ticket_bookings"

    id: Mapped[uuid.UUID] = _id()
    event_id: Mapped[uuid.UUID] = _ref("events.id")
    ticket_category_id: Mapped[uuid.UUID] = _ref("ticket_categories.id")
    purchaser_user_id: Mapped[uuid.UUID] = _ref("users.id")
    quote_id: Mapped[uuid.UUID] = _ref("ticket_quotes.id")
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger)
    total_price_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text, default="INR")
    member_eligible: Mapped[bool] = mapped_column(Boolean)
    applied_discount_bps: Mapped[int] = mapped_column(Integer)
    discount_applied: Mapped[bool] = mapped_column(Boolean)
    discount_reason: Mapped[str] = mapped_column(Text)
    discount_explanation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    payment_status: Mapped[str] = mapped_column(Text)
    hold_expires_at: Mapped[datetime | None] = _moment(nullable=True)
    cancelled_at: Mapped[datetime | None] = _moment(nullable=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class TicketBookingCommand(Base):
    """One booking request, keyed by the purchaser's idempotency key."""

    __tablename__ = "ticket_booking_commands"

    id: Mapped[uuid.UUID] = _id()
    purchaser_user_id: Mapped[uuid.UUID] = _ref("users.id")
    event_id: Mapped[uuid.UUID] = _ref("events.id")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    booking_id: Mapped[uuid.UUID] = _ref("ticket_bookings.id")
    created_at: Mapped[datetime] = created_at_col()


class EventTicket(Base):
    """One seat of a confirmed booking. GateSync will attach a credential to it later."""

    __tablename__ = "event_tickets"

    id: Mapped[uuid.UUID] = _id()
    booking_id: Mapped[uuid.UUID] = _ref("ticket_bookings.id")
    event_id: Mapped[uuid.UUID] = _ref("events.id")
    holder_user_id: Mapped[uuid.UUID] = _ref("users.id")
    seat_number: Mapped[int] = mapped_column(Integer)
    credential_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(Text, default="pending_issue")
    issued_at: Mapped[datetime | None] = _moment(nullable=True)
    checked_in_at: Mapped[datetime | None] = _moment(nullable=True)
    revoked_at: Mapped[datetime | None] = _moment(nullable=True)
    created_at: Mapped[datetime] = created_at_col()
