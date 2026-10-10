"""Paying for FairSeat bookings. Separate from the merchandise payment routes.

Needs both FAIRSEAT_ENABLED=1 and FAIRSEAT_PAYMENTS_ENABLED=1. Each route commits the
database work first and only then sends any refund that has become due, because the
payment provider is never called while booking locks are held.
"""

import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.fairseat import get_purchaser, require_fairseat
from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_db, require_finance_admin
from app.core.errors import AppError, bad_request
from app.schemas.fairseat import (
    BookingOut,
    TicketCheckoutOut,
    TicketCheckoutRequest,
    TicketMockSimulate,
    TicketPaymentOut,
    TicketVerifyRequest,
    TicketWebhookAck,
)
from app.services import fairseat_payments


def require_ticket_payments() -> None:
    if not get_settings().fairseat_payments_enabled:
        raise AppError(404, "ticket_payments_disabled", "Ticket payments are not switched on")


router = APIRouter(
    prefix="/ticket-payments", tags=["FairSeat payments"],
    dependencies=[Depends(require_fairseat), Depends(require_ticket_payments)],
)


async def _settle(db: AsyncSession, user: CurrentUser, session_id: str) -> TicketPaymentOut:
    booking_id, result = await fairseat_payments.verify_payment(db, user, session_id)
    await db.commit()
    if result == "amount_mismatch":
        raise bad_request("amount_mismatch", "The amount paid does not match this booking. Contact the treasurer.")
    if result in ("duplicate_payment", "not_payable"):
        raise AppError(409, result, "This payment could not be applied to the booking. Contact the treasurer.")
    if result == "refund_required":
        await fairseat_payments.process_refund(db, booking_id)
    return await fairseat_payments.present(db, booking_id)


@router.post("/stripe/create-checkout-session", response_model=TicketCheckoutOut)
async def create_checkout_session(
    body: TicketCheckoutRequest, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> TicketCheckoutOut:
    """Open a checkout session for your own booking while its hold lasts. The server sets the amount."""
    result = await fairseat_payments.create_checkout_session(db, user, body.booking_id)
    await db.commit()
    return result


@router.post("/stripe/verify", response_model=TicketPaymentOut)
async def verify_payment(
    body: TicketVerifyRequest, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> TicketPaymentOut:
    """Ask the provider whether the session is paid, then confirm the booking or start a refund.
    Calling it again for the same session changes nothing."""
    return await _settle(db, user, body.session_id)


@router.get("/stripe/return", response_model=TicketPaymentOut)
async def return_from_checkout(
    session_id: str, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> TicketPaymentOut:
    """Where the provider sends the buyer after paying, until a ticket page exists. Same as /verify."""
    return await _settle(db, user, session_id)


@router.post("/stripe/webhook", response_model=TicketWebhookAck)
async def stripe_webhook(
    request: Request, stripe_signature: str = Header(default=""), db: AsyncSession = Depends(get_db)
) -> TicketWebhookAck:
    """Server-to-server payment and refund events, authenticated by signature."""
    result = await fairseat_payments.handle_webhook(db, await request.body(), stripe_signature)
    await db.commit()
    if result.refund_booking_id:
        await fairseat_payments.process_refund(db, result.refund_booking_id)
    return result.ack


@router.post("/mock/simulate")
async def simulate_mock_payment(
    body: TicketMockSimulate, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Mock provider only (404 when Stripe is active). Then call /stripe/verify."""
    status = await fairseat_payments.simulate_mock_payment(db, user, body.session_id, body.outcome == "success")
    await db.commit()
    return {"session_id": body.session_id, "payment_status": status}


@router.get("/refunds", response_model=list[BookingOut])
async def open_refunds(
    user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
) -> list[BookingOut]:
    """Treasurer and president: refunds that are owed, in flight or failed."""
    return await fairseat_payments.list_open_refunds(db, user)


@router.post("/refunds/{booking_id}/retry", response_model=TicketPaymentOut)
async def retry_refund(
    booking_id: uuid.UUID, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
) -> TicketPaymentOut:
    """Treasurer and president: send a failed or stuck refund again."""
    await fairseat_payments.retry_refund(db, user, booking_id)
    return await fairseat_payments.present(db, booking_id)
