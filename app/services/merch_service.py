"""MerchFlow: catalogue, orders and stock.

Stock rules:
* Creating an order checks stock but does not reserve it.
* Stock is reduced only in `fulfil_paid_order`, after the payment is verified, by a
  conditional UPDATE (`... WHERE stock_quantity >= quantity`) inside the same
  transaction that marks the order paid and writes the ledger entry. Two buyers
  racing for the last item cannot both succeed.
* Cancelling a paid order puts the stock back and writes a refund entry.
* A payment for an order that can no longer be filled is refunded; no stock moves.
"""

import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.dependencies import FINANCE_ADMINS, CurrentUser
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.models.merch import MerchOrder, MerchProduct, OrderItem, ProductVariant
from app.schemas.merch import (
    OrderCreate,
    OrderItemOut,
    OrderOut,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
)
from app.services import audit_service, ledger_service, shared_directory

SALES_CATEGORY = "merchandise_sales"

WITH_VARIANTS = selectinload(MerchProduct.variants)
WITH_ITEMS = selectinload(MerchOrder.items).selectinload(OrderItem.variant).selectinload(ProductVariant.product)


class OutOfStock(Exception):
    def __init__(self, sku: str):
        super().__init__(sku)
        self.sku = sku


# --- Pricing -----------------------------------------------------------------------


def unit_price(product: MerchProduct, variant: ProductVariant, is_member: bool) -> Decimal:
    """The price this buyer pays. A variant override replaces the base price; members keep their discount."""
    list_price = variant.price_override if variant.price_override is not None else product.base_price
    if not is_member:
        return list_price
    discount = product.base_price - product.member_price
    return max(list_price - discount, Decimal("0.01"))


def present_product(product: MerchProduct, user: CurrentUser, include_inactive: bool = False) -> ProductOut:
    member = user.is_active_member
    variants = [
        VariantOut(
            id=variant.id,
            product_id=variant.product_id,
            size=variant.size,
            color=variant.color,
            sku=variant.sku,
            stock_quantity=variant.stock_quantity,
            in_stock=variant.stock_quantity > 0,
            price_override=variant.price_override,
            your_price=unit_price(product, variant, member),
            is_active=variant.is_active,
        )
        for variant in product.variants
        if include_inactive or variant.is_active
    ]
    return ProductOut(
        id=product.id,
        club_id=product.club_id,
        name=product.name,
        description=product.description,
        base_price=product.base_price,
        member_price=product.member_price,
        your_price=product.member_price if member else product.base_price,
        member_pricing_applied=member,
        image_url=product.image_url,
        is_active=product.is_active,
        variants=variants,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


# --- Catalogue ---------------------------------------------------------------------


async def _load_product(session: AsyncSession, club_id: uuid.UUID, product_id: uuid.UUID) -> MerchProduct:
    product = await session.scalar(
        select(MerchProduct)
        .where(MerchProduct.id == product_id, MerchProduct.club_id == club_id)
        .options(WITH_VARIANTS)
        .execution_options(populate_existing=True)
    )
    if product is None:
        raise not_found("Product")
    return product


async def list_products(session: AsyncSession, user: CurrentUser, include_inactive: bool = False) -> list[ProductOut]:
    if include_inactive and user.role not in FINANCE_ADMINS:
        raise forbidden("Only the treasurer or president can see inactive products")
    stmt = select(MerchProduct).where(MerchProduct.club_id == user.club_id).options(WITH_VARIANTS)
    if not include_inactive:
        stmt = stmt.where(MerchProduct.is_active.is_(True))
    products = await session.scalars(stmt.order_by(MerchProduct.name))
    return [present_product(product, user, include_inactive) for product in products]


async def get_product(session: AsyncSession, user: CurrentUser, product_id: uuid.UUID) -> ProductOut:
    product = await _load_product(session, user.club_id, product_id)
    is_admin = user.role in FINANCE_ADMINS
    if not product.is_active and not is_admin:
        raise not_found("Product")
    return present_product(product, user, include_inactive=is_admin)


def _new_variant(product_id: uuid.UUID, data: VariantCreate) -> ProductVariant:
    return ProductVariant(
        id=uuid.uuid4(),
        product_id=product_id,
        size=data.size,
        color=data.color,
        sku=data.sku.upper(),
        stock_quantity=data.stock_quantity,
        price_override=data.price_override,
        is_active=data.is_active,
    )


async def _flush_catalogue(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        if "sku" in str(exc.orig):
            raise conflict("duplicate_sku", "Another variant already uses that SKU") from exc
        raise


async def create_product(session: AsyncSession, user: CurrentUser, data: ProductCreate) -> ProductOut:
    product = MerchProduct(
        id=uuid.uuid4(),
        club_id=user.club_id,
        name=data.name,
        description=data.description,
        base_price=data.base_price,
        member_price=data.member_price,
        image_url=data.image_url,
        is_active=data.is_active,
        created_by=user.id,
    )
    session.add(product)
    session.add_all(_new_variant(product.id, variant) for variant in data.variants)
    await _flush_catalogue(session)
    return present_product(await _load_product(session, user.club_id, product.id), user, include_inactive=True)


async def update_product(
    session: AsyncSession, user: CurrentUser, product_id: uuid.UUID, data: ProductUpdate
) -> ProductOut:
    product = await _load_product(session, user.club_id, product_id)
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    base = changes.get("base_price", product.base_price)
    member = changes.get("member_price", product.member_price)
    if member > base:
        raise bad_request("invalid_price", "Member price cannot be higher than the base price")
    for name, value in changes.items():
        setattr(product, name, value)
    await session.flush()
    return present_product(await _load_product(session, user.club_id, product.id), user, include_inactive=True)


async def add_variant(
    session: AsyncSession, user: CurrentUser, product_id: uuid.UUID, data: VariantCreate
) -> ProductOut:
    product = await _load_product(session, user.club_id, product_id)
    variant = _new_variant(product.id, data)
    session.add(variant)
    await _flush_catalogue(session)
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="merch.variant_created",
        entity_type="product_variant", entity_id=variant.id,
        details={"sku": variant.sku, "stock_quantity": variant.stock_quantity},
    )
    return present_product(await _load_product(session, user.club_id, product.id), user, include_inactive=True)


async def update_variant(
    session: AsyncSession, user: CurrentUser, variant_id: uuid.UUID, data: VariantUpdate
) -> ProductOut:
    variant = await session.scalar(
        select(ProductVariant)
        .join(MerchProduct, MerchProduct.id == ProductVariant.product_id)
        .where(ProductVariant.id == variant_id, MerchProduct.club_id == user.club_id)
        .with_for_update(of=ProductVariant)
    )
    if variant is None:
        raise not_found("Variant")

    changes = data.model_dump(exclude_unset=True, exclude_none=True, exclude={"clear_price_override"})
    old_stock = variant.stock_quantity
    for name, value in changes.items():
        setattr(variant, name, value)
    if data.clear_price_override:
        variant.price_override = None
    await session.flush()
    if variant.stock_quantity != old_stock:
        audit_service.record(
            session, club_id=user.club_id, actor_id=user.id, action="merch.stock_set",
            entity_type="product_variant", entity_id=variant.id,
            details={"sku": variant.sku, "from": old_stock, "to": variant.stock_quantity},
        )
    return present_product(await _load_product(session, user.club_id, variant.product_id), user, include_inactive=True)


# --- Orders ------------------------------------------------------------------------


def _order_number() -> str:
    return f"WD-{datetime.now(timezone.utc):%y%m%d}-{secrets.token_hex(3).upper()}"


async def present_orders(session: AsyncSession, orders: list[MerchOrder]) -> list[OrderOut]:
    people = await shared_directory.get_users(session, [order.user_id for order in orders])
    return [
        OrderOut(
            id=order.id,
            club_id=order.club_id,
            user_id=order.user_id,
            user_name=people[order.user_id].full_name if order.user_id in people else None,
            order_number=order.order_number,
            total_amount=order.total_amount,
            payment_status=order.payment_status,
            status=order.status,
            currency=order.currency,
            stripe_checkout_session_id=order.stripe_checkout_session_id,
            stripe_payment_intent_id=order.stripe_payment_intent_id,
            refund_status=order.refund_status,
            refund_failure=order.refund_failure,
            event_id=order.event_id,
            items=[
                OrderItemOut(
                    id=item.id,
                    product_variant_id=item.product_variant_id,
                    product_name=item.variant.product.name,
                    size=item.variant.size,
                    color=item.variant.color,
                    sku=item.variant.sku,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    line_total=item.line_total,
                )
                for item in order.items
            ],
            created_at=order.created_at,
            updated_at=order.updated_at,
        )
        for order in orders
    ]


async def load_order(
    session: AsyncSession, club_id: uuid.UUID, order_id: uuid.UUID, *, lock: bool = False
) -> MerchOrder:
    stmt = (
        select(MerchOrder)
        .where(MerchOrder.id == order_id, MerchOrder.club_id == club_id)
        .options(WITH_ITEMS)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update(of=MerchOrder)
    order = await session.scalar(stmt)
    if order is None:
        raise not_found("Order")
    return order


async def check_stock(session: AsyncSession, order: MerchOrder) -> None:
    """Fail early, before taking payment, if the order can no longer be filled."""
    for item in order.items:
        variant = item.variant
        if not variant.is_active or not variant.product.is_active:
            raise conflict("unavailable", f"{variant.product.name} ({variant.sku}) is no longer on sale")
        if variant.stock_quantity < item.quantity:
            raise conflict(
                "insufficient_stock",
                f"Only {variant.stock_quantity} left of {variant.product.name} ({variant.size}, {variant.color})",
                {"sku": variant.sku, "available": variant.stock_quantity, "requested": item.quantity},
            )


async def create_order(session: AsyncSession, user: CurrentUser, data: OrderCreate) -> MerchOrder:
    """Create an unpaid order. The price comes from the catalogue and the buyer's membership, never the request."""
    quantities: dict[uuid.UUID, int] = {}
    for line in data.items:
        quantities[line.variant_id] = quantities.get(line.variant_id, 0) + line.quantity

    if data.event_id and await shared_directory.get_event(session, user.club_id, data.event_id) is None:
        raise not_found("Event")

    rows = await session.execute(
        select(ProductVariant, MerchProduct)
        .join(MerchProduct, MerchProduct.id == ProductVariant.product_id)
        .where(ProductVariant.id.in_(quantities), MerchProduct.club_id == user.club_id)
    )
    found = {variant.id: (variant, product) for variant, product in rows}
    if len(found) != len(quantities):
        raise not_found("Product variant")

    order = MerchOrder(
        id=uuid.uuid4(), club_id=user.club_id, user_id=user.id, order_number=_order_number(),
        total_amount=Decimal("0"), currency=get_settings().currency, payment_status="pending", status="created",
        event_id=data.event_id,
    )
    items = []
    for variant_id, quantity in quantities.items():
        variant, product = found[variant_id]
        if not (variant.is_active and product.is_active):
            raise conflict("unavailable", f"{product.name} ({variant.sku}) is not on sale")
        if variant.stock_quantity < quantity:
            raise conflict(
                "insufficient_stock",
                f"Only {variant.stock_quantity} left of {product.name} ({variant.size}, {variant.color})",
                {"sku": variant.sku, "available": variant.stock_quantity, "requested": quantity},
            )
        price = unit_price(product, variant, user.is_active_member)
        items.append(
            OrderItem(order_id=order.id, product_variant_id=variant.id, quantity=quantity,
                      unit_price=price, line_total=price * quantity)
        )
        order.total_amount += price * quantity

    session.add(order)
    await session.flush()
    session.add_all(items)
    await session.flush()
    return await load_order(session, user.club_id, order.id)


async def list_orders(session: AsyncSession, user: CurrentUser, *, everyone: bool = False) -> list[OrderOut]:
    if everyone and user.role not in FINANCE_ADMINS:
        raise forbidden("Only the treasurer or president can see every order")
    stmt = select(MerchOrder).where(MerchOrder.club_id == user.club_id).options(WITH_ITEMS)
    if not everyone:
        stmt = stmt.where(MerchOrder.user_id == user.id)
    orders = list(await session.scalars(stmt.order_by(MerchOrder.created_at.desc())))
    return await present_orders(session, orders)


async def get_order(session: AsyncSession, user: CurrentUser, order_id: uuid.UUID) -> OrderOut:
    order = await load_order(session, user.club_id, order_id)
    if order.user_id != user.id and user.role not in FINANCE_ADMINS:
        raise not_found("Order")
    return (await present_orders(session, [order]))[0]


# --- Payment outcomes (called by payment_service) --------------------------------------


def _summary(order: MerchOrder) -> str:
    return ", ".join(f"{item.quantity} x {item.variant.product.name}" for item in order.items)


async def _record_sale(session: AsyncSession, order: MerchOrder, payment_intent_id: str, actor_id: uuid.UUID) -> None:
    """One income entry per payment: the key makes a second call for the same payment a no-op."""
    await ledger_service.record_income(
        session,
        club_id=order.club_id,
        source_module="merchandise",
        amount=order.total_amount,
        created_by=actor_id,
        reference_id=order.id,
        reference_type="merch_order",
        payer_id=order.user_id,
        event_id=order.event_id,
        category=SALES_CATEGORY,
        description=f"Merchandise purchase: {_summary(order)} (order {order.order_number})",
        payment_status="paid",
        idempotency_key=f"{payment_intent_id}:income",
    )


async def _record_refund(
    session: AsyncSession, order: MerchOrder, actor_id: uuid.UUID, description: str, *, approved_by: uuid.UUID | None = None
) -> None:
    await ledger_service.record_refund(
        session,
        club_id=order.club_id,
        source_module="merchandise",
        amount=order.total_amount,
        created_by=actor_id,
        approved_by=approved_by,
        reference_id=order.id,
        reference_type="merch_order",
        payee_id=order.user_id,
        event_id=order.event_id,
        category=SALES_CATEGORY,
        description=description,
        idempotency_key=f"{order.stripe_payment_intent_id}:refund",
    )


async def _restock(session: AsyncSession, order: MerchOrder) -> None:
    for item in order.items:
        await session.execute(
            update(ProductVariant)
            .where(ProductVariant.id == item.product_variant_id)
            .values(stock_quantity=ProductVariant.stock_quantity + item.quantity)
            .execution_options(synchronize_session=False)
        )


async def fulfil_paid_order(
    session: AsyncSession, order: MerchOrder, payment_intent_id: str, *, actor_id: uuid.UUID
) -> MerchOrder:
    """Mark a locked order paid, take the stock and record the income, all or nothing.

    Safe to call again for the same payment (webhook retries): it returns the order unchanged.
    Raises OutOfStock, having changed nothing, if any line can no longer be filled.
    """
    if order.payment_status == "paid":
        return order
    if order.status != "created":
        raise conflict("order_closed", f"This order is already {order.status}")

    async with session.begin_nested():
        # Fixed order of updates, so two multi-item orders cannot deadlock each other.
        for item in sorted(order.items, key=lambda line: str(line.product_variant_id)):
            taken = await session.execute(
                update(ProductVariant)
                .where(ProductVariant.id == item.product_variant_id, ProductVariant.stock_quantity >= item.quantity)
                .values(stock_quantity=ProductVariant.stock_quantity - item.quantity)
                .execution_options(synchronize_session=False)
            )
            if taken.rowcount != 1:
                raise OutOfStock(item.variant.sku)

    order.payment_status = "paid"
    order.status = "paid"
    order.stripe_payment_intent_id = payment_intent_id
    await session.flush()

    await _record_sale(session, order, payment_intent_id, actor_id)
    audit_service.record(
        session, club_id=order.club_id, actor_id=actor_id, action="merch.order_paid",
        entity_type="merch_order", entity_id=order.id,
        details={"payment_intent_id": payment_intent_id, "amount": str(order.total_amount)},
    )
    return order


async def refund_unfilled_order(
    session: AsyncSession, order: MerchOrder, payment_intent_id: str, *, actor_id: uuid.UUID, reason: str
) -> MerchOrder:
    """The buyer paid but the order cannot be filled (sold out, or closed meanwhile).

    No stock was taken. Record the income and refund obligation now; only confirmed
    provider settlement creates the compensating ledger entry.
    """
    order.stripe_payment_intent_id = payment_intent_id
    order.payment_status = "paid"
    order.status = "cancelled"
    order.refund_status = "required"
    order.refund_reason = reason
    await session.flush()
    await _record_sale(session, order, payment_intent_id, actor_id)
    audit_service.record(
        session, club_id=order.club_id, actor_id=actor_id, action="merch.refund_required_unfilled",
        entity_type="merch_order", entity_id=order.id,
        details={"payment_intent_id": payment_intent_id, "reason": reason},
    )
    return order


async def record_external_refund(session: AsyncSession, order: MerchOrder) -> MerchOrder:
    """A paid order was refunded in full outside the app (Stripe Dashboard). Match it here."""
    if order.payment_status != "paid":
        return order
    if order.status not in ("delivered", "cancelled"):
        await _restock(session, order)
        order.status = "cancelled"
    order.payment_status = "refunded"
    order.refund_status = "succeeded"
    order.refund_failure = None
    await session.flush()
    await _record_refund(session, order, order.user_id, f"Refund issued by the payment provider for order {order.order_number}")
    audit_service.record(
        session, club_id=order.club_id, actor_id=None, action="merch.order_refunded_by_provider",
        entity_type="merch_order", entity_id=order.id, details={"payment_intent_id": order.stripe_payment_intent_id},
    )
    return order


def mark_payment_failed(session: AsyncSession, order: MerchOrder, reason: str, *, actor_id: uuid.UUID | None) -> None:
    """Note a declined payment. The order stays open, because the buyer can try again with
    another card. Stock was never taken, so there is nothing to put back."""
    if order.payment_status in ("paid", "refunded") or order.status != "created":
        return
    order.payment_status = "failed"
    audit_service.record(
        session, club_id=order.club_id, actor_id=actor_id, action="merch.payment_failed",
        entity_type="merch_order", entity_id=order.id, details={"reason": reason},
    )


ADMIN_MOVES = {
    "created": {"cancelled"},
    "paid": {"packed", "cancelled"},
    "packed": {"delivered", "cancelled"},
}


async def update_status(
    session: AsyncSession, user: CurrentUser, order_id: uuid.UUID, new_status: str
) -> tuple[MerchOrder, bool]:
    """Move an order along (packed, delivered) or cancel it.

    Returns (order, refund_due). Cancellation restocks once and persists the refund
    obligation. The caller commits before asking the provider to refund.
    """
    order = await load_order(session, user.club_id, order_id, lock=True)
    if new_status not in ADMIN_MOVES.get(order.status, set()):
        raise conflict("invalid_status_change", f"An order that is {order.status} cannot be marked {new_status}")

    old_status = order.status
    refund_due = False
    if new_status == "cancelled":
        if order.payment_status == "paid":
            await _restock(session, order)
            order.refund_status = "required"
            order.refund_reason = "cancelled_by_finance_admin"
            refund_due = True
        else:
            order.payment_status = "failed"
    order.status = new_status
    await session.flush()
    audit_service.record(
        session, club_id=order.club_id, actor_id=user.id, action="merch.order_status_changed",
        entity_type="merch_order", entity_id=order.id,
        details={"from": old_status, "to": new_status, "refund_required": refund_due},
    )
    return order, refund_due

