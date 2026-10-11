"""MemberCore tables (migrations/030-032). Money is whole paise.

The database enforces most of the rules here: a membership cannot be active without an
activation grant, and renewals, dues, reconciliations and grants cannot be edited.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col

MEMBERSHIP_STATUSES = ("pending", "active", "cancelled", "expired")


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _uuid(nullable: bool = False):
    return mapped_column(UUID(as_uuid=True), nullable=nullable)


def _moment(nullable: bool = False):
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class MembershipPlan(Base):
    __tablename__ = "membership_plans"

    id: Mapped[uuid.UUID] = _id()
    club_id: Mapped[uuid.UUID] = _uuid()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text, default="INR")
    validity_days: Mapped[int] = mapped_column(Integer)
    ticket_discount_bps: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Membership(Base):
    """One person's current standing with the club. History is in MembershipRenewal."""

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _uuid()
    club_id: Mapped[uuid.UUID] = _uuid()
    plan_id: Mapped[uuid.UUID] = _uuid()
    status: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = _moment()
    expires_at: Mapped[datetime] = _moment()
    discount_bps_snapshot: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class MembershipRenewal(Base):
    __tablename__ = "membership_renewals"

    id: Mapped[uuid.UUID] = _id()
    membership_id: Mapped[uuid.UUID] = _uuid()
    club_id: Mapped[uuid.UUID] = _uuid()
    renewal_number: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[uuid.UUID] = _uuid()
    plan_id: Mapped[uuid.UUID] = _uuid()
    previous_starts_at: Mapped[datetime | None] = _moment(nullable=True)
    previous_expires_at: Mapped[datetime | None] = _moment(nullable=True)
    new_starts_at: Mapped[datetime] = _moment()
    new_expires_at: Mapped[datetime] = _moment()
    discount_bps_snapshot: Mapped[int] = mapped_column(Integer)
    actor_user_id: Mapped[uuid.UUID | None] = _uuid(nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class MembershipDues(Base):
    __tablename__ = "membership_dues"

    id: Mapped[uuid.UUID] = _id()
    membership_id: Mapped[uuid.UUID] = _uuid()
    club_id: Mapped[uuid.UUID] = _uuid()
    renewal_id: Mapped[uuid.UUID] = _uuid()
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text, default="INR")
    idempotency_key: Mapped[uuid.UUID] = _uuid()
    created_at: Mapped[datetime] = created_at_col()


class MembershipDuesReconciliation(Base):
    """Evidence that dues were paid, failed or refunded. Append-only."""

    __tablename__ = "membership_dues_reconciliations"

    id: Mapped[uuid.UUID] = _id()
    dues_id: Mapped[uuid.UUID] = _uuid()
    club_id: Mapped[uuid.UUID] = _uuid()
    source: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    provider_event_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[uuid.UUID] = _uuid()
    outcome: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime] = _moment()
    verified_by_user_id: Mapped[uuid.UUID | None] = _uuid(nullable=True)
    evidence_reference: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class MembershipActivationGrant(Base):
    """The proof a membership needs before it may become active."""

    __tablename__ = "membership_activation_grants"

    id: Mapped[uuid.UUID] = _id()
    membership_id: Mapped[uuid.UUID] = _uuid()
    club_id: Mapped[uuid.UUID] = _uuid()
    renewal_id: Mapped[uuid.UUID] = _uuid()
    source: Mapped[str] = mapped_column(Text)
    dues_id: Mapped[uuid.UUID | None] = _uuid(nullable=True)
    reconciliation_id: Mapped[uuid.UUID | None] = _uuid(nullable=True)
    waived_by_user_id: Mapped[uuid.UUID | None] = _uuid(nullable=True)
    waiver_reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[uuid.UUID] = _uuid()
    created_at: Mapped[datetime] = created_at_col()


class MembershipCheckout(Base):
    __tablename__ = "membership_checkouts"

    id: Mapped[uuid.UUID] = _id()
    dues_id: Mapped[uuid.UUID] = _uuid()
    club_id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = _uuid()
    provider: Mapped[str] = mapped_column(Text)
    checkout_session_id: Mapped[str] = mapped_column(Text, unique=True)
    payment_intent_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="open")
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
