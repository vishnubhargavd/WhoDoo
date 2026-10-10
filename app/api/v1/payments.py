from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.payment import (
    CreatePaymentOrder,
    MockSimulate,
    PaymentOrderOut,
    VerifyPayment,
    VerifyPaymentOut,
    WebhookAck,
)
from app.services import merch_service, payment_service

router = APIRouter(prefix="/payments", tags=["Payments"])


@router.post("/razorpay/create-order", response_model=PaymentOrderOut)
async def create_payment_order(
    body: CreatePaymentOrder, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaymentOrderOut:
    """Open a provider order for one of your unpaid merchandise orders. The server sets the amount."""
    result = await payment_service.create_payment_order(db, user, body.merch_order_id)
    await db.commit()
    return result


@router.post("/razorpay/verify", response_model=VerifyPaymentOut)
async def verify_payment(
    body: VerifyPayment, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> VerifyPaymentOut:
    """Check the checkout signature. Only then: mark the order paid, take stock, post income to the ledger."""
    outcome = await payment_service.verify_payment(db, user, body)
    await db.commit()
    if outcome.error:
        raise outcome.error
    return VerifyPaymentOut(verified=True, order=await merch_service.get_order(db, user, outcome.order.id))


@router.post("/razorpay/webhook", response_model=WebhookAck)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> WebhookAck:
    """Server-to-server payment events, authenticated by signature rather than a user session."""
    ack = await payment_service.handle_webhook(db, await request.body(), x_razorpay_signature)
    await db.commit()
    return ack


@router.post("/mock/simulate", response_model=VerifyPayment)
async def simulate_mock_payment(
    body: MockSimulate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> VerifyPayment:
    """Mock provider only (404 with real Razorpay keys). Returns what Razorpay Checkout would hand
    the browser, ready to post to /razorpay/verify. `outcome: "failure"` returns a bad signature."""
    result = await payment_service.simulate_mock_payment(db, user, body.razorpay_order_id, body.outcome == "success")
    return VerifyPayment(**result)
