import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.merch import OrderOut


class CreateCheckoutSession(BaseModel):
    """Only which order to pay for. The amount, buyer and items come from the order row."""

    merch_order_id: uuid.UUID


class CheckoutSessionOut(BaseModel):
    """Where to send the buyer. The amount is the server's figure for the order, in paise."""

    merch_order_id: uuid.UUID
    order_number: str
    provider: Literal["stripe", "razorpay", "mock"]
    is_mock: bool
    session_id: str
    checkout_url: str = Field(description="Stripe's hosted checkout page, or the local mock checkout")
    amount: int = Field(description="In paise, as Stripe expects")
    currency: str


class VerifyPayment(BaseModel):
    """Only the Checkout Session id. No amount or status is accepted from the client."""

    model_config = ConfigDict(str_strip_whitespace=True)

    session_id: str = Field(min_length=5, max_length=200)


class VerifyPaymentOut(BaseModel):
    verified: bool
    order: OrderOut


class MockSimulate(BaseModel):
    session_id: str = Field(min_length=5, max_length=200)
    outcome: Literal["success", "failure"] = "success"


class MockSimulateOut(BaseModel):
    session_id: str
    payment_status: Literal["paid", "unpaid"]


class WebhookAck(BaseModel):
    status: Literal["processed", "ignored", "duplicate"]
    detail: str | None = None
