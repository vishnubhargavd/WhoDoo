import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemberProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # Rejects role, club_id, is_active_member, validity, etc.

    full_name: str | None = Field(default=None, max_length=150)
    phone: str | None = Field(default=None, max_length=20)
    department: str | None = Field(default=None, max_length=100)
    academic_year: str | None = Field(default=None, max_length=50)
    profile_image_url: str | None = Field(default=None, max_length=500)


class RenewalReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    membership_id: uuid.UUID
    club_id: uuid.UUID
    plan_name: str
    reminder_stage: str
    expires_at: datetime
    message: str
    action_url: str
    is_dismissed: bool
    created_at: datetime


class MemberProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    club_id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active_member: bool
    phone: str | None = None
    department: str | None = None
    academic_year: str | None = None
    profile_image_url: str | None = None
    upi_id_masked: str | None = None
    active_reminders: list[RenewalReminderOut] = []
