"""Request and response shapes from Member 2's Batch A: profiles, UPI ids, reminders and the event budget."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.schemas.events import invalid


class _In(BaseModel):
    # Unknown fields are refused, so nobody can slip `role`, `email` or a membership status in.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfileUpdate(_In):
    full_name: str | None = Field(None, min_length=1, max_length=120)
    display_name: str | None = Field(None, max_length=60)
    phone: str | None = Field(None, max_length=20)
    department: str | None = Field(None, max_length=80)
    academic_year: int | None = Field(None, ge=1, le=6)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        if not value:
            return None
        digits = value.replace(" ", "").replace("-", "")
        if not (digits.lstrip("+").isdigit() and 10 <= len(digits.lstrip("+")) <= 15 and digits.count("+") <= 1
                and (not digits.count("+") or digits.startswith("+"))):
            raise invalid("Enter a phone number of 10 to 15 digits, optionally starting with +")
        return digits

    @field_validator("display_name", "department")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value or None


class PaymentProfileIn(_In):
    upi_id: str = Field(min_length=3, max_length=320)


class PaymentProfileOut(BaseModel):
    upi_masked: str
    verification_status: Literal["unverified", "verified", "invalid"]
    verification_provider: str | None = None
    verified_by_mock: bool = False
    registered_name: str | None = None
    verified_at: datetime | None = None
    updated_at: datetime | None = None


class ProfileOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: str
    display_name: str | None = None
    phone: str | None = None
    department: str | None = None
    academic_year: int | None = None
    payment: PaymentProfileOut | None = None
    profile_available: bool = True


class ReminderOut(BaseModel):
    id: uuid.UUID
    stage: str
    title: str
    message: str
    plan_name: str | None
    club_name: str | None
    expires_at: datetime
    renew_url: str = "/memberships"
    created_at: datetime
    read: bool


class BudgetIn(_In):
    budget_paise: int = Field(ge=0, le=999_999_999_999)


class BudgetOut(BaseModel):
    event_id: uuid.UUID
    budget_paise: int | None = None
    spent_paise: int = 0                # paid expenses and reimbursements
    committed_paise: int = 0            # approved, not yet paid out
    pending_paise: int = 0              # expense claims still awaiting approval
    remaining_paise: int | None = None  # budget - spent - committed
    over_budget: bool = False
    warning: str | None = None
    available: bool = True
