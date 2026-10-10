import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk

TASK_STATUSES = ("todo", "in_progress", "blocked", "done")
TASK_PRIORITIES = ("low", "medium", "high")


class CrewTask(Base):
    __tablename__ = "crew_tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("events.id"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    priority: Mapped[str] = mapped_column(Text, default="medium")
    is_critical: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(Text, default="todo")
    blocker_note: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrewTaskActivity(Base):
    __tablename__ = "crew_task_activity"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crew_tasks.id"))
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text)
    from_value: Mapped[str | None] = mapped_column(Text)
    to_value: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class CrewTaskClashOverride(Base):
    __tablename__ = "crew_task_clash_overrides"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crew_tasks.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("events.id"))
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    volunteer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    overridden_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    clash_summary: Mapped[str] = mapped_column(Text)
    override_reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

