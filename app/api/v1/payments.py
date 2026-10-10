from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.payment import (
    CheckoutSessionOut,
    CreateCheckoutSession,
    MockSimulate,
    MockSimulateOut,
    VerifyPayment,
    VerifyPaymentOut,
    WebhookAck,
)
from app.services import merch_service, payment_service

router = APIRouter(prefix="/payments", tags=["Payments"])


@router.post("/stripe/create-checkout-session", response_model=CheckoutSessionOut)
async def create_checkout_session(
    body: CreateCheckoutSession, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> CheckoutSessionOut:
    """Open a checkout session for one of your unpaid merchandise orders. The server sets the amount."""
    result = await payment_service.create_checkout_session(db, user, body.merch_order_id)
    await db.commit()
    return result


@router.post("/stripe/verify", response_model=VerifyPaymentOut)
async def verify_payment(
    body: VerifyPayment, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> VerifyPaymentOut:
    """Ask the provider whether the session is paid. Only then: mark the order paid, take stock,
    post income to the ledger. Calling it again for the same session changes nothing."""
    outcome = await payment_service.verify_payment(db, user, body.session_id)
    await db.commit()
    if outcome.error:
        raise outcome.error
    return VerifyPaymentOut(verified=True, order=await merch_service.get_order(db, user, outcome.order.id))


@router.post("/stripe/webhook", response_model=WebhookAck)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> WebhookAck:
    """Server-to-server payment events, authenticated by signature rather than a user session."""
    ack = await payment_service.handle_webhook(db, await request.body(), stripe_signature)
    await db.commit()
    return ack


@router.post("/mock/simulate", response_model=MockSimulateOut)
async def simulate_mock_payment(
    body: MockSimulate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MockSimulateOut:
    """Mock provider only (404 when Stripe is active). Plays the buyer on the checkout page:
    `success` pays the session, `failure` declines the card. Then call /stripe/verify."""
    result = await payment_service.simulate_mock_payment(db, user, body.session_id, body.outcome == "success")
    await db.commit()
    return result
