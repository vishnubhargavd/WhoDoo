import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db, require_finance_admin
from app.schemas.merch import (
    OrderCreate,
    OrderOut,
    OrderStatusUpdate,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    VariantCreate,
    VariantUpdate,
)
from app.services import merch_service, payment_service

router = APIRouter(prefix="/merch", tags=["MerchFlow"])


@router.get("/products", response_model=list[ProductOut])
async def list_products(
    include_inactive: bool = Query(default=False, description="Treasurer and president only"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProductOut]:
    return await merch_service.list_products(db, user, include_inactive)


@router.get("/products/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ProductOut:
    return await merch_service.get_product(db, user, product_id)


@router.post("/products", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductCreate, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
) -> ProductOut:
    product = await merch_service.create_product(db, user, body)
    await db.commit()
    return product


@router.put("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    product = await merch_service.update_product(db, user, product_id, body)
    await db.commit()
    return product


@router.post("/products/{product_id}/variants", response_model=ProductOut, status_code=201)
async def add_variant(
    product_id: uuid.UUID,
    body: VariantCreate,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    product = await merch_service.add_variant(db, user, product_id, body)
    await db.commit()
    return product


@router.put("/variants/{variant_id}", response_model=ProductOut)
async def update_variant(
    variant_id: uuid.UUID,
    body: VariantUpdate,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    product = await merch_service.update_variant(db, user, variant_id, body)
    await db.commit()
    return product


@router.post("/orders", response_model=OrderOut, status_code=201)
async def create_order(
    body: OrderCreate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> OrderOut:
    """Create an unpaid order. Pay for it through /api/v1/payments/razorpay/create-order."""
    order = await merch_service.create_order(db, user, body)
    await db.commit()
    return await merch_service.get_order(db, user, order.id)


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    everyone: bool = Query(default=False, alias="all", description="Every order in the club (treasurer and president)"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[OrderOut]:
    return await merch_service.list_orders(db, user, everyone=everyone)


@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> OrderOut:
    return await merch_service.get_order(db, user, order_id)


@router.post("/orders/{order_id}/status", response_model=OrderOut)
async def update_order_status(
    order_id: uuid.UUID,
    body: OrderStatusUpdate,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    """Mark an order packed or delivered, or cancel it. Cancelling a paid order restocks and refunds it."""
    order, refund_due = await merch_service.update_status(db, user, order_id, body.status)
    if refund_due:
        # Refund at the provider before committing: if it fails, the cancellation is rolled back.
        await run_in_threadpool(
            payment_service.get_provider().refund, order.razorpay_payment_id, int(order.total_amount * 100)
        )
    await db.commit()
    return await merch_service.get_order(db, user, order_id)
