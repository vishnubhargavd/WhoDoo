import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import Money, PositiveMoney

OrderPaymentStatus = Literal["created", "pending", "paid", "failed", "refunded"]
OrderStatus = Literal["created", "paid", "packed", "delivered", "cancelled"]


def _blank_to_none(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


class _Stripped(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class VariantCreate(_Stripped):
    size: str = Field(min_length=1, max_length=20)
    color: str = Field(min_length=1, max_length=30)
    sku: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    stock_quantity: int = Field(default=0, ge=0, le=100000)
    price_override: PositiveMoney | None = Field(
        default=None, description="Replaces the product's base price for this variant; the member discount still applies"
    )
    is_active: bool = True

    _blank = field_validator("price_override", mode="before")(_blank_to_none)


class VariantUpdate(_Stripped):
    size: str | None = Field(default=None, min_length=1, max_length=20)
    color: str | None = Field(default=None, min_length=1, max_length=30)
    stock_quantity: int | None = Field(default=None, ge=0, le=100000)
    price_override: PositiveMoney | None = None
    clear_price_override: bool = False
    is_active: bool | None = None

    _blank = field_validator("size", "color", "stock_quantity", "price_override", mode="before")(_blank_to_none)


class ProductCreate(_Stripped):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    base_price: PositiveMoney
    member_price: PositiveMoney
    image_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    variants: list[VariantCreate] = Field(default_factory=list, max_length=50)

    _blank = field_validator("description", "image_url", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def member_price_is_a_discount(self):
        if self.member_price > self.base_price:
            raise ValueError("member price cannot be higher than the base price")
        return self


class ProductUpdate(_Stripped):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    base_price: PositiveMoney | None = None
    member_price: PositiveMoney | None = None
    image_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None

    _blank = field_validator("name", "description", "base_price", "member_price", "image_url", mode="before")(_blank_to_none)


class VariantOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    size: str
    color: str
    sku: str
    stock_quantity: int
    in_stock: bool
    price_override: Money | None
    your_price: Money = Field(description="What the caller pays for this variant")
    is_active: bool


class ProductOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    name: str
    description: str | None
    base_price: Money
    member_price: Money
    your_price: Money = Field(description="Base or member price, decided by the server from the caller's membership")
    member_pricing_applied: bool
    image_url: str | None
    is_active: bool
    variants: list[VariantOut]
    created_at: datetime
    updated_at: datetime


class OrderItemIn(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=20)


class OrderCreate(BaseModel):
    """Only which variants and how many. Prices are always worked out by the server."""

    items: list[OrderItemIn] = Field(min_length=1, max_length=20)
    event_id: uuid.UUID | None = None

    _blank = field_validator("event_id", mode="before")(_blank_to_none)


class OrderItemOut(BaseModel):
    id: uuid.UUID
    product_variant_id: uuid.UUID
    product_name: str
    size: str
    color: str
    sku: str
    quantity: int
    unit_price: Money
    line_total: Money


class OrderOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    order_number: str
    total_amount: Money
    payment_status: OrderPaymentStatus
    status: OrderStatus
    currency: str = "INR"
    stripe_checkout_session_id: str | None
    stripe_payment_intent_id: str | None
    refund_status: Literal['required', 'pending', 'failed', 'succeeded'] | None = None
    refund_failure: str | None = None
    event_id: uuid.UUID | None
    items: list[OrderItemOut]
    created_at: datetime
    updated_at: datetime


class OrderStatusUpdate(BaseModel):
    status: Literal["packed", "delivered", "cancelled"]


class InventoryManagerOut(BaseModel):
    club_id: uuid.UUID
    user_id: uuid.UUID
    assigned_by: uuid.UUID
    assigned_at: datetime
    updated_at: datetime
    user_name: str | None = None
    user_email: str | None = None


class InventoryManagerAssign(BaseModel):
    user_id: uuid.UUID

