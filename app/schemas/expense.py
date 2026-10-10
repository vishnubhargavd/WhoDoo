import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Money, ORMModel, PositiveMoney

ExpenseCategory = Literal["supplies", "food", "travel", "printing", "decoration", "other"]
ClaimStatus = Literal["draft", "pending_approval", "approved", "rejected", "change_requested", "paid"]
ClaimAction = Literal["approve", "reject", "request_changes", "mark_paid", "resubmit"]


class OcrResponse(BaseModel):
    """Result of uploading a receipt. Nothing is claimed yet: pass `receipt_id` to POST /expenses."""

    receipt_id: uuid.UUID
    receipt_url: str
    ocr_available: bool = Field(description="False when OCR could not read the file; enter the fields by hand")
    ocr_confidence: float = Field(description="Mean word confidence from the OCR engine, 0 to 100")
    extracted_vendor: str | None
    extracted_amount: Money | None
    extracted_date: date | None
    message: str


class _ClaimFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=1000)
    category: ExpenseCategory
    amount: PositiveMoney
    expense_date: date
    vendor_name: str | None = Field(default=None, max_length=120)
    event_id: uuid.UUID | None = None

    @field_validator("vendor_name", "event_id", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("expense_date")
    @classmethod
    def not_in_the_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("expense date cannot be in the future")
        return value


class ExpenseClaimCreate(_ClaimFields):
    receipt_id: uuid.UUID = Field(description="From POST /api/v1/expenses/ocr")


class ExpenseClaimResubmit(_ClaimFields):
    receipt_id: uuid.UUID | None = Field(default=None, description="Only when replacing the receipt")

    @field_validator("receipt_id", mode="before")
    @classmethod
    def blank_receipt_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ApproveBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    comment: str | None = Field(default=None, max_length=500)


class RejectBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=500)


class RequestChangesBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    comment: str = Field(min_length=3, max_length=500)


class ApprovalLogOut(ORMModel):
    id: uuid.UUID
    approver_id: uuid.UUID
    approver_name: str | None = None
    approver_role: str
    action: Literal["submitted", "approved", "rejected", "change_requested", "paid"]
    comment: str | None
    created_at: datetime


class ExpenseClaimOut(ORMModel):
    id: uuid.UUID
    club_id: uuid.UUID
    submitted_by: uuid.UUID
    submitted_by_name: str | None = None
    event_id: uuid.UUID | None
    event_name: str | None = None
    title: str
    description: str | None
    category: ExpenseCategory
    amount: Money
    expense_date: date
    vendor_name: str | None
    receipt_url: str
    ocr_confidence: float | None
    extracted_amount: Money | None
    extracted_vendor: str | None
    extracted_date: date | None
    status: ClaimStatus
    approval_stage: Literal["first", "final"]
    needs_final_approval: bool = Field(description="True when the amount is at or above the approval limit")
    current_approver_id: uuid.UUID | None
    current_approver_name: str | None = None
    final_approver_id: uuid.UUID | None
    rejection_reason: str | None
    allowed_actions: list[ClaimAction] = Field(description="What the caller may do to this claim right now")
    created_at: datetime
    updated_at: datetime


class ExpenseClaimDetail(ExpenseClaimOut):
    ocr_raw_response: dict[str, Any] | None
    approval_history: list[ApprovalLogOut]
    ledger_entry_id: uuid.UUID | None = None
    ledger_payment_status: str | None = None


class ApprovalQueue(BaseModel):
    to_approve: list[ExpenseClaimOut]
    to_pay: list[ExpenseClaimOut] = Field(description="Approved claims waiting to be paid (treasurer and president)")
