import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk

ENTRY_TYPES = ("income", "expense", "reimbursement", "refund", "adjustment")
SOURCE_MODULES = ("membership", "event_ticket", "merchandise", "expense_claim", "fundraiser", "manual")
PAYMENT_STATUSES = ("pending", "approved", "paid", "rejected", "refunded")
EXPENSE_CATEGORIES = ("supplies", "food", "travel", "printing", "decoration", "other")


class LedgerEntry(Base):
    """One immutable line in the club's books. A database trigger rejects edits and deletes."""

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    entry_type: Mapped[str] = mapped_column(Text)
    source_module: Mapped[str] = mapped_column(Text)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reference_type: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(Text, default="INR")
    direction: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    payer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payment_status: Mapped[str] = mapped_column(Text)
    receipt_url: Mapped[str | None] = mapped_column(Text)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ApprovalLog(Base):
    __tablename__ = "approval_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    expense_claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    approver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    approver_role: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created_at_col()
