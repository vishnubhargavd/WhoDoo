"""Ticket payments on real PostgreSQL, with the mock provider (and Stripe's network calls
stubbed for the Stripe-specific tests). No real provider is contacted."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import pytest
import stripe
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.database import SessionLocal, asyncpg_dsn
from app.models.fairseat import EventTicket, TicketBooking, TicketPaymentEvent
from app.models.ledger import LedgerEntry
from app.models.merch import MerchOrder, PaymentEvent
from app.services import fairseat_payments, fairseat_service, ledger_service, payment_service
from tests.test_fairseat import API, acting, book, bookings, category, code, gala, occupied  # noqa: F401  (fixtures)
from tests.test_merch import STRIPE_KEY, WEBHOOK_SECRET, FakeStripe, _event

PAY = f"{API}/ticket-payments"


@pytest.fixture(autouse=True)
def _provider():
    """A clean mock provider before and after every test."""
    payment_service.get_provider.cache_clear()
    provider = payment_service.get_provider()
    provider.next_refund_status = "succeeded"
    provider.refunds.clear()          # the mock is one object for the whole test run
    provider.refund_results.clear()
    yield provider
    provider.next_refund_status = "succeeded"


@pytest.fixture
def checkout(client, auth):
    async def _checkout(user, booking):
        response = await client.post(
            f"{PAY}/stripe/create-checkout-session", headers=auth(user), json={"booking_id": booking["booking_id"]})
        assert response.status_code == 200, response.text
        return response.json()

    return _checkout


@pytest.fixture
def pay(client, auth, checkout):
    """Open a session, pay on the mock checkout page, come back and verify."""

    async def _pay(user, booking, session=None):
        session = session or await checkout(user, booking)
        paid = await client.post(f"{PAY}/mock/simulate", headers=auth(user), json={"session_id": session["session_id"]})
        assert paid.status_code == 200, paid.text
        return await client.post(f"{PAY}/stripe/verify", headers=auth(user), json={"session_id": session["session_id"]})

    return _pay


async def ticket_entries(db) -> list[LedgerEntry]:
    rows = await db.scalars(
        select(LedgerEntry).where(LedgerEntry.source_module == "event_ticket")
        .order_by(LedgerEntry.created_at, LedgerEntry.entry_type).execution_options(populate_existing=True))
    return list(rows)


async def row(db, booking) -> TicketBooking:
    return await db.scalar(
        select(TicketBooking).where(TicketBooking.id == booking["booking_id"]).execution_options(populate_existing=True))


async def tickets(db, booking) -> list[str]:
    rows = await db.scalars(
        select(EventTicket.status).where(EventTicket.booking_id == booking["booking_id"]).order_by(EventTicket.seat_number))
    return list(rows)


async def expire(db, booking) -> None:
    await db.execute(text("UPDATE ticket_bookings SET hold_expires_at = now() - interval '1 second' WHERE id = :id"),
                     {"id": booking["booking_id"]})
    await db.commit()


def completed(booking, session, intent, amount=None) -> dict:
    return {"id": session["session_id"], "object": "checkout.session", "payment_status": "paid",
            "amount_total": session["amount"] if amount is None else amount, "currency": "inr",
            "payment_intent": intent, "metadata": {"ticket_booking_id": booking["booking_id"]}}


async def webhook(client, event_id, kind, obj, secret=None):
    body, headers = _event(event_id, kind, obj, secret=secret)
    return await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)


async def balance(client, auth, world) -> float:
    return (await client.get(f"{API}/ledger/summary", headers=auth(world.treasurer))).json()["current_balance"]


# --- Paying before the hold runs out -------------------------------------------------------


async def test_payment_confirms_the_booking_and_posts_one_ledger_entry(client, db, world, auth, gala, category, book, checkout, pay):
    cat = await category(capacity=10, price=25050)
    booking = (await book(world.member, cat, quantity=2)).json()

    session = await checkout(world.member, booking)
    assert (session["provider"], session["is_mock"], session["amount"], session["currency"]) == ("mock", True, 50100, "INR")
    again = await checkout(world.member, booking)
    assert again["session_id"] == session["session_id"]  # the open session is reused

    # Nothing is confirmed by asking; an unpaid session does not verify.
    early = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]})
    assert early.status_code == 409 and code(early) == "payment_not_completed"
    assert (await row(db, booking)).status == "pending_payment" and await ticket_entries(db) == []

    paid = await pay(world.member, booking, session)
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["outcome"] == "confirmed"
    assert (body["booking"]["status"], body["booking"]["payment_status"], body["booking"]["hold_expires_at"]) == ("confirmed", "paid", None)
    assert body["booking"]["refund_status"] is None and body["booking"]["paid_at"] is not None

    # Two tickets, each with its own credential from GateSync (only the digest is stored).
    assert [(t["seat_number"], t["status"]) for t in body["booking"]["tickets"]] == [(1, "issued"), (2, "issued")]
    assert await db.scalar(select(func.count()).select_from(EventTicket).where(EventTicket.credential_digest.isnot(None))) == 2

    saved = await row(db, booking)
    assert saved.stripe_checkout_session_id == session["session_id"] and saved.stripe_payment_intent_id.startswith("pi_mock_")
    [entry] = await ticket_entries(db)
    assert (entry.entry_type, entry.direction, entry.source_module, entry.payment_status) == ("income", "credit", "event_ticket", "paid")
    assert entry.amount == Decimal("501.00") and isinstance(entry.amount, Decimal)   # 50100 paise, exactly
    assert (entry.event_id, entry.reference_id, entry.reference_type) == (gala.id, saved.id, "ticket_booking")
    assert (entry.payer_id, entry.category, entry.currency) == (world.member.id, "ticket_sales", "INR")
    assert entry.idempotency_key == f"{saved.stripe_payment_intent_id}:income"
    assert await balance(client, auth, world) == 501.0

    # The return route, a refresh and a repeat all change nothing.
    for _ in range(2):
        assert (await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]})).json()["outcome"] == "confirmed"
    back = await client.get(f"{PAY}/stripe/return?session_id={session['session_id']}", headers=auth(world.member))
    assert back.status_code == 200 and back.json()["outcome"] == "confirmed"
    assert len(await ticket_entries(db)) == 1 and await tickets(db, booking) == ["issued"] * 2
    assert await occupied(db, cat) == 2
    paid_again = await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.member), json={"booking_id": booking["booking_id"]})
    assert paid_again.status_code == 409 and code(paid_again) == "already_paid"
    # Merchandise is untouched.
    assert await db.scalar(select(func.count()).select_from(MerchOrder)) == 0
    assert await db.scalar(select(func.count()).select_from(PaymentEvent)) == 0


async def test_profit_and_loss_counts_seats_not_ledger_entries(client, db, world, auth, gala, category, book, pay):
    cat = await category(capacity=10, price=20000)
    await pay(world.member, (await book(world.member, cat, quantity=3)).json())
    await pay(world.outsider, (await book(world.outsider, cat, quantity=2)).json())
    await book(world.volunteer, cat, quantity=1)                                   # held, not paid
    free = await category(capacity=5, price=0, name="Free")
    await book(world.lead, free, quantity=2)                                       # confirmed, not sold

    result = await ledger_service.get_event_profit_loss(db, world.club.id, gala.id)
    assert len(await ticket_entries(db)) == 2            # two payments
    assert result.tickets_sold == 5                      # five seats
    assert result.ticket_income == Decimal("1000.00") and result.net_profit_loss == Decimal("1000.00")

    # An older-style ticket income entry, not tied to a booking, still counts as one ticket.
    await ledger_service.record_income(
        db, club_id=world.club.id, source_module="event_ticket", amount=Decimal("150"), created_by=world.treasurer.id,
        event_id=gala.id, idempotency_key="door-sale-1")
    await db.commit()
    assert (await ledger_service.get_event_profit_loss(db, world.club.id, gala.id)).tickets_sold == 6


async def test_only_the_purchaser_can_pay_and_only_what_is_payable(client, db, world, auth, category, book, checkout, monkeypatch):
    cat = await category(capacity=10)
    booking = (await book(world.member, cat)).json()
    session = await checkout(world.member, booking)
    request = {"booking_id": booking["booking_id"]}

    for extra in ({"amount": 1}, {"currency": "usd"}, {"user_id": str(world.president.id)}, {"payment_status": "paid"}):
        assert (await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.member), json=request | extra)).status_code == 422
    for other in (world.outsider, world.president, world.treasurer):
        assert (await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(other), json=request)).status_code == 404
        for path in ("mock/simulate", "stripe/verify"):
            assert (await client.post(f"{PAY}/{path}", headers=auth(other), json={"session_id": session["session_id"]})).status_code == 404
    assert (await client.post(f"{PAY}/stripe/create-checkout-session", json=request)).status_code == 401
    assert (await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": "cs_made_up_000"})).status_code == 404
    forged = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"], "paid": True})
    assert forged.status_code == 422

    free = (await book(world.volunteer, await category(capacity=5, price=0, name="Free"))).json()
    refused = await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.volunteer), json={"booking_id": free["booking_id"]})
    assert refused.status_code == 409 and code(refused) == "booking_not_payable"

    lapsed = (await book(world.lead, cat)).json()
    await db.execute(text("UPDATE ticket_bookings SET hold_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": lapsed["booking_id"]})
    await db.commit()
    too_late = await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.lead), json={"booking_id": lapsed["booking_id"]})
    assert too_late.status_code == 409 and code(too_late) == "hold_expired"

    await client.post(f"{API}/ticket-bookings/{booking['booking_id']}/cancel", headers=auth(world.member))
    gone = await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.member), json=request)
    assert gone.status_code == 409 and code(gone) == "booking_not_payable"

    monkeypatch.setattr(get_settings(), "fairseat_payments_enabled", False)
    off = await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.member), json=request)
    assert off.status_code == 404 and code(off) == "ticket_payments_disabled"
    assert await ticket_entries(db) == []


# --- Payment failure and retry -------------------------------------------------------------


async def test_declined_payment_issues_nothing_and_can_be_retried(client, db, world, auth, category, book, checkout, pay):
    cat = await category(capacity=1)
    booking = (await book(world.member, cat)).json()
    session = await checkout(world.member, booking)

    declined = await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": session["session_id"], "outcome": "failure"})
    assert declined.json()["payment_status"] == "unpaid"
    saved = await row(db, booking)
    assert (saved.status, saved.payment_status) == ("pending_payment", "failed")
    assert await tickets(db, booking) == [] and await ticket_entries(db) == []
    assert await occupied(db, cat) == 1  # the hold stays while it lasts
    refused = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]})
    assert refused.status_code == 409 and code(refused) == "payment_not_completed"

    retry = await pay(world.member, booking)
    assert retry.status_code == 200 and retry.json()["outcome"] == "confirmed"
    assert len(await ticket_entries(db)) == 1 and await tickets(db, booking) == ["issued"]


async def test_failure_webhook_and_unrelated_events(client, db, world, auth, category, book, checkout):
    cat = await category(capacity=2)
    booking = (await book(world.member, cat)).json()
    await checkout(world.member, booking)

    failed = await webhook(client, "evt_t_failed", "payment_intent.payment_failed", {
        "id": "pi_declined", "object": "payment_intent", "metadata": {"ticket_booking_id": booking["booking_id"]}})
    assert failed.json() == {"status": "processed", "detail": "Payment failed"}
    assert (await row(db, booking)).payment_status == "failed"

    # A merchandise event arriving at the ticket endpoint is acknowledged and ignored, and the other way round.
    merch = await webhook(client, "evt_t_merch", "checkout.session.completed", {
        "id": "cs_merch", "object": "checkout.session", "payment_status": "paid", "amount_total": 79900,
        "currency": "inr", "payment_intent": "pi_merch", "metadata": {"merch_order_id": str(uuid.uuid4())}})
    assert merch.json() == {"status": "ignored", "detail": "Not a ticket payment"}
    session = {"session_id": "cs_x", "amount": 25000}
    body, headers = _event("evt_ticket_at_merch", "checkout.session.completed", completed(booking, session, "pi_ticket"))
    at_merch = await client.post(f"{API}/payments/stripe/webhook", content=body, headers=headers)
    assert at_merch.status_code == 200 and at_merch.json()["status"] == "ignored"
    # Stripe sends every event to both endpoints: one claiming it must not hide it from the other.
    same_id = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
    assert same_id.json()["status"] == "processed" and (await row(db, booking)).status == "confirmed"

    other = await webhook(client, "evt_t_other", "customer.created", {"id": "cus_1", "object": "customer"})
    assert other.json()["status"] == "ignored"
    unsigned = await client.post(f"{PAY}/stripe/webhook", content=body)
    assert unsigned.status_code == 400 and code(unsigned) == "invalid_webhook_signature"
    wrong = await webhook(client, "evt_t_wrong", "checkout.session.completed", completed(booking, session, "pi_z"), secret="whsec_guessed")
    assert wrong.status_code == 400


# --- Duplicate success reports --------------------------------------------------------------


async def test_duplicate_and_simultaneous_success_reports_make_one_payment(client, db, world, auth, gala, category, book, checkout):
    cat = await category(capacity=5, price=30000)
    booking = (await book(world.member, cat, quantity=3)).json()
    session = await checkout(world.member, booking)
    await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": session["session_id"]})
    intent = payment_service.get_provider().verify_payment(session["session_id"]).payment_intent_id

    session_event = _event("evt_dup_session", "checkout.session.completed", completed(booking, session, intent))
    intent_event = _event("evt_dup_intent", "payment_intent.succeeded", {
        "id": intent, "object": "payment_intent", "amount_received": 90000, "currency": "inr",
        "metadata": {"ticket_booking_id": booking["booking_id"]}})

    def post(event):
        return client.post(f"{PAY}/stripe/webhook", content=event[0], headers=event[1])

    def verify():
        return client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]})

    # Two deliveries of each event and three returns from the browser, all at once.
    results = await asyncio.gather(post(session_event), post(session_event), post(intent_event), post(intent_event),
                                   verify(), verify(), verify())
    assert all(response.status_code == 200 for response in results), [r.text for r in results]
    acks = [response.json()["status"] for response in results[:4]]
    assert sorted(acks) == ["duplicate", "duplicate", "processed", "processed"]
    assert {response.json()["outcome"] for response in results[4:]} == {"confirmed"}

    [entry] = await ticket_entries(db)
    assert entry.amount == Decimal("900.00")
    assert await tickets(db, booking) == ["issued"] * 3
    assert await occupied(db, cat) == 3 and await balance(client, auth, world) == 900.0
    assert await db.scalar(select(func.count()).select_from(TicketPaymentEvent)) == 2
    logged = await db.scalar(select(TicketPaymentEvent).where(TicketPaymentEvent.provider_event_id == "evt_dup_session"))
    assert (str(logged.ticket_booking_id), logged.payment_intent_id) == (booking["booking_id"], intent)

    # A late repeat with a new event id is recognised by the payment itself.
    late = await webhook(client, "evt_dup_late", "checkout.session.completed", completed(booking, session, intent))
    assert late.json() == {"status": "processed", "detail": "already_recorded"}
    # A different payment for the same booking is flagged, not applied.
    second = await webhook(client, "evt_dup_other", "checkout.session.completed", completed(booking, session, "pi_another"))
    assert second.json() == {"status": "ignored", "detail": "duplicate_payment"}
    assert len(await ticket_entries(db)) == 1 and (await row(db, booking)).stripe_payment_intent_id == intent


async def test_wrong_amount_or_currency_is_not_accepted(client, db, world, auth, category, book, checkout):
    cat = await category(capacity=5, price=30000)
    booking = (await book(world.member, cat)).json()
    session = await checkout(world.member, booking)
    cheap = await webhook(client, "evt_cheap", "checkout.session.completed", completed(booking, session, "pi_cheap", amount=100))
    assert cheap.json() == {"status": "ignored", "detail": "amount_mismatch"}
    dollars = completed(booking, session, "pi_usd") | {"currency": "usd"}
    assert (await webhook(client, "evt_usd", "checkout.session.completed", dollars)).json()["detail"] == "amount_mismatch"
    saved = await row(db, booking)
    assert (saved.status, saved.payment_status, saved.stripe_payment_intent_id) == ("pending_payment", "pending", None)
    assert await ticket_entries(db) == [] and await tickets(db, booking) == []

    # The same check on the browser's return, with the provider reporting a different total.
    await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": session["session_id"]})
    payment_service.get_provider().verify_payment(session["session_id"]).amount_total = 1
    short = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]})
    assert short.status_code == 400 and code(short) == "amount_mismatch"
    assert (await row(db, booking)).status == "pending_payment" and await ticket_entries(db) == []


# --- Payment after the hold has run out -----------------------------------------------------


async def test_late_payment_is_confirmed_when_the_seats_are_still_free(client, db, world, auth, category, book, checkout, pay):
    cat = await category(capacity=2)
    booking = (await book(world.member, cat, quantity=2)).json()
    session = await checkout(world.member, booking)
    await expire(db, booking)
    assert await occupied(db, cat) == 0

    late = await pay(world.member, booking, session)
    assert late.status_code == 200 and late.json()["outcome"] == "confirmed"
    assert await occupied(db, cat) == 2 and await tickets(db, booking) == ["issued"] * 2
    assert len(await ticket_entries(db)) == 1 and payment_service.get_provider().refunds == {}


async def test_late_payment_is_refunded_when_the_seat_went_to_someone_else(client, db, world, auth, category, book, checkout, pay, _provider):
    cat = await category(capacity=1, price=40000)
    slow = (await book(world.member, cat)).json()
    slow_session = await checkout(world.member, slow)
    await expire(db, slow)

    quick = (await book(world.outsider, cat)).json()             # the freed seat is taken
    assert (await pay(world.outsider, quick)).json()["outcome"] == "confirmed"

    late = await pay(world.member, slow, slow_session)            # the first buyer's money arrives now
    assert late.status_code == 200
    body = late.json()
    assert body["outcome"] == "refunded"
    assert (body["booking"]["status"], body["booking"]["payment_status"], body["booking"]["refund_status"]) == ("expired", "refunded", "succeeded")
    assert body["booking"]["refund_reason"] == "seats_reassigned" and body["booking"]["tickets"] == []

    saved = await row(db, slow)
    assert _provider.refunds[f"{saved.stripe_payment_intent_id}:refund"] == (saved.stripe_payment_intent_id, 40000)
    assert await occupied(db, cat) == 1 and await tickets(db, slow) == [] and await tickets(db, quick) == ["issued"]
    # The ledger shows all three movements; only the kept sale remains in the balance.
    kinds = sorted((entry.entry_type, entry.reference_id == saved.id) for entry in await ticket_entries(db))
    assert kinds == [("income", False), ("income", True), ("refund", True)]
    refund = next(entry for entry in await ticket_entries(db) if entry.entry_type == "refund")
    assert (refund.direction, refund.amount, refund.idempotency_key) == ("debit", Decimal("400.00"), f"{saved.stripe_payment_intent_id}:refund")
    assert await balance(client, auth, world) == 400.0

    # Coming back again refunds nothing more.
    again = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": slow_session["session_id"]})
    assert again.json()["outcome"] == "refunded"
    assert len(_provider.refunds) == 1 and len(await ticket_entries(db)) == 3


async def test_late_payment_racing_a_new_booking_never_oversells(client, db, world, auth, category, book, checkout, pay):
    buyers = [world.outsider, world.volunteer, world.lead, world.lead2]
    for round_number, rival in enumerate(buyers):
        cat = await category(capacity=1, name=f"Round {round_number}")
        slow = (await book(world.member, cat)).json()
        session = await checkout(world.member, slow)
        await expire(db, slow)

        late, rival_booking = await asyncio.gather(pay(world.member, slow, session), book(rival, cat))
        assert late.status_code == 200
        outcome = (late.json()["outcome"], rival_booking.status_code)
        # Either the late payer got the seat back first, or the rival did and the payer is refunded.
        assert outcome in {("confirmed", 409), ("refunded", 201)}, outcome
        assert await occupied(db, cat) == 1
        live = [b for b in await bookings(db, cat) if b.status in ("confirmed", "pending_payment")]
        assert len(live) == 1


async def test_payment_for_a_cancelled_hold_or_closed_event_is_refunded(client, db, world, auth, gala, category, book, checkout, pay):
    cat = await category(capacity=5)
    cancelled = (await book(world.member, cat)).json()
    session = await checkout(world.member, cancelled)
    await client.post(f"{API}/ticket-bookings/{cancelled['booking_id']}/cancel", headers=auth(world.member))
    result = (await pay(world.member, cancelled, session)).json()
    assert (result["outcome"], result["booking"]["status"], result["booking"]["refund_reason"]) == ("refunded", "cancelled", "booking_cancelled")

    held = (await book(world.outsider, cat)).json()
    held_session = await checkout(world.outsider, held)
    await expire(db, held)
    await db.execute(text("UPDATE events SET status = 'cancelled' WHERE id = :id"), {"id": gala.id})
    await db.commit()
    closed = (await pay(world.outsider, held, held_session)).json()
    assert (closed["outcome"], closed["booking"]["refund_reason"]) == ("refunded", "event_not_bookable")
    assert await occupied(db, cat) == 0 and await db.scalar(select(func.count()).select_from(EventTicket)) == 0
    assert await balance(client, auth, world) == 0.0


# --- Cancelling after payment, and refund states ----------------------------------------------


async def test_cancelling_a_paid_booking_refunds_it_and_frees_the_seat(client, db, world, auth, category, book, pay, _provider):
    cat = await category(capacity=1, price=35000)
    booking = (await book(world.member, cat)).json()
    await pay(world.member, booking)
    cancel_url = f"{API}/ticket-bookings/{booking['booking_id']}/cancel"
    assert (await client.post(cancel_url, headers=auth(world.outsider))).status_code == 404

    for _ in range(2):  # twice is the same as once
        cancelled = await client.post(cancel_url, headers=auth(world.member))
        assert cancelled.status_code == 200
        body = cancelled.json()
        assert (body["status"], body["payment_status"], body["refund_status"], body["refund_reason"]) == (
            "cancelled", "refunded", "succeeded", "cancelled_by_purchaser")
        assert [ticket["status"] for ticket in body["tickets"]] == ["revoked"]
    assert len(_provider.refunds) == 1
    income, refund = await ticket_entries(db)
    assert (income.entry_type, income.amount) == ("income", Decimal("350.00"))      # history is kept, not deleted
    assert (refund.entry_type, refund.direction, refund.amount) == ("refund", "debit", Decimal("350.00"))
    assert await balance(client, auth, world) == 0.0

    assert await occupied(db, cat) == 0
    assert (await book(world.outsider, cat)).status_code == 201


async def test_refund_stays_pending_until_the_provider_confirms_it(client, db, world, auth, category, book, pay, _provider):
    cat = await category(capacity=1, price=35000)
    booking = (await book(world.member, cat)).json()
    await pay(world.member, booking)
    _provider.next_refund_status = "pending"
    cancel_url = f"{API}/ticket-bookings/{booking['booking_id']}/cancel"

    cancelled = (await client.post(cancel_url, headers=auth(world.member))).json()
    # Not claimed as refunded: the money has not been confirmed as returned.
    assert (cancelled["status"], cancelled["payment_status"], cancelled["refund_status"]) == ("cancelled", "paid", "pending")
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income"]
    assert await balance(client, auth, world) == 350.0
    # The seat is free and the tickets are revoked straight away, whatever the refund is doing.
    assert await occupied(db, cat) == 0 and await tickets(db, booking) == ["revoked"]

    # Cancelling again while the refund is in flight starts nothing new.
    again = (await client.post(cancel_url, headers=auth(world.member))).json()
    assert again["refund_status"] == "pending" and len(_provider.refunds) == 1
    waiting = await client.get(f"{PAY}/refunds", headers=auth(world.treasurer))
    assert [item["booking_id"] for item in waiting.json()] == [booking["booking_id"]]
    assert (await client.get(f"{PAY}/refunds", headers=auth(world.member))).status_code == 403

    saved = await row(db, booking)
    refund_object = {"id": saved.stripe_refund_id, "object": "refund", "status": "succeeded",
                     "payment_intent": saved.stripe_payment_intent_id, "amount": 35000}
    for event_id, expected in (("evt_refund_ok", "processed"), ("evt_refund_ok", "duplicate"), ("evt_refund_ok_2", "processed")):
        ack = await webhook(client, event_id, "refund.updated", refund_object)
        assert ack.json()["status"] == expected
    saved = await row(db, booking)
    assert (saved.payment_status, saved.refund_status) == ("refunded", "succeeded") and saved.refunded_at is not None
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income", "refund"]
    assert await balance(client, auth, world) == 0.0
    assert (await client.get(f"{PAY}/refunds", headers=auth(world.treasurer))).json() == []


async def test_failed_refund_is_recorded_and_can_be_retried(client, db, world, auth, category, book, pay, _provider, monkeypatch):
    cat = await category(capacity=2, price=35000)
    first = (await book(world.member, cat)).json()
    await pay(world.member, first)
    _provider.next_refund_status = "failed"

    cancelled = (await client.post(f"{API}/ticket-bookings/{first['booking_id']}/cancel", headers=auth(world.member))).json()
    assert (cancelled["status"], cancelled["payment_status"], cancelled["refund_status"]) == ("cancelled", "paid", "failed")
    saved = await row(db, first)
    assert saved.refund_failure == "declined by the mock provider" and saved.refund_attempts == 1
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income"]   # no refund entry for a refund that failed

    retry_url = f"{PAY}/refunds/{first['booking_id']}/retry"
    assert (await client.post(retry_url, headers=auth(world.member))).status_code == 403
    _provider.next_refund_status = "succeeded"
    retried = await client.post(retry_url, headers=auth(world.treasurer))
    assert retried.status_code == 200 and retried.json()["outcome"] == "refunded"
    intent = saved.stripe_payment_intent_id
    assert set(_provider.refunds) == {f"{intent}:refund", f"{intent}:refund:2"}   # a new attempt, a new provider key
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income", "refund"]
    done = await client.post(retry_url, headers=auth(world.treasurer))
    assert done.status_code == 409 and code(done) == "already_refunded"

    # The provider being unreachable is a failed refund too, never a silent success.
    second = (await book(world.outsider, cat)).json()
    await pay(world.outsider, second)

    def unreachable(*args):
        raise AppError(502, "payment_provider_error", "The payment service could not complete this request. Try again.")

    monkeypatch.setattr(_provider, "refund", unreachable)
    down = (await client.post(f"{API}/ticket-bookings/{second['booking_id']}/cancel", headers=auth(world.outsider))).json()
    assert (down["payment_status"], down["refund_status"]) == ("paid", "failed")
    assert "could not complete" in (await row(db, second)).refund_failure
    never_paid = (await book(world.volunteer, cat)).json()
    none_due = await client.post(f"{PAY}/refunds/{never_paid['booking_id']}/retry", headers=auth(world.treasurer))
    assert none_due.status_code == 409 and code(none_due) == "no_refund_due"


async def test_refund_failure_reported_later_by_webhook(client, db, world, auth, category, book, pay, _provider):
    cat = await category(capacity=1)
    booking = (await book(world.member, cat)).json()
    await pay(world.member, booking)
    _provider.next_refund_status = "pending"
    await client.post(f"{API}/ticket-bookings/{booking['booking_id']}/cancel", headers=auth(world.member))
    saved = await row(db, booking)
    ack = await webhook(client, "evt_refund_failed", "refund.failed", {
        "id": saved.stripe_refund_id, "object": "refund", "status": "failed", "failure_reason": "expired_or_canceled_card",
        "payment_intent": saved.stripe_payment_intent_id, "amount": 25000})
    assert ack.json() == {"status": "processed", "detail": "Refund failed"}
    saved = await row(db, booking)
    assert (saved.payment_status, saved.refund_status, saved.refund_failure) == ("paid", "failed", "expired_or_canceled_card")
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income"]


async def test_paid_booking_cannot_be_cancelled_after_the_event_starts_or_once_used(client, db, world, auth, gala, category, book, pay):
    cat = await category(capacity=5)
    booking = (await book(world.member, cat)).json()
    await pay(world.member, booking)
    async with SessionLocal() as session:
        with pytest.raises(AppError) as started:
            await fairseat_service.cancel_booking(session, acting(world.member), uuid.UUID(booking["booking_id"]), now=gala.starts_at)
        assert (started.value.status_code, started.value.code) == (409, "event_started")

    await db.execute(text("UPDATE event_tickets SET status = 'checked_in', checked_in_at = now() WHERE booking_id = :id"), {"id": booking["booking_id"]})
    await db.commit()
    used = await client.post(f"{API}/ticket-bookings/{booking['booking_id']}/cancel", headers=auth(world.member))
    assert used.status_code == 409 and code(used) == "ticket_already_used"
    saved = await row(db, booking)
    assert (saved.status, saved.payment_status, saved.refund_status) == ("confirmed", "paid", None)
    assert payment_service.get_provider().refunds == {}


async def test_refund_made_in_the_stripe_dashboard_cancels_the_booking(client, db, world, auth, category, book, pay):
    cat = await category(capacity=1, price=35000)
    booking = (await book(world.member, cat)).json()
    await pay(world.member, booking)
    intent = (await row(db, booking)).stripe_payment_intent_id

    partial = await webhook(client, "evt_partial", "charge.refunded", {"id": "ch_1", "object": "charge", "payment_intent": intent, "amount": 35000, "amount_refunded": 1000})
    assert partial.json()["status"] == "ignored" and (await row(db, booking)).status == "confirmed"

    for event_id in ("evt_full_1", "evt_full_2"):
        full = await webhook(client, event_id, "charge.refunded", {"id": "ch_1", "object": "charge", "payment_intent": intent, "amount": 35000, "amount_refunded": 35000})
        assert full.json() == {"status": "processed", "detail": "Booking refunded"}
    saved = await row(db, booking)
    assert (saved.status, saved.payment_status, saved.refund_status, saved.refund_reason) == ("cancelled", "refunded", "succeeded", "refunded_by_provider")
    assert await tickets(db, booking) == ["revoked"] and await occupied(db, cat) == 0
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income", "refund"]


async def test_cancelling_an_event_releases_and_refunds_every_booking(client, db, world, auth, gala, category, book, pay, _provider):
    cat = await category(capacity=10, price=20000)
    paid = [(await book(buyer, cat, quantity=2)).json() for buyer in (world.member, world.outsider)]
    for buyer, booking in zip((world.member, world.outsider), paid):
        await pay(buyer, booking)
    held = (await book(world.volunteer, cat)).json()
    free = (await book(world.lead, await category(capacity=5, price=0, name="Free"))).json()

    # What Member 2's change_status would do: release inside its transaction, refund after committing.
    async with SessionLocal() as session:
        owed = await fairseat_service.release_bookings_for_event(session, gala.id)
        await session.commit()
        assert sorted(map(str, owed)) == sorted(booking["booking_id"] for booking in paid)
        for booking_id in owed:
            assert await fairseat_payments.process_refund(session, booking_id) == "succeeded"

    assert await occupied(db, cat) == 0
    for booking in paid:
        saved = await row(db, booking)
        assert (saved.status, saved.payment_status, saved.refund_reason) == ("cancelled", "refunded", "event_cancelled")
        assert await tickets(db, booking) == ["revoked"] * 2
    assert (await row(db, held)).status == "cancelled" and (await row(db, free)).status == "cancelled"
    assert len(_provider.refunds) == 2 and await balance(client, auth, world) == 0.0
    async with SessionLocal() as session:  # nothing left to release
        assert await fairseat_service.release_bookings_for_event(session, gala.id) == []


async def test_event_cancelled_through_its_status_route_refunds_paid_bookings(client, db, world, auth, gala, category, book, pay, _provider):
    """The whole path: the event's status change releases bookings in its own transaction, and
    the route sends the refunds only after that has been committed."""
    cat = await category(capacity=10, price=20000)
    first, second = (await book(world.member, cat, quantity=2)).json(), (await book(world.outsider, cat)).json()
    await pay(world.member, first)
    await pay(world.outsider, second)
    intents = [(await row(db, booking)).stripe_payment_intent_id for booking in (first, second)]
    # The first refund fails at the provider; that must not stop the event being cancelled or the other refund.
    _provider.refund_results[f"{intents[0]}:refund"] = payment_service.RefundResult(None, "failed", "card closed")
    _provider.refunds[f"{intents[0]}:refund"] = (intents[0], 40000)

    cancelled = await client.post(f"{API}/events/{gala.id}/status", headers=auth(world.president), json={"status": "cancelled"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

    one, two = await row(db, first), await row(db, second)
    assert (one.status, one.payment_status, one.refund_status, one.refund_failure) == ("cancelled", "paid", "failed", "card closed")
    assert (two.status, two.payment_status, two.refund_status, two.refund_reason) == ("cancelled", "refunded", "succeeded", "event_cancelled")
    assert await tickets(db, first) == ["revoked"] * 2 and await occupied(db, cat) == 0
    assert sorted(entry.entry_type for entry in await ticket_entries(db)) == ["income", "income", "refund"]
    owed = await client.get(f"{PAY}/refunds", headers=auth(world.treasurer))
    assert [item["booking_id"] for item in owed.json()] == [first["booking_id"]]
    retried = await client.post(f"{PAY}/refunds/{first['booking_id']}/retry", headers=auth(world.treasurer))
    assert retried.json()["outcome"] == "refunded" and await balance(client, auth, world) == 0.0


async def test_cancellation_and_a_payment_arriving_at_the_same_moment(client, db, world, auth, category, book, checkout, _provider):
    """A payment and the event's cancellation race. Either the payment confirms first and is
    then refunded as part of the cancellation, or it arrives after and is refunded as a late
    payment. Either way: no live booking on a cancelled event, and the money goes back once."""
    from app.models.shared import Event as EventRow

    for round_number in range(4):
        event = EventRow(id=uuid.uuid4(), club_id=world.club.id, name=f"Pay race {round_number}", lead_id=world.lead.id,
                         starts_at=datetime.now(timezone.utc) + timedelta(days=7),
                         ends_at=datetime.now(timezone.utc) + timedelta(days=7, hours=2),
                         venue="Hall", status="published", created_by=world.president.id)
        db.add(event)
        await db.commit()
        cat = await category(capacity=3, event=event)
        booking = (await book(world.member, cat)).json()
        session = await checkout(world.member, booking)
        await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": session["session_id"]})

        verify, cancel = await asyncio.gather(
            client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]}),
            client.post(f"{API}/events/{event.id}/status", headers=auth(world.president), json={"status": "cancelled"}))
        assert cancel.status_code == 200 and verify.status_code == 200, (verify.text, cancel.text)
        saved = await row(db, booking)
        assert saved.status in ("cancelled", "expired") and (saved.payment_status, saved.refund_status) == ("refunded", "succeeded")
        assert saved.refund_reason in ("event_cancelled", "event_not_bookable", "booking_cancelled")
        assert await occupied(db, cat) == 0 and await tickets(db, booking) in ([], ["revoked"])
        assert _provider.refunds[f"{saved.stripe_payment_intent_id}:refund"] == (saved.stripe_payment_intent_id, 25000)
    kinds = [entry.entry_type for entry in await ticket_entries(db)]
    assert (kinds.count("income"), kinds.count("refund")) == (4, 4) and await balance(client, auth, world) == 0.0


async def test_out_of_order_webhooks(client, db, world, auth, category, book, checkout, _provider):
    cat = await category(capacity=5)
    booking = (await book(world.member, cat)).json()
    session = await checkout(world.member, booking)

    # A refund report for a payment we have never seen is acknowledged and ignored.
    stray = await webhook(client, "evt_o_stray", "refund.updated", {
        "id": "re_unknown", "object": "refund", "status": "succeeded", "payment_intent": "pi_never_seen", "amount": 25000})
    assert stray.json() == {"status": "ignored", "detail": "Not a ticket payment"}

    # The PaymentIntent event arrives before the session event, and a failure report arrives after the success.
    intent_first = await webhook(client, "evt_o_intent", "payment_intent.succeeded", {
        "id": "pi_order_1", "object": "payment_intent", "amount_received": 25000, "currency": "inr",
        "metadata": {"ticket_booking_id": booking["booking_id"]}})
    assert intent_first.json() == {"status": "processed", "detail": "confirmed"}
    session_second = await webhook(client, "evt_o_session", "checkout.session.completed", completed(booking, session, "pi_order_1"))
    assert session_second.json() == {"status": "processed", "detail": "already_recorded"}
    late_failure = await webhook(client, "evt_o_failed", "payment_intent.payment_failed", {
        "id": "pi_order_0", "object": "payment_intent", "metadata": {"ticket_booking_id": booking["booking_id"]}})
    assert late_failure.json()["status"] == "ignored"
    saved = await row(db, booking)
    assert (saved.status, saved.payment_status) == ("confirmed", "paid") and len(await ticket_entries(db)) == 1

    # The refund's "succeeded" arrives twice, and a stale "pending" arrives after it.
    _provider.next_refund_status = "pending"
    await client.post(f"{API}/ticket-bookings/{booking['booking_id']}/cancel", headers=auth(world.member))
    refund_id = (await row(db, booking)).stripe_refund_id
    report = {"id": refund_id, "object": "refund", "payment_intent": "pi_order_1", "amount": 25000}
    for event_id, state, expected in (("evt_o_r1", "succeeded", "processed"), ("evt_o_r2", "succeeded", "processed"),
                                      ("evt_o_r3", "pending", "ignored"), ("evt_o_r4", "failed", "processed")):
        ack = await webhook(client, event_id, "refund.updated", report | {"status": state})
        assert ack.json()["status"] == expected, (state, ack.text)
    saved = await row(db, booking)
    assert (saved.payment_status, saved.refund_status) == ("refunded", "succeeded")   # a later "failed" cannot undo it
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income", "refund"]


async def test_late_payment_respects_paused_sales_and_event_wide_capacity(client, db, world, auth, gala, category, book, checkout, pay):
    general, vip = await category(capacity=2, name="General"), await category(capacity=1, name="VIP", price=90000)
    slow = (await book(world.member, general, quantity=2)).json()
    slow_session = await checkout(world.member, slow)
    await expire(db, slow)

    # The organizer regroups while the hold is lapsed: General shrinks to 1, VIP grows to 2 (event still 3).
    config_url = f"{API}/events/{gala.id}/tickets"
    current = (await client.get(config_url, headers=auth(world.president))).json()
    body = {name: current[name] for name in ("sales_enabled", "capacity", "opens_at", "closes_at", "max_per_booking")}
    body["categories"] = [{"id": general["id"], "name": "General", "price_paise": 25000, "capacity": 1},
                          {"id": vip["id"], "name": "VIP", "price_paise": 90000, "capacity": 2}]
    assert (await client.put(config_url, headers=auth(world.president), json=body)).status_code == 200
    late = (await pay(world.member, slow, slow_session)).json()
    assert (late["outcome"], late["booking"]["refund_reason"]) == ("refunded", "seats_reassigned")   # two seats no longer fit
    assert await occupied(db, general) == 0

    # Sales paused while another hold is lapsed: its late payment is refunded, not revived.
    other = (await book(world.outsider, vip)).json()
    other_session = await checkout(world.outsider, other)
    await expire(db, other)
    assert (await client.put(config_url, headers=auth(world.president), json=body | {"sales_enabled": False})).status_code == 200
    paused = (await pay(world.outsider, other, other_session)).json()
    assert (paused["outcome"], paused["booking"]["refund_reason"]) == ("refunded", "event_not_bookable")
    assert await db.scalar(select(func.count()).select_from(EventTicket)) == 0 and await balance(client, auth, world) == 0.0


# --- The provider is never called while locks are held ------------------------------------------


async def test_provider_calls_happen_outside_database_locks(client, db, world, auth, gala, category, book, checkout, _provider, monkeypatch):
    cat = await category(capacity=5)
    booking = (await book(world.member, cat)).json()
    dsn = asyncpg_dsn(get_settings().database_url)
    seen: list[tuple[str, bool]] = []

    def probe(name, original):
        """Wrap a provider call: while it runs, try to lock the event, settings, category and booking rows
        from another connection. If the caller held any of them, NOWAIT fails at once."""

        async def rows_are_free() -> bool:
            conn = await asyncpg.connect(dsn)
            try:
                async with conn.transaction():
                    await conn.execute("SELECT 1 FROM events WHERE id = $1 FOR UPDATE NOWAIT", gala.id)
                    await conn.execute("SELECT 1 FROM event_ticket_settings WHERE event_id = $1 FOR UPDATE NOWAIT", gala.id)
                    await conn.execute("SELECT 1 FROM ticket_categories WHERE id = $1 FOR UPDATE NOWAIT", uuid.UUID(cat["id"]))
                    await conn.execute("SELECT 1 FROM ticket_bookings WHERE id = $1 FOR UPDATE NOWAIT", uuid.UUID(booking["booking_id"]))
                return True
            except asyncpg.exceptions.LockNotAvailableError:
                return False
            finally:
                await conn.close()

        def wrapped(*args, **kwargs):  # runs in a worker thread, so it can use its own event loop
            seen.append((name, asyncio.run(rows_are_free())))
            return original(*args, **kwargs)

        return wrapped

    for name in ("create_checkout_session", "verify_payment", "refund"):
        monkeypatch.setattr(_provider, name, probe(name, getattr(_provider, name)))

    session = await checkout(world.member, booking)
    await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": session["session_id"]})
    assert (await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": session["session_id"]})).status_code == 200
    cancelled = await client.post(f"{API}/ticket-bookings/{booking['booking_id']}/cancel", headers=auth(world.member))
    assert cancelled.json()["refund_status"] == "succeeded"

    assert {name for name, _ in seen} == {"create_checkout_session", "verify_payment", "refund"}
    assert all(free for _, free in seen), seen


# --- Stripe provider (network calls stubbed) ----------------------------------------------------


@pytest.fixture
def stripe_mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", "stripe")
    monkeypatch.setattr(settings, "stripe_secret_key", STRIPE_KEY)
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    fake = FakeStripe()
    monkeypatch.setattr(stripe.checkout.Session, "create", fake.create_session)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake.retrieve_session)
    monkeypatch.setattr(stripe.Refund, "create", fake.create_refund)
    payment_service.get_provider.cache_clear()
    yield fake
    monkeypatch.undo()
    payment_service.get_provider.cache_clear()


async def test_stripe_ticket_checkout_refund_and_webhook(client, db, world, auth, gala, category, book, checkout, stripe_mode):
    cat = await category(capacity=5, price=25000)
    booking = (await book(world.member, cat, quantity=2)).json()
    session = await checkout(world.member, booking)
    assert (session["provider"], session["is_mock"], session["checkout_url"]) == ("stripe", False, "https://checkout.stripe.com/c/pay/cs_test_1")
    assert STRIPE_KEY not in json.dumps(session)

    [sent] = stripe_mode.created
    [line] = sent["line_items"]
    assert (sent["mode"], sent["api_key"], sent["client_reference_id"]) == ("payment", STRIPE_KEY, booking["booking_id"])
    assert (line["quantity"], line["price_data"]["currency"], line["price_data"]["unit_amount"]) == (2, "inr", 25000)
    assert sent["metadata"]["ticket_booking_id"] == booking["booking_id"] == sent["payment_intent_data"]["metadata"]["ticket_booking_id"]
    assert "merch_order_id" not in sent["metadata"] and sent["metadata"]["event_id"] == str(gala.id)
    assert sent["success_url"].endswith("/tickets/return?session_id={CHECKOUT_SESSION_ID}")
    assert sent["expires_at"] - int(datetime.now(timezone.utc).timestamp()) in range(1790, 1801)
    assert (await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": "cs_test_1"})).status_code == 404

    intent = stripe_mode.pay("cs_test_1")
    # The webhook arrives first, signed with the configured secret; the mock's secret is refused.
    refused = await webhook(client, "evt_s_1", "checkout.session.completed", completed(booking, session, intent))
    assert refused.status_code == 400
    accepted = await webhook(client, "evt_s_1", "checkout.session.completed", completed(booking, session, intent), secret=WEBHOOK_SECRET)
    assert accepted.json() == {"status": "processed", "detail": "confirmed"}
    verified = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": "cs_test_1"})
    assert verified.json()["outcome"] == "confirmed" and len(await ticket_entries(db)) == 1

    cancelled = (await client.post(f"{API}/ticket-bookings/{booking['booking_id']}/cancel", headers=auth(world.member))).json()
    assert (cancelled["payment_status"], cancelled["refund_status"]) == ("refunded", "succeeded")
    [refund] = stripe_mode.refunds   # one full refund, against the PaymentIntent
    assert (refund["payment_intent"], refund["amount"], refund["idempotency_key"]) == (intent, 50000, f"{intent}:refund")
    assert (await row(db, booking)).stripe_refund_id == "re_1"
    assert [entry.entry_type for entry in await ticket_entries(db)] == ["income", "refund"]


async def test_hold_length_and_stripe_session_length_differ_on_purpose(client, db, world, auth, category, book, checkout, stripe_mode):
    """Stripe's shortest session is 30 minutes; a hold is 10. So a buyer can pay after the hold
    has gone, which is exactly the late-payment path."""
    cat = await category(capacity=1)
    booking = (await book(world.member, cat)).json()
    session = await checkout(world.member, booking)
    hold = datetime.fromisoformat(session["hold_expires_at"]) - datetime.now(timezone.utc)
    assert hold < timedelta(minutes=10) < timedelta(seconds=fairseat_payments.STRIPE_MIN_SESSION_SECONDS)
