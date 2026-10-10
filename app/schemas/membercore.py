import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_PRICE_PAISE = 999_999_999_999


class PlanIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    price_paise: int = Field(ge=0, le=MAX_PRICE_PAISE, description="Whole paise. 0 means an officer approves each member")
    validity_days: int = Field(ge=1, le=3650)
    ticket_discount_bps: int = Field(0, ge=0, le=10000, description="Basis points off ticket prices: 1000 = 10%")
    is_active: bool = True


class PlanUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    price_paise: int | None = Field(None, ge=0, le=MAX_PRICE_PAISE)
    validity_days: int | None = Field(None, ge=1, le=3650)
    ticket_discount_bps: int | None = Field(None, ge=0, le=10000)
    is_active: bool | None = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    price_paise: int
    currency: str
    validity_days: int
    ticket_discount_bps: int
    is_active: bool


class EnrollIn(BaseModel):
    plan_id: uuid.UUID


class MembershipOut(BaseModel):
    """A person's standing. `status` is what is true now: an active row past its expiry reads expired."""

    membership_id: uuid.UUID | None = None
    user_id: uuid.UUID
    full_name: str = ""
    email: str = ""
    status: Literal["none", "pending", "active", "cancelled", "expired"] = "none"
    plan: PlanOut | None = None
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    discount_bps: int = 0
    # What is still owed for the latest enrollment or renewal, if anything.
    awaiting: Literal["payment", "approval"] | None = None
    awaiting_plan: PlanOut | None = None
    amount_due_paise: int = 0
    new_expires_at: datetime | None = None


class MembershipCheckoutOut(BaseModel):
    provider: Literal["stripe", "mock"]
    is_mock: bool
    session_id: str
    checkout_url: str | None
    amount: int = Field(description="In paise")
    currency: str


class SessionIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    session_id: str = Field(min_length=5, max_length=200)
    outcome: Literal["success", "failure"] = "success"


class PaymentResult(BaseModel):
    outcome: str
    membership: MembershipOut


class ReasonIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=500)


class OfflinePaymentIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reference: str = Field(min_length=3, max_length=200, description="Receipt number or how the money was received")
