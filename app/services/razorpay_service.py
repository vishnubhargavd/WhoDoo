"""Razorpay service: Test-mode orders, HMAC signature verification, TrustLedger updates, and UPI reimbursements."""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.models.expense import ExpenseClaim
from app.models.fairseat import TicketBooking, EventTicket
from app.models.payment_profile import RazorpayOrder, ReimbursementPayout, UserPaymentProfile
from app.models.shared import User
from app.schemas.upi import RazorpayOrderCreate, RazorpayOrderOut, RazorpayPaymentVerify
from app.services import ledger_service, upi_service


def compute_razorpay_signature(order_id: str, payment_id: str, secret: str) -> str:
    """Generate expected HMAC-SHA256 signature for Razorpay verification."""
    message = f"{order_id}|{payment_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def create_order(
    db: AsyncSession,
    club_id: uuid.UUID,
    user_id: uuid.UUID,
    req: RazorpayOrderCreate,
) -> RazorpayOrderOut:
    """Create a test-mode Razorpay order for user-authorized checkout."""
    settings = get_settings()
    rzp_order_id = f"order_test_{secrets.token_hex(8)}"

    order = RazorpayOrder(
        id=uuid.uuid4(),
        club_id=club_id,
        user_id=user_id,
        reference_type=req.reference_type,
        reference_id=req.reference_id,
        rzp_order_id=rzp_order_id,
        amount_paise=req.amount_paise,
        currency=req.currency,
        status="created",
        created_at=datetime.now(timezone.utc),
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    key_id = settings.razorpay_key_id or "rzp_test_placeholder_key"

    return RazorpayOrderOut(
        id=order.id,
        club_id=order.club_id,
        rzp_order_id=order.rzp_order_id,
        amount_paise=order.amount_paise,
        currency=order.currency,
        key_id=key_id,
        status=order.status,
        reference_type=order.reference_type,
        reference_id=order.reference_id,
    )


async def verify_payment(
    db: AsyncSession,
    club_id: uuid.UUID,
    user_id: uuid.UUID,
    req: RazorpayPaymentVerify,
) -> RazorpayOrder:
    """Verify Razorpay payment via mandatory HMAC-SHA256 signature verification."""
    settings = get_settings()
    secret = settings.razorpay_key_secret or "rzp_test_secret"

    # Mandatory cryptographic check
    expected = compute_razorpay_signature(req.rzp_order_id, req.rzp_payment_id, secret)
    if not hmac.compare_digest(expected, req.rzp_signature):
        raise bad_request("invalid_signature", "Payment signature verification failed. Untrusted callback.")

    order = await db.scalar(
        select(RazorpayOrder).where(
            RazorpayOrder.rzp_order_id == req.rzp_order_id,
            RazorpayOrder.club_id == club_id,
        )
    )
    if not order:
        raise not_found("Razorpay Order")

    # Idempotent settlement: if already paid, return without duplicating ledger entries
    if order.status == "paid":
        return order

    order.status = "paid"
    order.rzp_payment_id = req.rzp_payment_id
    order.rzp_signature = req.rzp_signature
    order.paid_at = datetime.now(timezone.utc)

    # Post-payment settlement
    if order.reference_type == "ticket_booking":
        booking = await db.get(TicketBooking, order.reference_id)
        if booking:
            booking.status = "confirmed"
            booking.payment_status = "paid"
            booking.updated_at = datetime.now(timezone.utc)
            # Activate tickets
            tickets = (
                await db.scalars(
                    select(EventTicket).where(EventTicket.booking_id == booking.id)
                )
            ).all()
            for t in tickets:
                t.status = "issued"
                t.issued_at = datetime.now(timezone.utc)

            # Record in TrustLedger
            try:
                from app.schemas.ledger import LedgerEntryCreate
                await ledger_service.record_entry(
                    db,
                    club_id,
                    LedgerEntryCreate(
                        amount=booking.total_price_paise / 100.0,
                        entry_type="income",
                        category="ticket_sales",
                        description=f"FairSeat Ticket Booking #{str(booking.id)[:8]} (Razorpay: {req.rzp_payment_id})",
                        event_id=booking.event_id,
                    ),
                    created_by=user_id,
                )
            except Exception:
                pass

    await db.commit()
    await db.refresh(order)
    return order


async def initiate_reimbursement_payout(
    db: AsyncSession,
    club_id: uuid.UUID,
    officer: User,
    claim_id: uuid.UUID,
    mode: str = "upi",
    notes: str | None = None,
) -> ReimbursementPayout:
    """Initiate reimbursement payout for an approved expense claim."""
    claim = await db.get(ExpenseClaim, claim_id)
    if not claim or claim.club_id != club_id:
        raise not_found("Expense Claim")

    if claim.status != "approved":
        raise bad_request(
            "claim_not_approved",
            f"Cannot initiate payout for claim with status '{claim.status}'. Must be approved first.",
        )

    # Fetch claimant's payment profile
    profile = await db.get(UserPaymentProfile, claim.submitted_by)
    dest_upi = profile.upi_id if profile else None

    if mode == "upi" and not dest_upi:
        raise bad_request(
            "missing_upi_profile",
            "Claimant does not have a registered UPI ID profile. Ask them to add their VPA handle first.",
        )

    settings = get_settings()
    has_razorpayx = bool(settings.razorpayx_account_number and settings.use_razorpay)

    provider = "razorpayx" if has_razorpayx else "manual"
    provider_payout_id = f"pout_{secrets.token_hex(8)}" if has_razorpayx else None

    # An approved claim is NOT automatically marked paid without verified provider settlement
    status = "processing" if has_razorpayx else "pending"

    payout_notes = notes or (
        "RazorpayX test payout initiated" if has_razorpayx else "Pending manual UPI/bank disbursement"
    )

    amount_paise = int(claim.amount * 100)

    payout = ReimbursementPayout(
        id=uuid.uuid4(),
        claim_id=claim_id,
        club_id=club_id,
        claimant_id=claim.submitted_by,
        amount_paise=amount_paise,
        payout_mode=mode,
        destination_upi=dest_upi,
        provider=provider,
        provider_payout_id=provider_payout_id,
        status=status,
        initiated_by=officer.id,
        initiated_at=datetime.now(timezone.utc),
        notes=payout_notes,
    )
    db.add(payout)
    await db.commit()
    await db.refresh(payout)
    return payout
