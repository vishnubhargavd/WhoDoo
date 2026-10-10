import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.merch import OrderOut


class CreatePaymentOrder(BaseModel):
    merch_order_id: uuid.UUID


class PaymentOrderOut(BaseModel):
    """What the checkout needs. The amount is the server's figure for the order, in paise."""

    merch_order_id: uuid.UUID
    order_number: str
    provider: Literal["razorpay", "mock"]
    is_mock: bool
    key_id: str = Field(description="Public key for Razorpay Checkout")
    razorpay_order_id: str
    amount: int = Field(description="In paise, as Razorpay expects")
    currency: str


class VerifyPayment(BaseModel):
    """The three fields Razorpay Checkout hands back. No amount or status is accepted from the client."""

    model_config = ConfigDict(str_strip_whitespace=True)

    razorpay_order_id: str = Field(min_length=5, max_length=80)
    razorpay_payment_id: str = Field(min_length=5, max_length=80)
    razorpay_signature: str = Field(min_length=10, max_length=200)


class VerifyPaymentOut(BaseModel):
    verified: bool
    order: OrderOut


class MockSimulate(BaseModel):
    razorpay_order_id: str
    outcome: Literal["success", "failure"] = "success"


class WebhookAck(BaseModel):
    status: Literal["processed", "ignored"]
    detail: str | None = None
