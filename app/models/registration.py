"""Venues and public event registrations (migrations/012_public_events.sql). Money is whole paise."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk

REGISTRATION_MODES = ("none", "free", "paid")
# Registrations that hold a place.
LIVE = ("pending_payment", "registered", "checked_in")


def _moment():
    return mapped_column(DateTime(timezone=True), nullable=True)


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text)
    capacity: Mapped[int] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()


class EventRegistration(Base):
    __tablename__ = "event_registrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("events.id"))
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clubs.id"))
    participant_no: Mapped[int] = mapped_column(Integer)
    participant_code: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    student_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text, default="INR")
    hold_expires_at: Mapped[datetime | None] = _moment()
    checkout_session_id: Mapped[str | None] = mapped_column(Text, unique=True)
    payment_intent_id: Mapped[str | None] = mapped_column(Text, unique=True)
    paid_at: Mapped[datetime | None] = _moment()
    refund_status: Mapped[str | None] = mapped_column(Text)
    credential_digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    access_digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    checked_in_at: Mapped[datetime | None] = _moment()
    checked_in_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    email_sent_at: Mapped[datetime | None] = _moment()
    # Migration 013.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    email_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
