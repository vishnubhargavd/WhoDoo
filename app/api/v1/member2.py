"""Batch A routes (Member 2): profile and UPI, renewal reminders, reimbursement payouts,
event registrations and budget, and the Razorpay webhook.

The person is always the signed-in caller; nothing here takes a user id for "me" from the
request. Officer routes check the caller's role from the database.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import bad_request
from app.models.member2 import ProviderWebhookReceipt
from app.schemas.member2 import (
    BudgetIn,
    BudgetOut,
    PaymentProfileIn,
    PaymentProfileOut,
    PayoutOut,
    PayoutReadiness,
    ProfileOut,
    ProfileUpdate,
    RegistrationSummary,
    ReminderOut,
)
from app.services import (
    event_insights,
    event_service,
    fairseat_payments,
    membership_service,
    modules,
    payment_service,
    payout_service,
    profile_service,
    reminder_service,
)
from app.services.payment_service import InvalidWebhookSignature
from app.services.razorpay_provider import translate_webhook, verify_signature

logger = logging.getLogger("whodoo.razorpay")

me = APIRouter(prefix="/me", tags=["Profile"])
payouts = APIRouter(tags=["Reimbursement payouts"], dependencies=[Depends(modules.require("profile"))])
events = APIRouter(prefix="/events", tags=["Event registrations and budget"])
razorpay = APIRouter(prefix="/payments/razorpay", tags=["Payments"])


# --- Profile and UPI ------------------------------------------------------------------------


@me.get("/profile", response_model=ProfileOut)
async def get_profile(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ProfileOut:
    return await profile_service.get_profile(db, user)


@me.put("/profile", response_model=ProfileOut)
async def update_profile(
    body: ProfileUpdate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ProfileOut:
    """Your own name, display name, phone, department and year. Role, email (your sign-in),
    membership status and expiry cannot be changed here; unknown fields are refused."""
    out = await profile_service.update_profile(db, user, body)
    await db.commit()
    return out


@me.put("/payment-profile", response_model=PaymentProfileOut, dependencies=[Depends(modules.require("profile"))])
async def set_payment_profile(
    body: PaymentProfileIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaymentProfileOut:
    """Add or replace your UPI id. It is stored encrypted and shown masked. Saving it never
    authorises anyone to debit you."""
    out = await profile_service.set_upi(db, user, body.upi_id)
    await db.commit()
    return out


@me.delete("/payment-profile", status_code=204, dependencies=[Depends(modules.require("profile"))])
async def remove_payment_profile(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await profile_service.remove_upi(db, user)
    await db.commit()


@me.get("/notifications", response_model=list[ReminderOut])
async def notifications(
    unread_only: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ReminderOut]:
    """Your membership renewal reminders for the current term."""
    found = await reminder_service.list_for(db, user, unread_only=unread_only)
    await db.commit()
    return found


@me.post("/notifications/{reminder_id}/read", status_code=204, dependencies=[Depends(modules.require("reminders"))])
async def mark_read(
    reminder_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    await reminder_service.mark_read(db, user, reminder_id)
    await db.commit()


# --- Payouts -------------------------------------------------------------------------------


@payouts.get("/expenses/{claim_id}/payout", response_model=PayoutReadiness)
async def payout_readiness(
    claim_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PayoutReadiness:
    """Treasurer or president: can this approved claim be paid out over UPI, and its attempts so far."""
    return await payout_service.readiness(db, user, claim_id)


@payouts.post("/expenses/{claim_id}/payout", response_model=PayoutOut)
async def start_payout(
    claim_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PayoutOut:
    """Treasurer or president, not the claimant. The claim is marked paid only if the provider
    reports the payout processed."""
    return await payout_service.pay_out(db, user, claim_id)


@payouts.post("/payouts/{payout_id}/refresh", response_model=PayoutOut)
async def refresh_payout(
    payout_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PayoutOut:
    return await payout_service.refresh(db, user, payout_id)


# --- Events --------------------------------------------------------------------------------


@events.get("/{event_id}/registrations", response_model=RegistrationSummary)
async def registrations(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> RegistrationSummary:
    """Organizers: registrations, seats left per category, and the low-ticket level."""
    event = await event_service.get_managed_event(db, user, event_id)
    return await event_insights.registrations(db, event)


@events.get("/{event_id}/budget", response_model=BudgetOut)
async def get_budget(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BudgetOut:
    return await event_insights.budget(db, user, event_id)


@events.put("/{event_id}/budget", response_model=BudgetOut)
async def set_budget(
    event_id: uuid.UUID, body: BudgetIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BudgetOut:
    """The president, the event's lead or the treasurer. Whole paise."""
    out = await event_insights.set_budget(db, user, event_id, body)
    await db.commit()
    return out


# --- Razorpay webhook ---------------------------------------------------------------------


@razorpay.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Razorpay events, authenticated by X-Razorpay-Signature. One endpoint for every module:
    each one picks up only its own payments (by the notes on the payment link) and reads the
    link back from Razorpay before recording anything. A repeated event id is turned away."""
    body = await request.body()
    try:
        verify_signature(body, x_razorpay_signature, get_settings().razorpay_webhook_secret.strip())
        event = translate_webhook(body)
    except InvalidWebhookSignature:
        raise bad_request("invalid_webhook_signature", "Webhook signature is missing or wrong") from None
    key = (x_razorpay_event_id or event.id)[:255]
    receipts = modules.migrated("profile")
    if receipts and await db.scalar(select(ProviderWebhookReceipt.event_key).where(
            ProviderWebhookReceipt.provider == "razorpay", ProviderWebhookReceipt.event_key == key)):
        return {"status": "duplicate"}

    outcome = "ignored"
    if event.type.startswith("razorpay.payout."):
        outcome = await payout_service.handle_provider_event(db, event.type.removeprefix("razorpay."), event.data)
        await db.commit()
    elif payment_service.get_provider().name == "razorpay":
        # Every module's own handler runs; each ignores payments that are not its own. They are
        # idempotent on their own keys, so a partial failure is safely retried by Razorpay.
        results = [(await payment_service.handle_webhook(db, body, x_razorpay_signature)).status]
        await db.commit()
        if modules.migrated("memberships"):
            result = await membership_service.handle_webhook(db, body, x_razorpay_signature)
            await db.commit()
            if result == "refund_due":
                await membership_service.refund_unhonoured(db, str(event.data.get("id", "")))
                await db.commit()
            results.append(result)
        settings = get_settings()
        if settings.fairseat_enabled and settings.fairseat_payments_enabled and modules.migrated("tickets"):
            ticket = await fairseat_payments.handle_webhook(db, body, x_razorpay_signature)
            await db.commit()
            if ticket.refund_booking_id:
                await fairseat_payments.process_refund(db, ticket.refund_booking_id)
            results.append(ticket.ack.status)
        outcome = "processed" if any(r in ("processed", "activated") for r in results) else "ignored"

    if receipts:
        db.add(ProviderWebhookReceipt(provider="razorpay", event_key=key, event_type=event.type, outcome=outcome))
        try:
            await db.commit()
        except IntegrityError:  # a concurrent delivery recorded it first: the work above was idempotent
            await db.rollback()
    return {"status": outcome}
