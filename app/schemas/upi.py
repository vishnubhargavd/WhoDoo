import re
import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator

UPI_REGEX = re.compile(r"^[-a-zA-Z0-9._]{2,120}@[a-zA-Z]{2,64}$")


class UPIProfileUpdate(BaseModel):
    upi_id: str | None = Field(None, description="VPA handle, e.g. user@okaxis")
    preferred_payout_method: Literal["upi", "bank_transfer", "cash_manual"] = "upi"
    bank_account_number: str | None = None
    bank_ifsc: str | None = None

    @field_validator("upi_id")
    @classmethod
    def validate_upi_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        clean = v.strip().lower()
        if not clean:
            return None
        if not UPI_REGEX.match(clean):
            raise ValueError("Invalid UPI ID syntax. Must match username@bankhandle (e.g., student@okaxis).")
        return clean


class UPIProfileOut(BaseModel):
    user_id: uuid.UUID
    upi_id_masked: str | None
    preferred_payout_method: str
    is_syntax_valid: bool
    verification_status: str
    bank_account_masked: str | None = None
    bank_ifsc: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RazorpayOrderCreate(BaseModel):
    reference_type: Literal["membership_dues", "ticket_booking", "merch_order", "club_bill"]
    reference_id: uuid.UUID
    amount_paise: int = Field(..., gt=0)
    currency: str = "INR"


class RazorpayOrderOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    rzp_order_id: str
    amount_paise: int
    currency: str
    key_id: str
    status: str
    reference_type: str
    reference_id: uuid.UUID


class RazorpayPaymentVerify(BaseModel):
    rzp_order_id: str
    rzp_payment_id: str
    rzp_signature: str


class ReimbursementPayoutRequest(BaseModel):
    claim_id: uuid.UUID
    mode: Literal["upi", "bank_transfer", "manual"] = "upi"
    notes: str | None = None
