import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk


class UserPaymentProfile(Base):
    __tablename__ = "user_payment_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    upi_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_payout_method: Mapped[str] = mapped_column(Text, default="upi", nullable=False)
    bank_account_number_masked: Mapped[str | None] = mapped_column(Text, nullable=True)
    bank_ifsc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RazorpayOrder(Base):
    __tablename__ = "razorpay_orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reference_type: Mapped[str] = mapped_column(Text, nullable=False)  # membership_dues, ticket_booking, merch_order, club_bill
    reference_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rzp_order_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    rzp_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rzp_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(Text, default="created", nullable=False)  # created, attempted, paid, failed
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReimbursementPayout(Base):
    __tablename__ = "reimbursement_payouts"

    id: Mapped[uuid.UUID] = uuid_pk()
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("expense_claims.id", ondelete="RESTRICT"), nullable=False)
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    claimant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout_mode: Mapped[str] = mapped_column(Text, nullable=False)  # upi, bank_transfer, manual
    destination_upi: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(Text, default="manual", nullable=False)  # razorpayx, manual
    provider_payout_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)  # pending, processing, paid, failed
    initiated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    initiated_at: Mapped[datetime] = created_at_col()
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

