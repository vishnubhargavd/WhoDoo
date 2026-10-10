"""Tables for GateSync, CrewDoo Connect, CertiDoo and LegacyVault (migrations/pending/033-036)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col

AUDIENCES = ("everyone", "members", "volunteers", "officers", "event")
CERTIFICATE_KINDS = ("participation", "volunteering", "achievement")
HANDOVER_ROLES = ("president", "treasurer", "event_lead", "volunteer", "everyone")


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ref(target: str, nullable: bool = False):
    return mapped_column(UUID(as_uuid=True), ForeignKey(target), nullable=nullable)


def _moment(nullable: bool = False):
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class TicketRedemption(Base):
    """A ticket admitted at the door. The ticket is the primary key: one admission each."""

    __tablename__ = "ticket_redemptions"

    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("event_tickets.id"), primary_key=True)
    event_id: Mapped[uuid.UUID] = _ref("events.id")
    club_id: Mapped[uuid.UUID] = _ref("clubs.id")
    scanned_by: Mapped[uuid.UUID] = _ref("users.id")
    method: Mapped[str] = mapped_column(Text, default="qr")
    checked_in_at: Mapped[datetime] = _moment()


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = _id()
    club_id: Mapped[uuid.UUID] = _ref("clubs.id")
    author_id: Mapped[uuid.UUID] = _ref("users.id")
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text, default="everyone")
    event_id: Mapped[uuid.UUID | None] = _ref("events.id", nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()
    removed_at: Mapped[datetime | None] = _moment(nullable=True)
    removed_by: Mapped[uuid.UUID | None] = _ref("users.id", nullable=True)


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("announcements.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    read_at: Mapped[datetime] = created_at_col()


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[uuid.UUID] = _id()
    club_id: Mapped[uuid.UUID] = _ref("clubs.id")
    recipient_user_id: Mapped[uuid.UUID] = _ref("users.id")
    event_id: Mapped[uuid.UUID | None] = _ref("events.id", nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    recipient_name: Mapped[str] = mapped_column(Text)
    verify_code: Mapped[str] = mapped_column(Text, unique=True)
    issued_by: Mapped[uuid.UUID] = _ref("users.id")
    issued_at: Mapped[datetime] = created_at_col()
    revoked_at: Mapped[datetime | None] = _moment(nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = _ref("users.id", nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text)


class HandoverNote(Base):
    __tablename__ = "handover_notes"

    id: Mapped[uuid.UUID] = _id()
    club_id: Mapped[uuid.UUID] = _ref("clubs.id")
    role: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID] = _ref("users.id")
    updated_by: Mapped[uuid.UUID] = _ref("users.id")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    archived_at: Mapped[datetime | None] = _moment(nullable=True)


class HandoverNoteVersion(Base):
    __tablename__ = "handover_note_versions"

    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("handover_notes.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    saved_by: Mapped[uuid.UUID] = _ref("users.id")
    saved_at: Mapped[datetime] = created_at_col()
