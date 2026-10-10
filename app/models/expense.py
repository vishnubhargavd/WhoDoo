import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk

CLAIM_STATUSES = ("draft", "pending_approval", "approved", "rejected", "change_requested", "paid")


class ExpenseClaim(Base):
    __tablename__ = "expense_claims"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    submitted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    expense_date: Mapped[date] = mapped_column(Date)
    vendor_name: Mapped[str | None] = mapped_column(Text)
    receipt_url: Mapped[str] = mapped_column(Text)
    ocr_raw_response: Mapped[dict | None] = mapped_column(JSONB)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    extracted_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    extracted_vendor: Mapped[str | None] = mapped_column(Text)
    extracted_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    approval_stage: Mapped[str] = mapped_column(Text, default="first")
    first_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    current_approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    final_approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ReceiptUpload(Base):
    """A stored receipt and what OCR read from it, before it is attached to a claim."""

    __tablename__ = "receipt_uploads"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    receipt_url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    ocr_raw_response: Mapped[dict | None] = mapped_column(JSONB)
    ocr_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    extracted_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    extracted_vendor: Mapped[str | None] = mapped_column(Text)
    extracted_date: Mapped[date | None] = mapped_column(Date)
    expense_claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("expense_claims.id"))
    created_at: Mapped[datetime] = created_at_col()
