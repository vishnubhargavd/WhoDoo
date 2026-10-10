import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, created_at_col, updated_at_col, uuid_pk

ORDER_PAYMENT_STATUSES = ("created", "pending", "paid", "failed", "refunded")
ORDER_STATUSES = ("created", "paid", "packed", "delivered", "cancelled")


class MerchProduct(Base):
    __tablename__ = "merch_products"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    member_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    image_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    # Relationships are always loaded explicitly (selectinload); async code cannot lazy-load.
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", order_by="ProductVariant.sku", lazy="raise"
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("merch_products.id"))
    size: Mapped[str] = mapped_column(Text)
    color: Mapped[str] = mapped_column(Text)
    sku: Mapped[str] = mapped_column(Text, unique=True)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    price_override: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    product: Mapped[MerchProduct] = relationship(back_populates="variants", lazy="raise")


class MerchOrder(Base):
    __tablename__ = "merch_orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    club_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_number: Mapped[str] = mapped_column(Text, unique=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(Text, default="INR")
    payment_status: Mapped[str] = mapped_column(Text, default="pending")
    status: Mapped[str] = mapped_column(Text, default="created")
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(Text, unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(Text, unique=True)
    refund_status: Mapped[str | None] = mapped_column(Text)
    stripe_refund_id: Mapped[str | None] = mapped_column(Text)
    refund_reason: Mapped[str | None] = mapped_column(Text)
    refund_failure: Mapped[str | None] = mapped_column(Text)
    refund_attempts: Mapped[int] = mapped_column(Integer, default=0)
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", order_by="OrderItem.created_at", lazy="raise")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("merch_orders.id"))
    product_variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("product_variants.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = created_at_col()

    order: Mapped[MerchOrder] = relationship(back_populates="items", lazy="raise")
    variant: Mapped[ProductVariant] = relationship(lazy="raise")


class PaymentEvent(Base):
    """One webhook delivery. `provider_event_id` is unique, so a repeat is skipped."""

    __tablename__ = "payment_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(Text)
    provider_event_id: Mapped[str] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text)
    merch_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("merch_orders.id"))
    payment_intent_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = created_at_col()
