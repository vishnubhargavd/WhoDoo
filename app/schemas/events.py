import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.services.syncsphere import IST

EventStatus = Literal["draft", "published", "cancelled", "completed"]
MAX_DURATION = timedelta(hours=72)


def as_aware(value: datetime | None) -> datetime | None:
    """Normalize to UTC. Times without an offset (as sent by <input type=datetime-local>) are club-local IST."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(timezone.utc)


def invalid(message: str) -> PydanticCustomError:
    """A validation error whose message reaches the user as written (no "Value error, " prefix)."""
    return PydanticCustomError("invalid", message)


def check_window(starts_at: datetime | None, ends_at: datetime | None) -> None:
    if starts_at and ends_at:
        if ends_at <= starts_at:
            raise ValueError("The event must end after it starts")
        if ends_at - starts_at > MAX_DURATION:
            raise ValueError("An event cannot be longer than 72 hours")


class _Input(BaseModel):
    # Unknown fields (club_id, status, created_by...) are rejected, never silently trusted.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("starts_at", "ends_at", check_fields=False)
    @classmethod
    def _aware(cls, value):
        return as_aware(value)

    @field_validator("description", "venue", check_fields=False)
    @classmethod
    def _blank_is_none(cls, value):
        return value or None



class EventCreate(_Input):
    name: str = Field(min_length=3, max_length=120)
    description: str | None = Field(None, max_length=2000)
    starts_at: datetime
    ends_at: datetime
    venue: str | None = Field(None, max_length=120)
    lead_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _window(self):
        try:
            check_window(self.starts_at, self.ends_at)
        except ValueError as exc:
            raise invalid(str(exc)) from exc
        return self


class EventUpdate(_Input):
    name: str | None = Field(None, min_length=3, max_length=120)
    description: str | None = Field(None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue: str | None = Field(None, max_length=120)
    lead_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _window(self):
        try:
            check_window(self.starts_at, self.ends_at)
        except ValueError as exc:
            raise invalid(str(exc)) from exc
        return self


class EventStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: EventStatus


class TaskProgress(BaseModel):
    total: int = 0
    todo: int = 0
    in_progress: int = 0
    blocked: int = 0
    done: int = 0
    at_risk: int = 0

    @property
    def percent_done(self) -> int:
        return round(self.done * 100 / self.total) if self.total else 0


class CrewMember(BaseModel):
    """Someone with at least one task on the event, and how their share is going."""

    id: uuid.UUID
    full_name: str
    role: str
    tasks_total: int = 0
    tasks_done: int = 0
    tasks_at_risk: int = 0


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    club_id: uuid.UUID
    name: str
    description: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    event_date: date | None
    venue: str | None
    status: str
    lead_id: uuid.UUID | None
    lead_name: str | None = None
    created_by: uuid.UUID | None
    created_by_name: str | None = None
    created_at: datetime
    updated_at: datetime
    can_manage: bool = False
    allowed_statuses: list[str] = []
    progress: TaskProgress | None = None
    crew: list[CrewMember] = []


# --- Event Autopilot and SyncSphere -----------------------------------------------


class FactorOut(BaseModel):
    code: str
    severity: str
    points: int
    title: str
    explanation: str
    action: str
    evidence: list[str]


class ReadinessOut(BaseModel):
    event_id: uuid.UUID
    applicable: bool
    score: int | None
    band: str | None
    summary: str
    factors: list[FactorOut]
    stats: dict
    generated_at: datetime
    method: str = "Deterministic rules over this event's data; no AI or guessing."


class ConflictOut(BaseModel):
    kind: str
    other_event_id: uuid.UUID
    other_event_name: str
    overlap_start: datetime
    overlap_end: datetime
    reasons: list[str]
    explanation: str
    other_visible: bool = True


class SlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime
    explanation: str


class ScheduleCheckOut(BaseModel):
    event_id: uuid.UUID
    checked: bool
    message: str
    conflicts: list[ConflictOut]
    unchecked_events: list[str]
    suggestions: list[SlotOut]
    suggestion_note: str
    scope: str = "Compared with this club's own events only."
