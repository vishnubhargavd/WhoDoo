import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col


class EventTicketSettings(Base):
    """One row per ticketed event (migrations/pending/021_fairseat_event_settings.sql). Locked
    after the event row and before its categories by everything that changes seats.

    Ticket categories are app.models.fairseat.TicketCategory: there is one category table."""

    __tablename__ = "event_ticket_settings"
    __table_args__ = (ForeignKeyConstraint(["event_id", "club_id"], ["events.id", "events.club_id"]),)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    sales_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    capacity: Mapped[int] = mapped_column(Integer)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_per_booking: Mapped[int] = mapped_column(Integer, default=6)
    sales_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    updated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
