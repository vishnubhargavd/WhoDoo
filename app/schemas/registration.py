import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.events import invalid

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_STUDENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_-]{2,29}$")


class VenueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    capacity: int
    location: str | None


class RegistrationIn(BaseModel):
    """What a student types. Nothing here can set a price, a status or an event's limit."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=10, max_length=20)
    email: str = Field(min_length=5, max_length=200)
    student_id: str = Field(min_length=3, max_length=30)
    # Hidden on the page. A person leaves it empty; a form-filling bot usually does not.
    website: str = Field("", max_length=200)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        digits = re.sub(r"[\s()-]", "", value)
        if digits.startswith("+91"):
            digits = digits[3:]
        if not re.fullmatch(r"[6-9]\d{9}", digits):
            raise invalid("Enter a 10-digit Indian mobile number")
        return digits

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        if not _EMAIL.match(value):
            raise invalid("Enter a valid email address")
        return value.lower()

    @field_validator("student_id")
    @classmethod
    def _student_id(cls, value: str) -> str:
        if not _STUDENT_ID.match(value):
            raise invalid("Enter your college student ID (letters and numbers)")
        return value.upper()


class PublicEventOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    club_name: str
    starts_at: datetime
    ends_at: datetime
    venue: str | None
    venue_location: str | None = None
    registration_mode: Literal["none", "free", "paid"]
    price_paise: int
    max_participants: int | None
    registered: int = 0
    places_left: int | None = None
    # open: can register now.  full: every place is taken.  closed: the event has started.
    # none: this event does not take public registrations.
    registration_state: Literal["open", "full", "closed", "none"]


class RegistrationOut(BaseModel):
    """A participant's own registration. `ticket_url` is the private link to their ticket."""

    participant_code: str
    participant_no: int
    full_name: str
    email: str
    status: Literal["pending_payment", "registered", "checked_in", "cancelled", "expired"]
    amount_paise: int
    hold_expires_at: datetime | None
    event_id: uuid.UUID
    event_name: str
    ticket_url: str
    resumed: bool = Field(False, description="True when this person already had a place and it is returned again")


class RegistrationCheckoutOut(BaseModel):
    provider: Literal["stripe", "mock"]
    is_mock: bool
    session_id: str
    checkout_url: str | None
    amount: int
    currency: str


class RegistrationMock(BaseModel):
    outcome: Literal["success", "failure"] = "success"
