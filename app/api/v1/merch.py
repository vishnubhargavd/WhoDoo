import uuid

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db, require_finance_admin
from app.core.errors import AppError
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
from app.services import merch_service, payment_service, storage_service

router = APIRouter(prefix="/merch", tags=["MerchFlow"])


@router.get("/images/{club_id}/{filename}")
async def get_product_image(club_id: uuid.UUID, filename: str):
    data = await run_in_threadpool(storage_service.read_product_image, club_id, filename)
    ext = filename.rsplit(".", 1)[-1].lower()
    media_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"
    return Response(data, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})



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


@router.post("/products/{product_id}/image", response_model=ProductOut)
async def upload_product_image(
    product_id: uuid.UUID,
    file: UploadFile = File(..., description="Product image as PNG, JPEG or WebP, up to 5 MB"),
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> ProductOut:
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise AppError(413, "file_too_large", "Product image must be 5 MB or smaller")
    extension, content_type = storage_service.identify_receipt(data)
    if extension == ".pdf":
        raise AppError(415, "unsupported_file", "Product image must be PNG, JPEG, or WebP")
    url = await run_in_threadpool(storage_service.save_product_image, user.club_id, data, extension, content_type)
    product = await merch_service.update_product(db, user, product_id, ProductUpdate(image_url=url))
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
    """Create an unpaid order. Pay for it through /api/v1/payments/stripe/create-checkout-session."""
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
    await db.commit()
    if refund_due:
        await payment_service.process_refund(db, order_id)
    return await merch_service.get_order(db, user, order_id)


@router.post("/orders/{order_id}/refund/retry", response_model=OrderOut)
async def retry_order_refund(
    order_id: uuid.UUID,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    """Retry an owed refund for this club, reusing the key when the outcome is uncertain."""
    from app.core.errors import conflict
    from app.services import audit_service

    order = await merch_service.load_order(db, user.club_id, order_id, lock=True)
    if order.refund_status not in ("required", "pending", "failed"):
        raise conflict("no_refund_due", "This order has no outstanding refund.")
    audit_service.record(
        db, club_id=user.club_id, actor_id=user.id, action="merch.refund_retry",
        entity_type="merch_order", entity_id=order_id, details={"from": order.refund_status},
    )
    await db.commit()
    await payment_service.process_refund(db, order_id)
    return await merch_service.get_order(db, user, order_id)
