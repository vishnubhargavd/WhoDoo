import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.events import as_aware, invalid

TaskStatus = Literal["todo", "in_progress", "blocked", "done"]
TaskPriority = Literal["low", "medium", "high"]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("due_at", "starts_at", "ends_at", check_fields=False)
    @classmethod
    def _aware(cls, value):
        return as_aware(value)

    @field_validator("description", "blocker_note", "override_reason", check_fields=False)
    @classmethod
    def _blank_is_none(cls, value):
        return value or None


class TaskCreate(_Input):
    title: str = Field(min_length=3, max_length=120)
    description: str | None = Field(None, max_length=1000)
    assignee_id: uuid.UUID | None = None
    priority: TaskPriority = "medium"
    is_critical: bool = False
    due_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    override_clash: bool = False
    override_reason: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _schedule_order(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise invalid("Duty end time must be after start time")
        return self


class TaskUpdate(_Input):
    """Organizer edits. `unassign: true` clears the assignee (a missing field means "no change")."""

    title: str | None = Field(None, min_length=3, max_length=120)
    description: str | None = Field(None, max_length=1000)
    assignee_id: uuid.UUID | None = None
    unassign: bool = False
    priority: TaskPriority | None = None
    is_critical: bool | None = None
    due_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    override_clash: bool = False
    override_reason: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _validate_update(self):
        if self.unassign and self.assignee_id:
            raise invalid("Choose either an assignee or unassign, not both")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise invalid("Duty end time must be after start time")
        return self


class TaskStatusChange(_Input):
    status: TaskStatus
    blocker_note: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _note_for_blockers(self):
        if self.status == "blocked" and not self.blocker_note:
            raise invalid("Say what is blocking the task")
        return self


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    event_name: str | None = None
    event_starts_at: datetime | None = None
    title: str
    description: str | None
    assignee_id: uuid.UUID | None
    assignee_name: str | None = None
    priority: str
    is_critical: bool
    status: str
    blocker_note: str | None
    due_at: datetime | None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    risks: list[str] = []
    clashes: list[str] = Field(default_factory=list, description="Integration point for volunteer clash review")
    can_edit: bool = False
    can_update_status: bool = False


class ClashCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID | None = None
    assignee_id: uuid.UUID
    title: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("starts_at", "ends_at", check_fields=False)
    @classmethod
    def _aware(cls, value):
        return as_aware(value)


class ClashCheckOut(BaseModel):
    has_clashes: bool
    is_exact_duplicate: bool
    can_override: bool
    clashes: list[str] = Field(default_factory=list)
    clash_details: list[dict] = Field(default_factory=list)



class TaskActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: str
    actor_id: uuid.UUID
    actor_name: str | None = None
    from_value: str | None
    to_value: str | None
    note: str | None
    created_at: datetime


class AssigneeOut(BaseModel):
    id: uuid.UUID
    full_name: str
    role: str
