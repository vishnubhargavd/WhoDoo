import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk


class EventTicketSettings(Base):
    """One row per ticketed event (migrations/012_event_tickets.sql). Also the row the
    reservation service locks (SELECT ... FOR UPDATE) to serialize capacity changes."""

    __tablename__ = "event_ticket_settings"
    __table_args__ = (ForeignKeyConstraint(["event_id", "club_id"], ["events.id", "events.club_id"]),)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    sales_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    capacity: Mapped[int] = mapped_column(Integer)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_per_booking: Mapped[int] = mapped_column(Integer, default=10)
    sales_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    updated_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class EventTicketCategory(Base):
    __tablename__ = "event_ticket_categories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "club_id"], ["event_ticket_settings.event_id", "event_ticket_settings.club_id"]
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_paise: Mapped[int] = mapped_column(BigInteger)
    capacity: Mapped[int] = mapped_column(Integer)
    member_discount_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
