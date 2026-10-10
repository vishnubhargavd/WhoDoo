"""TEMPORARY placeholder models for tables owned by other modules.

See migrations/000_shared_placeholders.sql. Only app/services/shared_directory.py
(and the seed script and tests) should import these.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, uuid_pk


class Club(Base):
    __tablename__ = "clubs"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clubs.id"))
    full_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    role: Mapped[str] = mapped_column(Text, default="member")
    is_active_member: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("clubs.id"))
    name: Mapped[str] = mapped_column(Text)
    event_date: Mapped[date | None] = mapped_column(Date)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at_col()
