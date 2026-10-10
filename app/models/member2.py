"""Batch A tables (migrations/pending/040-042): profiles, UPI ids, payouts, reminders, budgets."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _uuid():
    return mapped_column(UUID(as_uuid=True))


def _moment(nullable: bool = True):
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    club_id: Mapped[uuid.UUID] = _uuid()
    display_name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    academic_year: Mapped[int | None] = mapped_column(SmallInteger)
    photo_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_at_col()


class UserPaymentProfile(Base):
    __tablename__ = "user_payment_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    club_id: Mapped[uuid.UUID] = _uuid()
    upi_ciphertext: Mapped[str] = mapped_column(Text)
    upi_masked: Mapped[str] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(Text, default="unverified")
    verification_provider: Mapped[str | None] = mapped_column(Text)
    verification_reference: Mapped[str | None] = mapped_column(Text)
    registered_name: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = _moment()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ReimbursementPayout(Base):
    __tablename__ = "reimbursement_payouts"

    id: Mapped[uuid.UUID] = _id()
    club_id: Mapped[uuid.UUID] = _uuid()
    expense_claim_id: Mapped[uuid.UUID] = _uuid()
    payee_id: Mapped[uuid.UUID] = _uuid()
    initiated_by: Mapped[uuid.UUID] = _uuid()
    attempt: Mapped[int] = mapped_column(Integer)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text, default="INR")
    upi_masked: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="initiated")
    provider_payout_id: Mapped[str | None] = mapped_column(Text)
    utr: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ProviderWebhookReceipt(Base):
    __tablename__ = "provider_webhook_receipts"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    event_key: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = created_at_col()


class MembershipRenewalReminder(Base):
    __tablename__ = "membership_renewal_reminders"

    id: Mapped[uuid.UUID] = _id()
    club_id: Mapped[uuid.UUID] = _uuid()
    membership_id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = _uuid()
    plan_id: Mapped[uuid.UUID] = _uuid()
    stage: Mapped[str] = mapped_column(Text)
    term_expires_at: Mapped[datetime] = _moment(nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    read_at: Mapped[datetime | None] = _moment()


class EventBudget(Base):
    __tablename__ = "event_budgets"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    club_id: Mapped[uuid.UUID] = _uuid()
    budget_paise: Mapped[int] = mapped_column(BigInteger)
    updated_by: Mapped[uuid.UUID] = _uuid()
    updated_at: Mapped[datetime] = updated_at_col()


class CrewTaskShift(Base):
    """migrations/pending/043: the time slot of a CrewPulse task, when it has one."""

    __tablename__ = "crew_task_shifts"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    club_id: Mapped[uuid.UUID] = _uuid()
    starts_at: Mapped[datetime] = _moment(nullable=False)
    ends_at: Mapped[datetime] = _moment(nullable=False)
