"""Paying for FairSeat bookings, and giving the money back.

Uses the same payment provider as merchandise (Stripe test mode, or the mock), through
its own routes and its own records. Nothing here reads or writes a merchandise order.

What is trusted. The amount, currency and owner come from the booking row. Whether a
payment happened comes from the provider: a session we fetch ourselves, or a webhook
whose signature has been checked. Nothing from the browser decides either.

Locks and the provider. Booking state changes under the event, ticket settings, category
and booking locks, in that order (ticket_locks), like everything else in FairSeat. The provider is never called
while those locks are held: every function below either talks to the provider or holds
locks, not both. Refunds are therefore three steps: claim (locks, commit), call the
provider (no locks), record the answer (locks, commit).

A payment that arrives late. A hold lasts 10 minutes; a checkout page can stay open
longer. If the money arrives after the hold ran out, the booking is confirmed only if
its seats are still free in its category and within the event's total, and the event is
still selling. Otherwise the payment is recorded, the booking stays without seats, and a
full refund is owed. Seats that went to someone else are never taken back.

Refunds are honest. A booking is `refunded` only once the provider says the refund
succeeded. Until then it is `paid` with `refund_status` required, pending or failed.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, bad_request, conflict, not_found
from app.models.fairseat import TicketBooking, TicketCategory, TicketPaymentEvent
from app.models.shared import Event
from app.models.tickets import EventTicketSettings
from app.schemas.fairseat import BookingOut, TicketCheckoutOut, TicketPaymentOut, TicketWebhookAck
from app.services import audit_service, fairseat_service, ledger_service, payment_service, ticket_locks
from app.services.payment_service import CheckoutLine, CheckoutRequest, InvalidWebhookSignature, RefundResult

B = TicketBooking
LEDGER_CATEGORY = "ticket_sales"
STRIPE_MIN_SESSION_SECONDS = 30 * 60  # Stripe will not make a checkout session shorter than this

MESSAGES = {
    "confirmed": "Payment received. Your booking is confirmed.",
    "refund_required": "Your payment arrived but the booking could not be completed. A full refund is on its way.",
    "refund_pending": "Your refund has been sent to the payment provider and is being processed.",
    "refunded": "Your payment has been refunded in full.",
    "refund_failed": "Your refund could not be completed yet. The treasurer has been notified.",
}


def rupees(paise: int) -> Decimal:
    """Whole paise to rupees, exactly. Never through a float."""
    return Decimal(paise) / Decimal(100)


def _outcome(booking: TicketBooking) -> str:
    if booking.refund_status is None:
        return "confirmed"
    return {"required": "refund_required", "pending": "refund_pending",
            "succeeded": "refunded", "failed": "refund_failed"}[booking.refund_status]


async def present(session: AsyncSession, booking_id: uuid.UUID, now: datetime | None = None) -> TicketPaymentOut:
    booking = await session.scalar(select(B).where(B.id == booking_id).execution_options(populate_existing=True))
    shown: BookingOut = (await fairseat_service._present(session, [booking], now or fairseat_service.utcnow()))[0]
    outcome = _outcome(booking)
    return TicketPaymentOut(outcome=outcome, message=MESSAGES[outcome], booking=shown)


@dataclass
class Locked:
    event: Event
    settings: EventTicketSettings | None
    category: TicketCategory
    booking: TicketBooking

    def __iter__(self):
        # (event, category, booking), for the many callers that do not need the settings.
        return iter((self.event, self.category, self.booking))


async def _locked(session: AsyncSession, booking_id: uuid.UUID) -> Locked | None:
    """The booking with its event, ticket settings and category, all locked in the agreed order:
    event, settings, category, booking."""
    found = (await session.execute(select(B.event_id, B.ticket_category_id).where(B.id == booking_id))).first()
    if found is None:
        return None
    event, settings, categories = await ticket_locks.lock_for_event(
        session, found.event_id, [found.ticket_category_id])
    booking = await ticket_locks.lock_booking(session, booking_id)
    return Locked(event, settings, categories[found.ticket_category_id], booking)


def _booking_id_from(metadata: dict[str, Any] | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str((metadata or {}).get("ticket_booking_id")))
    except ValueError:
        return None


# --- Opening a checkout session --------------------------------------------------------


async def create_checkout_session(
    session: AsyncSession, user: CurrentUser, booking_id: uuid.UUID, now: datetime | None = None
) -> TicketCheckoutOut:
    """Open (or reopen) a checkout session for the caller's own booking while its hold lasts."""
    now = now or fairseat_service.utcnow()
    settings = get_settings()

    def payable(booking: TicketBooking | None) -> TicketBooking:
        if booking is None or booking.purchaser_user_id != user.id:
            raise not_found("Booking")
        if booking.payment_status in ("paid", "refunded"):
            raise conflict("already_paid", "This booking has already been paid for.")
        if booking.status != "pending_payment":
            raise conflict("booking_not_payable", f"This booking is {booking.status} and cannot be paid for.")
        if booking.hold_expires_at <= now:
            raise conflict("hold_expired", "The hold on these seats has run out. Please book again.")
        return booking

    # Plain reads first: no locks are held while the provider is called.
    booking = payable(await session.get(TicketBooking, booking_id))
    event = await session.get(Event, booking.event_id)
    category = await session.get(TicketCategory, booking.ticket_category_id)
    if event.status != "published":
        raise conflict("event_not_bookable", f"This event is {event.status} and is not taking payments.")

    provider = payment_service.get_provider()
    current = None
    if booking.stripe_checkout_session_id:
        current = await run_in_threadpool(provider.verify_payment, booking.stripe_checkout_session_id)
    if current is not None and current.payment_status == "paid":
        raise conflict("payment_received", "A payment for this booking has been received and is being confirmed.")
    if current is None or current.status != "open":
        base = settings.app_base_url.rstrip("/")
        back = settings.fairseat_return_url or f"{base}/api/v1/ticket-payments/stripe/return"
        request = CheckoutRequest(
            amount_paise=booking.total_price_paise,
            currency=booking.currency.lower(),
            lines=[CheckoutLine(name=f"{event.name}: {category.name}", unit_paise=booking.unit_price_paise,
                                quantity=booking.quantity)],
            # ticket_booking_id is the link back to the booking. The rest is for reading in the Stripe Dashboard.
            metadata={"ticket_booking_id": str(booking.id), "event_id": str(event.id),
                      "ticket_category_id": str(category.id), "user_id": str(user.id),
                      "club_id": str(event.club_id), "quantity": str(booking.quantity)},
            success_url=f"{back}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{back}?cancelled=1&booking_id={booking.id}",
            reference=str(booking.id),
            expires_at=int(now.timestamp()) + STRIPE_MIN_SESSION_SECONDS,
        )
        try:
            current = await run_in_threadpool(provider.create_checkout_session, request)
        except payment_service.ProviderRejectedKey as exc:
            provider = payment_service._fall_back_to_mock(str(exc))
            current = await run_in_threadpool(provider.create_checkout_session, request)
        if current.amount_total != booking.total_price_paise:
            raise AppError(502, "payment_provider_error", "The payment service returned the wrong amount")

    # Now take the locks and store the session, if the booking is still waiting for it.
    _, _, booking = await _locked(session, booking_id)
    payable(booking)
    if booking.stripe_checkout_session_id != current.id:
        booking.stripe_checkout_session_id = current.id
        booking.payment_status = "pending"  # a new attempt after a declined one
        booking.updated_at = now
        await session.flush()
        audit_service.record(
            session, club_id=event.club_id, actor_id=user.id, action="fairseat.checkout_session_created",
            entity_type="ticket_booking", entity_id=booking.id,
            details={"provider": provider.name, "session_id": current.id, "paise": booking.total_price_paise},
        )
    return TicketCheckoutOut(
        booking_id=booking.id, provider=provider.name, is_mock=provider.is_mock, session_id=current.id,
        checkout_url=current.url, amount=booking.total_price_paise, currency=booking.currency,
        hold_expires_at=booking.hold_expires_at,
    )


# --- Recording a payment the provider has confirmed ------------------------------------


async def record_payment(
    session: AsyncSession, booking_id: uuid.UUID, *, payment_intent_id: str, paise: Any, currency: Any,
    now: datetime | None = None,
) -> str:
    """The provider says this booking was paid. Runs under the locks, touches only the database,
    and can be repeated: the same payment a second time changes nothing.

    Returns confirmed, refund_required, already_recorded, duplicate_payment, amount_mismatch
    or not_payable. The caller commits, and then sends any refund that is now owed.
    """
    now = now or fairseat_service.utcnow()
    settings = get_settings()
    locked = await _locked(session, booking_id)
    if locked is None:
        return "not_payable"
    event, category, booking = locked
    ticket_settings = locked.settings

    def note(action: str, **details) -> None:
        audit_service.record(
            session, club_id=event.club_id, actor_id=None, action=action, entity_type="ticket_booking",
            entity_id=booking.id, details={"payment_intent_id": payment_intent_id, **details},
        )

    if booking.payment_status in ("paid", "refunded"):
        if booking.stripe_payment_intent_id == payment_intent_id:
            return "already_recorded"
        # A second, different payment for one booking. It cannot be attached; flag it for the treasurer.
        note("fairseat.duplicate_payment", recorded=booking.stripe_payment_intent_id)
        return "duplicate_payment"
    if booking.payment_status == "not_required":
        note("fairseat.payment_for_free_booking")
        return "not_payable"
    try:
        matches = int(paise) == booking.total_price_paise and str(currency).lower() == booking.currency.lower()
    except (TypeError, ValueError):
        matches = False
    if not matches:
        note("fairseat.amount_mismatch", expected_paise=booking.total_price_paise, got=f"{paise} {currency}")
        return "amount_mismatch"

    # Decide, under the locks, whether the seats are still this booking's to have.
    reason = None
    if booking.status == "cancelled":
        reason = "booking_cancelled"
    elif not (booking.status == "pending_payment" and booking.hold_expires_at > now):
        # The hold has run out. Confirm only if the event is still selling and nobody else has
        # been given the seats since: both the category's seats and the event's total are counted.
        event_categories = list(await session.scalars(
            select(TicketCategory.id).where(TicketCategory.event_id == event.id)))
        counts = await fairseat_service.occupied_seats(session, event_categories, now)
        held = await fairseat_service._seats_held_by(session, event.id, booking.purchaser_user_id, now)
        selling = ticket_settings is not None and ticket_settings.sales_enabled and category.active
        if event.status != "published" or event.starts_at is None or now >= event.starts_at or not selling:
            reason = "event_not_bookable"
        elif (category.capacity - counts[category.id] < booking.quantity
              or ticket_settings.capacity - sum(counts.values()) < booking.quantity):
            reason = "seats_reassigned"
        elif held + booking.quantity > settings.fairseat_max_per_purchaser:
            reason = "purchaser_limit"

    booking.stripe_payment_intent_id = payment_intent_id
    booking.payment_status = "paid"
    booking.paid_at = now
    booking.hold_expires_at = None
    booking.updated_at = now
    if reason is None:
        booking.status = "confirmed"
    else:
        booking.status = "cancelled" if booking.status == "cancelled" else "expired"
        booking.refund_status = "required"
        booking.refund_reason = reason
    await session.flush()
    if reason is None:
        # Entitlements only. They carry no credential and admit nobody until GateSync issues one.
        session.add_all(fairseat_service._new_tickets(booking))
        await session.flush()

    # The money did arrive, so the ledger says so, exactly once per payment.
    await ledger_service.record_income(
        session,
        club_id=event.club_id,
        source_module="event_ticket",
        amount=rupees(booking.total_price_paise),
        created_by=booking.purchaser_user_id,
        payer_id=booking.purchaser_user_id,
        event_id=event.id,
        reference_id=booking.id,
        reference_type="ticket_booking",
        category=LEDGER_CATEGORY,
        description=f"Tickets: {booking.quantity} x {category.name} ({event.name})",
        payment_status="paid",
        idempotency_key=f"{payment_intent_id}:income",
    )
    note("fairseat.payment_recorded", outcome="confirmed" if reason is None else "refund_required", reason=reason)
    return "confirmed" if reason is None else "refund_required"


async def verify_payment(
    session: AsyncSession, user: CurrentUser, session_id: str, now: datetime | None = None
) -> tuple[uuid.UUID, str]:
    """The buyer is back from checkout. Ask the provider what happened, then record it."""
    row = (await session.execute(
        select(B.id, B.purchaser_user_id).where(B.stripe_checkout_session_id == session_id)
    )).first()
    if row is None or row.purchaser_user_id != user.id:
        raise not_found("Booking")
    checkout = await run_in_threadpool(payment_service.get_provider().verify_payment, session_id)  # no locks held
    if checkout is None or _booking_id_from(checkout.metadata) != row.id:
        raise bad_request("payment_verification_failed", "This payment could not be verified.")
    if checkout.payment_status != "paid" or not checkout.payment_intent_id:
        raise conflict("payment_not_completed", "This payment has not been completed.")
    result = await record_payment(
        session, row.id, payment_intent_id=checkout.payment_intent_id, paise=checkout.amount_total,
        currency=checkout.currency, now=now,
    )
    return row.id, result


async def mark_payment_failed(session: AsyncSession, booking_id: uuid.UUID, reason: str) -> bool:
    """Note a declined payment. The hold stays, so the buyer can try again until it runs out."""
    locked = await _locked(session, booking_id)
    if locked is None:
        return False
    event, _, booking = locked
    if booking.status != "pending_payment" or booking.payment_status != "pending":
        return False
    booking.payment_status = "failed"
    await session.flush()
    audit_service.record(
        session, club_id=event.club_id, actor_id=None, action="fairseat.payment_failed",
        entity_type="ticket_booking", entity_id=booking.id, details={"reason": reason},
    )
    return True


# --- Refunds ---------------------------------------------------------------------------


async def _apply_refund_result(
    session: AsyncSession, booking_id: uuid.UUID, result: RefundResult, now: datetime
) -> str | None:
    """Record what the provider said about a refund. Under the locks; database only."""
    locked = await _locked(session, booking_id)
    if locked is None:
        return None
    event, category, booking = locked
    if booking.refund_status is None or booking.refund_status == "succeeded":
        return booking.refund_status
    if result.id and booking.stripe_refund_id is None:
        booking.stripe_refund_id = result.id
    if result.status == "succeeded":
        booking.refund_status = "succeeded"
        booking.payment_status = "refunded"
        booking.refunded_at = now
        booking.refund_failure = None
        await session.flush()
        # A compensating entry. The income entry stays exactly as it was.
        await ledger_service.record_refund(
            session,
            club_id=event.club_id,
            source_module="event_ticket",
            amount=rupees(booking.total_price_paise),
            created_by=booking.purchaser_user_id,
            payee_id=booking.purchaser_user_id,
            event_id=event.id,
            reference_id=booking.id,
            reference_type="ticket_booking",
            category=LEDGER_CATEGORY,
            description=f"Ticket refund: {booking.quantity} x {category.name} ({event.name}), {booking.refund_reason}",
            idempotency_key=f"{booking.stripe_payment_intent_id}:refund",
        )
    elif result.status == "failed":
        booking.refund_status = "failed"
        booking.refund_failure = (result.failure_reason or "The payment provider could not complete the refund")[:500]
    else:
        booking.refund_status = "pending"
    booking.updated_at = now
    await session.flush()
    audit_service.record(
        session, club_id=event.club_id, actor_id=None, action="fairseat.refund_" + booking.refund_status,
        entity_type="ticket_booking", entity_id=booking.id,
        details={"refund_id": booking.stripe_refund_id, "reason": booking.refund_reason,
                 "failure": booking.refund_failure},
    )
    return booking.refund_status


async def process_refund(session: AsyncSession, booking_id: uuid.UUID, now: datetime | None = None) -> str | None:
    """Send an owed refund to the provider and record the answer. Commits as it goes, because
    the provider must not be called while database locks are held. Safe to call again."""
    now = now or fairseat_service.utcnow()
    # 1. Claim it, under the locks.
    locked = await _locked(session, booking_id)
    if locked is None:
        return None
    _, _, booking = locked
    if booking.refund_status not in ("required", "failed", "pending"):
        await session.rollback()
        return booking.refund_status
    if booking.refund_status != "pending":
        booking.refund_attempts += 1  # a fresh attempt; a repeat of a pending one reuses its key
    booking.refund_status = "pending"
    booking.refund_failure = None
    intent, paise, attempt = booking.stripe_payment_intent_id, booking.total_price_paise, booking.refund_attempts
    await session.commit()  # locks released here

    # 2. Ask the provider, holding nothing. The key makes a repeated request refund only once.
    provider_key = f"{intent}:refund" if attempt <= 1 else f"{intent}:refund:{attempt}"
    try:
        result = await run_in_threadpool(payment_service.get_provider().refund, intent, paise, provider_key)
    except AppError as exc:
        result = RefundResult(id=None, status="failed", failure_reason=exc.message)

    # 3. Record the answer, under the locks again.
    status = await _apply_refund_result(session, booking_id, result, now)
    await session.commit()
    return status


async def retry_refund(session: AsyncSession, user: CurrentUser, booking_id: uuid.UUID) -> uuid.UUID:
    """A treasurer or president sends a stuck refund again."""
    booking = await session.get(TicketBooking, booking_id)
    event = await session.get(Event, booking.event_id) if booking else None
    if booking is None or event.club_id != user.club_id:
        raise not_found("Booking")
    if booking.refund_status is None:
        raise conflict("no_refund_due", "No refund is owed on this booking.")
    if booking.refund_status == "succeeded":
        raise conflict("already_refunded", "This booking has already been refunded.")
    audit_service.record(
        session, club_id=event.club_id, actor_id=user.id, action="fairseat.refund_retry",
        entity_type="ticket_booking", entity_id=booking.id, details={"from": booking.refund_status},
    )
    await session.commit()
    await process_refund(session, booking_id)
    return booking_id


async def refund_cancelled_event(session: AsyncSession, event_id: uuid.UUID) -> int:
    """After an event's cancellation has been committed: send the refunds its paid bookings now
    owe. Does nothing when ticket payments are off or nothing is owed. One refund failing does
    not stop the others; a failed one stays on the treasurer's list to retry."""
    settings = get_settings()
    if not (settings.fairseat_enabled and settings.fairseat_payments_enabled):
        return 0
    owed = list(await session.scalars(
        select(B.id).where(B.event_id == event_id, B.refund_status == "required").order_by(B.id)
    ))
    await session.commit()  # end the read (nothing to save); process_refund runs its own short transactions
    for booking_id in owed:
        await process_refund(session, booking_id)
    return len(owed)


async def list_open_refunds(session: AsyncSession, user: CurrentUser) -> list[BookingOut]:
    """Refunds that are owed, in flight or failed, for the treasurer to watch."""
    bookings = list(await session.scalars(
        select(B).join(Event, Event.id == B.event_id)
        .where(Event.club_id == user.club_id, B.refund_status.in_(("required", "pending", "failed")))
        .order_by(B.updated_at, B.id)
    ))
    return await fairseat_service._present(session, bookings, fairseat_service.utcnow())


async def _external_refund(session: AsyncSession, booking_id: uuid.UUID, refund_id: str | None, now: datetime) -> str:
    """The provider reports a full refund we did not start (made in the Stripe Dashboard).
    Bring the booking into line: seats back, tickets revoked, refund recorded."""
    _, _, booking = await _locked(session, booking_id)
    if booking.refund_status is None:
        if booking.status == "confirmed":
            await fairseat_service._revoke_tickets(session, booking.id, now, even_if_used=True)
            booking.status = "cancelled"
            booking.cancelled_at = now
        booking.refund_status = "pending"
        booking.refund_reason = "refunded_by_provider"
        await session.flush()
    await _apply_refund_result(session, booking_id, RefundResult(id=refund_id, status="succeeded"), now)
    return "Booking refunded"


# --- Webhook ---------------------------------------------------------------------------


@dataclass
class WebhookResult:
    ack: TicketWebhookAck
    refund_booking_id: uuid.UUID | None = None   # send this refund after committing


async def handle_webhook(session: AsyncSession, body: bytes, signature: str, now: datetime | None = None) -> WebhookResult:
    """The provider's own report of a ticket payment or refund. Events about anything else
    (merchandise, for one) are acknowledged and ignored."""
    now = now or fairseat_service.utcnow()
    provider = payment_service.get_provider()
    try:
        event = provider.handle_webhook(body, signature)
    except InvalidWebhookSignature:
        raise bad_request("invalid_webhook_signature", "Webhook signature is missing or wrong") from None

    # Claim the event id. A repeat delivery conflicts on the unique key and stops here. If
    # anything below fails, this row is rolled back with it and the provider's retry starts clean.
    row_id = (await session.execute(
        insert(TicketPaymentEvent)
        .values(id=uuid.uuid4(), provider=provider.name, provider_event_id=event.id, event_type=event.type,
                outcome="received")
        .on_conflict_do_nothing(index_elements=["provider_event_id"])
        .returning(TicketPaymentEvent.id)
    )).scalar()
    if row_id is None:
        return WebhookResult(TicketWebhookAck(status="duplicate", detail="Event already handled"))

    data = event.data
    booking_id, intent, status, detail, refund_due = None, None, "ignored", f"Unhandled event {event.type}", False

    async def booking_for_intent(payment_intent: Any) -> uuid.UUID | None:
        if not isinstance(payment_intent, str):
            return None
        return await session.scalar(select(B.id).where(B.stripe_payment_intent_id == payment_intent))

    if event.type in ("checkout.session.completed", "payment_intent.succeeded"):
        from_session = event.type == "checkout.session.completed"
        booking_id = _booking_id_from(data.get("metadata"))
        intent = data.get("payment_intent") if from_session else data.get("id")
        paise = data.get("amount_total") if from_session else data.get("amount_received")
        if booking_id is None:
            detail = "Not a ticket payment"
        elif from_session and data.get("payment_status") != "paid":
            detail = "Session completed without payment"
        elif not isinstance(intent, str):
            detail = "No payment yet"
        else:
            result = await record_payment(
                session, booking_id, payment_intent_id=intent, paise=paise, currency=data.get("currency"), now=now)
            status = "ignored" if result in ("amount_mismatch", "not_payable", "duplicate_payment") else "processed"
            detail, refund_due = result, result == "refund_required"
            if result == "not_payable":
                booking_id = None
    elif event.type == "payment_intent.payment_failed":
        booking_id, intent = _booking_id_from(data.get("metadata")), data.get("id")
        if booking_id is None:
            detail = "Not a ticket payment"
        else:
            changed = await mark_payment_failed(session, booking_id, "provider_reported_failure")
            status, detail = ("processed", "Payment failed") if changed else ("ignored", "Booking is not waiting for payment")
            if not changed and await session.get(TicketBooking, booking_id) is None:
                booking_id = None
    elif event.type in ("refund.updated", "refund.failed", "charge.refund.updated"):
        intent = data.get("payment_intent")
        booking_id = await booking_for_intent(intent)
        if booking_id is None:
            detail = "Not a ticket payment"
        else:
            booking = await session.get(TicketBooking, booking_id)
            reported = data.get("status")
            if reported == "succeeded" and booking.refund_status is None:
                if int(data.get("amount") or 0) >= booking.total_price_paise:
                    status, detail = "processed", await _external_refund(session, booking_id, data.get("id"), now)
                else:
                    detail = "Partial refunds are not recorded automatically"
            elif reported in ("succeeded", "failed"):
                result = RefundResult(id=data.get("id"), status=reported, failure_reason=data.get("failure_reason"))
                status, detail = "processed", f"Refund {await _apply_refund_result(session, booking_id, result, now)}"
            else:
                detail = f"Refund is {reported}"
    elif event.type == "charge.refunded":
        intent = data.get("payment_intent")
        booking_id = await booking_for_intent(intent)
        if booking_id is None:
            detail = "Not a ticket payment"
        else:
            booking = await session.get(TicketBooking, booking_id)
            if int(data.get("amount_refunded") or 0) < booking.total_price_paise:
                detail = "Partial refunds are not recorded automatically"
            else:
                status, detail = "processed", await _external_refund(session, booking_id, None, now)

    await session.execute(
        update(TicketPaymentEvent).where(TicketPaymentEvent.id == row_id).values(
            ticket_booking_id=booking_id, payment_intent_id=intent if isinstance(intent, str) else None,
            outcome=status, detail=detail)
    )
    return WebhookResult(TicketWebhookAck(status=status, detail=detail), booking_id if refund_due else None)


# --- Mock provider ---------------------------------------------------------------------


async def simulate_mock_payment(session: AsyncSession, user: CurrentUser, session_id: str, succeed: bool) -> str:
    """Mock provider only: stand in for the buyer paying (or being declined) on the checkout page."""
    provider = payment_service.get_provider()
    if not isinstance(provider, payment_service.MockPaymentProvider):
        raise not_found("Mock payments")
    row = (await session.execute(
        select(B.id, B.purchaser_user_id).where(B.stripe_checkout_session_id == session_id)
    )).first()
    if row is None or row.purchaser_user_id != user.id:
        raise not_found("Booking")
    checkout = provider.simulate_payment(session_id, succeed)
    if checkout is None:
        raise conflict("session_expired", "This checkout session has expired. Open the booking again to pay.")
    if not succeed:
        await mark_payment_failed(session, row.id, "card_declined")
    return checkout.payment_status

