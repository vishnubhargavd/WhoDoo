import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import Money, ORMModel, PositiveMoney

EntryType = Literal["income", "expense", "reimbursement", "refund", "adjustment"]
SourceModule = Literal["membership", "event_ticket", "merchandise", "expense_claim", "fundraiser", "manual"]
Direction = Literal["credit", "debit"]
PaymentStatus = Literal["pending", "approved", "paid", "rejected", "refunded"]


class LedgerEntryOut(ORMModel):
    id: uuid.UUID
    club_id: uuid.UUID
    entry_type: EntryType
    source_module: SourceModule
    reference_id: uuid.UUID | None
    reference_type: str | None
    amount: Money
    currency: str
    direction: Direction
    category: str | None
    description: str | None
    payer_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    approved_by: uuid.UUID | None
    payment_status: PaymentStatus
    receipt_url: str | None
    event_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class LedgerFilters(BaseModel):
    """Query filters for the ledger. Blank strings (from HTML forms) count as "not set"."""

    date_from: date | None = None
    date_to: date | None = None
    entry_type: EntryType | None = None
    source_module: SourceModule | None = None
    event_id: uuid.UUID | None = None
    payment_status: PaymentStatus | None = None
    category: str | None = None
    q: str | None = Field(default=None, description="Search in description, category and reference type")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def defaults_for_blank_paging(self):
        self.limit = self.limit or 50
        self.offset = self.offset or 0
        return self


class LedgerEntryPage(BaseModel):
    items: list[LedgerEntryOut]
    total: int
    limit: int
    offset: int


class MonthlyTrendPoint(BaseModel):
    month: str = Field(description="YYYY-MM")
    income: Money
    expenses: Money
    net: Money


class LedgerSummary(BaseModel):
    current_balance: Money
    total_income: Money
    total_expenses: Money
    total_reimbursements_paid: Money
    pending_reimbursements: Money
    total_refunds: Money
    income_by_source: dict[str, Money]
    expenses_by_category: dict[str, Money]
    monthly_trend: list[MonthlyTrendPoint]


class EventProfitLoss(BaseModel):
    event_id: uuid.UUID
    event_name: str | None = None
    ticket_income: Money
    merchandise_income: Money
    other_income: Money
    refunds: Money
    approved_expenses: Money = Field(description="Expenses and reimbursements that are approved or paid")
    paid_reimbursements: Money
    outstanding_reimbursements: Money
    net_profit_loss: Money
    result: Literal["profit", "loss", "break_even"]
    tickets_sold: int
    attendees: int | None = Field(default=None, description="Null until check-in data is available")


class ManualEntryCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    entry_type: Literal["income", "expense", "adjustment"]
    amount: PositiveMoney
    description: str = Field(min_length=3, max_length=500)
    direction: Direction | None = Field(default=None, description="Required for adjustments only")
    source_module: Literal["manual", "membership", "event_ticket", "fundraiser"] = "manual"
    category: str | None = Field(default=None, max_length=60)
    event_id: uuid.UUID | None = None
    payment_status: Literal["pending", "paid"] = "paid"

    @field_validator("direction", "category", "event_id", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def direction_matches_type(self):
        if self.entry_type == "adjustment":
            if self.direction is None:
                raise ValueError("direction is required for an adjustment")
        else:
            expected = "credit" if self.entry_type == "income" else "debit"
            if self.direction not in (None, expected):
                raise ValueError(f"{self.entry_type} entries are always {expected}")
            self.direction = expected
        return self
