import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Money, ORMModel, PositiveMoney

ExpenseCategory = Literal["supplies", "food", "travel", "printing", "decoration", "other"]
ClaimStatus = Literal["draft", "pending_approval", "approved", "rejected", "change_requested", "paid"]
ClaimAction = Literal["approve", "reject", "request_changes", "mark_paid", "resubmit"]


class OcrLineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: Money | None = None
    amount: Money = Field(description="The line's amount as printed on the receipt")


class OcrResponse(BaseModel):
    """Result of uploading a receipt. Nothing is claimed yet: pass `receipt_id` to POST /expenses.

    Every `extracted_*` value is a reading for the user to confirm, and is null when it could
    not be read. `extracted_amount` is only ever the figure on the receipt's total line.
    """

    receipt_id: uuid.UUID
    receipt_url: str
    ocr_available: bool = Field(description="False when OCR could not read the file; enter the fields by hand")
    ocr_confidence: float = Field(
        description="Tesseract's mean word confidence for the text it read, 0 to 100. "
        "It measures how clearly the characters were read, not whether the fields are right."
    )
    extracted_vendor: str | None
    extracted_amount: Money | None = Field(description="Final total from the receipt's total line, else null")
    extracted_date: date | None
    amount_status: Literal["read_from_total_line", "not_found"]
    suggested_amount: Money | None = Field(
        default=None,
        description="Offered only when the total could not be read: what the readable lines add up to. "
        "Never applied without the user choosing it.",
    )
    line_items: list[OcrLineItem] = []
    items_total: Money | None = None
    subtotal: Money | None = None
    tax_total: Money | None = None
    discount_total: Money | None = None
    warnings: list[str] = []
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
    first_approved_by: uuid.UUID | None = Field(description="Who gave the first of two approvals, if any")
    first_approved_by_name: str | None = None
    final_approver_id: uuid.UUID | None
    final_approver_name: str | None = None
    approval_blocked_reason: str | None = Field(
        default=None, description="For approvers: why the caller cannot approve this pending claim, if they cannot"
    )
    rejection_reason: str | None
    reviewer_note: str | None = Field(
        default=None, description="What the reviewer asked to be changed, while the claim is waiting for the submitter"
    )
    reviewer_name: str | None = Field(default=None, description="Who asked for the changes")
    allowed_actions: list[ClaimAction] = Field(description="What the caller may do to this claim right now")
    status_label: str = Field(description="Plain-language status, including who the claim is waiting on")
    amount_edited: bool = Field(description="True when the submitter changed the amount OCR read from the receipt")
    created_at: datetime
    updated_at: datetime


class ExpenseClaimDetail(ExpenseClaimOut):
    ocr_raw_response: dict[str, Any] | None
    approval_history: list[ApprovalLogOut]
    ledger_entry_id: uuid.UUID | None = None
    ledger_payment_status: str | None = None


class ApprovalQueue(BaseModel):
    to_approve: list[ExpenseClaimOut]
    awaiting_others: list[ExpenseClaimOut] = Field(
        default=[], description="Claims the caller gave the first approval on, now waiting for a different approver"
    )
    to_pay: list[ExpenseClaimOut] = Field(description="Approved claims waiting to be paid (treasurer and president)")
