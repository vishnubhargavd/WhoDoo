"""FairSeat on real PostgreSQL: the migrations the app uses, plus the pending FairSeat one.

Requests in the concurrency tests each get their own database connection (the test
settings use no connection pool), so they are separate transactions competing for locks.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError
from app.db.database import SessionLocal
from app.models.fairseat import EventTicket, TicketBooking, TicketBookingCommand, TicketCategory
from app.models.ledger import LedgerEntry
from app.models.merch import PaymentEvent
from app.models.shared import Club, Event, User
from app.schemas.fairseat import QuoteRequest, ReservationRequest
from app.services import fairseat_pricing, fairseat_service

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def _fairseat_tables(fairseat_schema):
    """Runs before `db`, so the FairSeat tables exist when it clears the database."""


def acting(user) -> CurrentUser:
    return CurrentUser(id=user.id, club_id=user.club_id, role=user.role, is_active_member=user.is_active_member)


def key() -> str:
    return f"test-{uuid.uuid4().hex}"


def code(response) -> str:
    return response.json()["error"]["code"]


@pytest.fixture
async def gala(db, world):
    """A published event next week, led by Leo Lead."""
    event = Event(id=uuid.uuid4(), club_id=world.club.id, name="Spring Gala", lead_id=world.lead.id,
                  starts_at=NOW + timedelta(days=7), ends_at=NOW + timedelta(days=7, hours=3),
                  venue="Main Hall", status="published", created_by=world.president.id)
    db.add(event)
    await db.commit()
    return event


@pytest.fixture
def category(client, auth, world, gala):
    """Add a ticket category to the event the way an organizer does: by saving the event's
    whole ticket configuration, with the event's capacity grown to match (strict partition).
    Sales are on and the event's booking window runs from two days ago to its start."""

    async def _category(capacity=10, price=25000, name="General", event=None, **extra):
        event = event or gala
        url = f"{API}/events/{event.id}/tickets"
        current = (await client.get(url, headers=auth(world.president))).json()
        kept = [{key: c[key] for key in ("id", "name", "description", "price_paise", "capacity",
                                         "member_discount_eligible", "is_visible")} for c in current["categories"]]
        new = {"name": name, "price_paise": price, "capacity": capacity, "is_visible": extra.pop("active", True)} | extra
        body = {"sales_enabled": True, "capacity": current["allocated"] + capacity, "max_per_booking": 6,
                "opens_at": (NOW - timedelta(days=2)).isoformat(), "closes_at": event.starts_at.isoformat(),
                "categories": kept + [new]}
        saved = await client.put(url, headers=auth(world.president), json=body)
        assert saved.status_code == 200, saved.text
        listed = (await client.get(f"{API}/ticket-categories?event_id={event.id}", headers=auth(world.president))).json()
        return next(item for item in listed if item["name"] == name)

    return _category


@pytest.fixture
def book(client, auth):
    """Ask for a quote, then reserve with it. Returns the reservation response."""

    async def _book(user, cat, quantity=1, idempotency_key=None):
        selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": quantity}
        quote = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(user), json=selection)
        if quote.status_code != 201:
            return quote
        return await client.post(f"{API}/ticket-bookings", headers=auth(user), json=selection | {
            "idempotency_key": idempotency_key or key(), "accepted_quote_id": quote.json()["id"]})

    return _book


async def occupied(db, cat) -> int:
    counts = await fairseat_service.occupied_seats(db, [uuid.UUID(cat["id"])], datetime.now(timezone.utc))
    return counts[uuid.UUID(cat["id"])]


async def bookings(db, cat) -> list[TicketBooking]:
    rows = await db.scalars(
        select(TicketBooking).where(TicketBooking.ticket_category_id == cat["id"])
        .order_by(TicketBooking.created_at).execution_options(populate_existing=True)
    )
    return list(rows)


async def seats_left(client, auth, user, cat) -> int:
    listed = (await client.get(f"{API}/ticket-categories?event_id={cat['event_id']}", headers=auth(user))).json()
    return next(item["available"] for item in listed if item["id"] == cat["id"])


# --- 1. Normal booking -------------------------------------------------------------------


async def test_normal_booking_holds_seats_and_takes_no_money(client, db, world, auth, category, book):
    cat = await category(capacity=10, price=25000)
    reserved = await book(world.member, cat, quantity=2)
    assert reserved.status_code == 201, reserved.text
    body = reserved.json()
    assert (body["kind"], body["replayed"]) == ("reserved", False)
    assert (body["status"], body["payment_status"]) == ("pending_payment", "pending")
    assert (body["quantity"], body["unit_price_paise"], body["total_price_paise"], body["currency"]) == (2, 25000, 50000, "INR")
    assert body["purchaser_user_id"] == str(world.member.id) and body["tickets"] == []
    hold = datetime.fromisoformat(body["hold_expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=9, seconds=50) < hold <= timedelta(minutes=10)

    assert await occupied(db, cat) == 2 and await seats_left(client, auth, world.member, cat) == 8
    mine = await client.get(f"{API}/ticket-bookings", headers=auth(world.member))
    assert [item["booking_id"] for item in mine.json()] == [body["booking_id"]]
    assert (await client.get(f"{API}/ticket-bookings/{body['booking_id']}", headers=auth(world.member))).status_code == 200

    # This phase stops at the hold: no ledger income, no payment record, no ticket rows.
    for model in (LedgerEntry, PaymentEvent, EventTicket):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


# --- 2 and 6. Last seat and sold out --------------------------------------------------------


async def test_last_seat_then_sold_out(client, db, world, auth, category, book):
    cat = await category(capacity=3)
    assert (await book(world.member, cat, quantity=1)).status_code == 201
    too_many = await book(world.outsider, cat, quantity=3)
    assert too_many.status_code == 409 and code(too_many) == "sold_out"
    assert too_many.json()["error"]["details"] == {"available": 2, "requested": 3, "waitlist": "unavailable"}

    assert (await book(world.outsider, cat, quantity=2)).status_code == 201  # exactly the last two
    assert await occupied(db, cat) == 3 and await seats_left(client, auth, world.member, cat) == 0

    for buyer in (world.volunteer, world.lead):
        refused = await book(buyer, cat)
        assert refused.status_code == 409 and code(refused) == "sold_out"
        assert refused.json()["error"]["details"]["available"] == 0
    assert await occupied(db, cat) == 3 and len(await bookings(db, cat)) == 2


# --- 3. Two (and more) simultaneous transactions for the final seat ---------------------------


async def test_simultaneous_requests_for_the_final_seat(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    buyers = [world.president, world.treasurer, world.lead, world.lead2, world.volunteer, world.member, world.outsider]
    results = await asyncio.gather(*(book(buyer, cat) for buyer in buyers))

    assert sorted(response.status_code for response in results) == [201] + [409] * 6
    assert {code(response) for response in results if response.status_code == 409} == {"sold_out"}
    rows = await bookings(db, cat)
    assert len(rows) == 1 and await occupied(db, cat) == 1
    winner = next(response for response in results if response.status_code == 201)
    assert rows[0].id == uuid.UUID(winner.json()["booking_id"])


async def test_second_transaction_waits_for_the_first_then_loses(db, world, category):
    """The mechanism, step by step: B cannot even count seats until A commits."""
    cat = await category(capacity=1)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}

    async def quote_for(user):
        async with SessionLocal() as session:
            quote = await fairseat_service.create_quote(session, acting(user), QuoteRequest(**selection))
            await session.commit()
            return ReservationRequest(**selection, idempotency_key=key(), accepted_quote_id=quote.id)

    request_a, request_b = await quote_for(world.member), await quote_for(world.outsider)
    async with SessionLocal() as a, SessionLocal() as b:
        first = await fairseat_service.reserve(a, acting(world.member), request_a)   # A holds the locks, uncommitted
        waiting = asyncio.create_task(fairseat_service.reserve(b, acting(world.outsider), request_b))
        done, _ = await asyncio.wait({waiting}, timeout=1.0)
        assert not done, "B answered while A's transaction was still open"
        assert await occupied(db, cat) == 0  # nothing visible to anyone else yet

        await a.commit()
        with pytest.raises(AppError) as lost:
            await asyncio.wait_for(waiting, timeout=5)
        assert (lost.value.status_code, lost.value.code) == (409, "sold_out")
        await b.rollback()
    assert first.status == "pending_payment" and await occupied(db, cat) == 1 and len(await bookings(db, cat)) == 1


async def test_if_the_first_transaction_rolls_back_the_second_gets_the_seat(db, world, category):
    cat = await category(capacity=1)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
    requests = {}
    for user in (world.member, world.outsider):
        async with SessionLocal() as session:
            quote = await fairseat_service.create_quote(session, acting(user), QuoteRequest(**selection))
            await session.commit()
            requests[user.id] = ReservationRequest(**selection, idempotency_key=key(), accepted_quote_id=quote.id)

    async with SessionLocal() as a, SessionLocal() as b:
        await fairseat_service.reserve(a, acting(world.member), requests[world.member.id])
        waiting = asyncio.create_task(fairseat_service.reserve(b, acting(world.outsider), requests[world.outsider.id]))
        await asyncio.sleep(0.3)
        await a.rollback()  # A's request failed after taking the seat: nothing of it may remain
        won = await asyncio.wait_for(waiting, timeout=5)
        await b.commit()
    rows = await bookings(db, cat)
    assert [row.purchaser_user_id for row in rows] == [world.outsider.id] and rows[0].id == won.booking_id
    assert await db.scalar(select(func.count()).select_from(TicketBookingCommand)) == 1


async def test_concurrent_group_bookings_never_exceed_capacity(client, db, world, auth, category, book):
    cat = await category(capacity=5)
    buyers = [world.president, world.treasurer, world.lead, world.lead2, world.volunteer, world.member]
    results = await asyncio.gather(*(book(buyer, cat, quantity=2) for buyer in buyers))
    assert sorted(response.status_code for response in results) == [201, 201, 409, 409, 409, 409]
    assert await occupied(db, cat) == 4 <= cat["capacity"]
    # One seat is left; a request for two is refused, a request for one fits.
    assert code(await book(world.outsider, cat, quantity=2)) == "sold_out"
    assert (await book(world.outsider, cat, quantity=1)).status_code == 201
    assert await occupied(db, cat) == 5


# --- 4 and 5. Idempotency -------------------------------------------------------------------


async def test_replaying_a_request_returns_the_same_booking(client, db, world, auth, category):
    cat = await category(capacity=5)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 2}
    quote = (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)).json()
    request = selection | {"idempotency_key": key(), "accepted_quote_id": quote["id"]}

    first = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request)
    assert first.status_code == 201 and first.json()["replayed"] is False
    for _ in range(3):
        again = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request)
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert again.json() | {"replayed": False} == first.json()
    assert len(await bookings(db, cat)) == 1 and await occupied(db, cat) == 2

    # Five copies of a fresh request sent at once still make one booking.
    request = request | {"idempotency_key": key()}
    burst = await asyncio.gather(*(
        client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request) for _ in range(5)))
    assert sorted(response.status_code for response in burst) == [200, 200, 200, 200, 201]
    assert len({response.json()["booking_id"] for response in burst}) == 1
    assert len(await bookings(db, cat)) == 2 and await occupied(db, cat) == 4

    # The key belongs to the purchaser: someone else using the same string makes their own booking.
    other_quote = (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.outsider), json=selection | {"quantity": 1})).json()
    other = await client.post(f"{API}/ticket-bookings", headers=auth(world.outsider), json=selection | {
        "quantity": 1, "idempotency_key": request["idempotency_key"], "accepted_quote_id": other_quote["id"]})
    assert other.status_code == 201 and other.json()["purchaser_user_id"] == str(world.outsider.id)


async def test_same_key_with_a_different_request_is_refused(client, db, world, auth, category):
    general, vip = await category(capacity=5), await category(capacity=5, name="VIP", price=90000)

    async def quoted(cat, quantity):
        selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": quantity}
        quote = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)
        return selection | {"accepted_quote_id": quote.json()["id"]}

    shared_key = key()
    original = await quoted(general, 1)
    assert (await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=original | {"idempotency_key": shared_key})).status_code == 201

    changed = [await quoted(general, 2), await quoted(vip, 1), await quoted(general, 1)]  # quantity, category, quote
    for request in changed:
        refused = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request | {"idempotency_key": shared_key})
        assert refused.status_code == 409 and code(refused) == "idempotency_conflict"
    assert len(await bookings(db, general)) == 1 and await bookings(db, vip) == []
    assert await occupied(db, general) == 1


# --- 7. Cancellation releases capacity ---------------------------------------------------------


async def test_cancelling_a_hold_puts_the_seat_back_on_sale(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    held = (await book(world.member, cat)).json()
    assert code(await book(world.outsider, cat)) == "sold_out"
    cancel_url = f"{API}/ticket-bookings/{held['booking_id']}/cancel"

    assert (await client.post(cancel_url, headers=auth(world.outsider))).status_code == 404  # not theirs
    assert (await client.post(cancel_url, headers=auth(world.president))).status_code == 404  # nor an officer's
    assert await occupied(db, cat) == 1

    for _ in range(2):  # cancelling twice is the same as cancelling once
        cancelled = await client.post(cancel_url, headers=auth(world.member))
        assert cancelled.status_code == 200
        assert (cancelled.json()["status"], cancelled.json()["hold_expires_at"]) == ("cancelled", None)
    assert await occupied(db, cat) == 0

    assert (await book(world.outsider, cat)).status_code == 201
    assert await occupied(db, cat) == 1
    statuses = sorted(row.status for row in await bookings(db, cat))
    assert statuses == ["cancelled", "pending_payment"]


async def test_free_booking_is_confirmed_and_cancelling_revokes_its_tickets(client, db, world, auth, category, book):
    cat = await category(capacity=2, price=0, name="Free entry")
    free = await book(world.member, cat, quantity=2)
    assert free.status_code == 201
    body = free.json()
    assert (body["status"], body["payment_status"], body["total_price_paise"], body["hold_expires_at"]) == (
        "confirmed", "not_required", 0, None)
    assert [(ticket["seat_number"], ticket["status"]) for ticket in body["tickets"]] == [(1, "pending_issue"), (2, "pending_issue")]
    assert await db.scalar(select(func.count()).select_from(EventTicket).where(EventTicket.credential_digest.isnot(None))) == 0

    # Free tickets still obey capacity, and a replay does not issue more.
    assert code(await book(world.outsider, cat)) == "sold_out"
    assert await occupied(db, cat) == 2

    cancelled = await client.post(f"{API}/ticket-bookings/{body['booking_id']}/cancel", headers=auth(world.member))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert {ticket["status"] for ticket in cancelled.json()["tickets"]} == {"revoked"}
    tickets = list(await db.scalars(select(EventTicket)))
    assert len(tickets) == 2 and all(ticket.revoked_at is not None for ticket in tickets)
    assert await occupied(db, cat) == 0 and (await book(world.outsider, cat, quantity=2)).status_code == 201
    assert await db.scalar(select(func.count()).select_from(LedgerEntry)) == 0


async def test_a_used_or_paid_booking_cannot_be_cancelled_this_way(client, db, world, auth, category, book):
    cat = await category(capacity=4, price=0)
    free = (await book(world.member, cat)).json()
    await db.execute(text("UPDATE event_tickets SET status = 'checked_in', checked_in_at = now() WHERE booking_id = :id"),
                     {"id": free["booking_id"]})
    paid_cat = await category(capacity=4, price=5000, name="Paid")
    paid = (await book(world.member, paid_cat)).json()
    # A later phase will mark bookings paid; here the state is set directly to test the guard.
    await db.execute(text("UPDATE ticket_bookings SET status = 'confirmed', payment_status = 'paid', hold_expires_at = NULL "
                          "WHERE id = :id"), {"id": paid["booking_id"]})
    await db.commit()

    used = await client.post(f"{API}/ticket-bookings/{free['booking_id']}/cancel", headers=auth(world.member))
    assert used.status_code == 409 and code(used) == "ticket_already_used"
    refund = await client.post(f"{API}/ticket-bookings/{paid['booking_id']}/cancel", headers=auth(world.member))
    assert refund.status_code == 409 and code(refund) == "refund_required"
    assert await occupied(db, cat) == 1 and await occupied(db, paid_cat) == 1


# --- 8. Expired holds --------------------------------------------------------------------------


async def test_an_expired_hold_frees_its_seat_without_any_sweeper(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    request_key = key()
    stale = (await book(world.member, cat, idempotency_key=request_key)).json()
    assert code(await book(world.outsider, cat)) == "sold_out"

    await db.execute(text("UPDATE ticket_bookings SET hold_expires_at = now() - interval '1 second' WHERE id = :id"),
                     {"id": stale["booking_id"]})
    await db.commit()
    # Nothing has marked it expired, but it no longer counts and is reported as expired.
    assert await occupied(db, cat) == 0 and await seats_left(client, auth, world.outsider, cat) == 1
    seen = (await client.get(f"{API}/ticket-bookings/{stale['booking_id']}", headers=auth(world.member))).json()
    assert (seen["status"], seen["hold_expires_at"]) == ("expired", None)

    assert (await book(world.outsider, cat)).status_code == 201
    assert await occupied(db, cat) == 1
    # Booking tidied the stale row while it held the lock.
    assert sorted(row.status for row in await bookings(db, cat)) == ["expired", "pending_payment"]
    # The old holder cannot get it back by cancelling or retrying.
    late = await client.post(f"{API}/ticket-bookings/{stale['booking_id']}/cancel", headers=auth(world.member))
    assert late.json()["status"] == "expired" and await occupied(db, cat) == 1
    assert code(await book(world.member, cat)) == "sold_out"


async def test_sweeping_expired_holds_and_concurrent_sweeps(client, db, world, auth, category, book):
    cats = [await category(capacity=3, name=name) for name in ("Tier A", "Tier B", "Tier C")]
    for cat in cats:
        for buyer in (world.member, world.outsider):
            assert (await book(buyer, cat)).status_code == 201
    live = (await book(world.volunteer, cats[0])).json()
    await db.execute(text("UPDATE ticket_bookings SET hold_expires_at = now() - interval '1 minute' WHERE id <> :keep"),
                     {"keep": live["booking_id"]})
    await db.commit()

    async def sweep() -> int:
        async with SessionLocal() as session:
            count = await fairseat_service.expire_holds(session)
            await session.commit()
            return count

    counts = await asyncio.wait_for(asyncio.gather(*(sweep() for _ in range(4))), timeout=10)  # no deadlock
    assert sum(counts) == 6
    statuses = [row.status for cat in cats for row in await bookings(db, cat)]
    assert (statuses.count("expired"), statuses.count("pending_payment")) == (6, 1)
    assert [await occupied(db, cat) for cat in cats] == [1, 0, 0]
    assert await sweep() == 0


async def test_hold_deadline_is_exact(db, world, category):
    cat = await category(capacity=1)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
    start = datetime.now(timezone.utc)

    async def reserve_at(user, moment):
        async with SessionLocal() as session:
            quote = await fairseat_service.create_quote(session, acting(user), QuoteRequest(**selection), now=moment)
            result = await fairseat_service.reserve(session, acting(user), ReservationRequest(
                **selection, idempotency_key=key(), accepted_quote_id=quote.id), now=moment)
            await session.commit()
            return result

    first = await reserve_at(world.member, start)
    assert first.hold_expires_at == start + timedelta(seconds=600)
    with pytest.raises(AppError) as early:  # one millisecond before the deadline the seat is still held
        await reserve_at(world.outsider, first.hold_expires_at - timedelta(milliseconds=1))
    assert early.value.code == "sold_out"
    second = await reserve_at(world.outsider, first.hold_expires_at)  # at the deadline it is free
    assert second.status == "pending_payment"
    counts = await fairseat_service.occupied_seats(db, [uuid.UUID(cat["id"])], first.hold_expires_at)
    assert counts[uuid.UUID(cat["id"])] == 1


# --- 9. Waitlist is not offered ------------------------------------------------------------------


async def test_waitlist_is_explicitly_unavailable(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    assert "waitlist_enabled" not in cat
    await book(world.member, cat)
    full = await book(world.outsider, cat)
    assert full.status_code == 409 and code(full) == "sold_out"
    assert full.json()["error"]["details"]["waitlist"] == "unavailable"
    # No waitlist storage exists, so no entry can be left stranded.
    assert await db.scalar(text("SELECT to_regclass('public.ticket_waitlist_entries')")) is None
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
    asked = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.outsider), json=selection | {"join_waitlist": True})
    assert asked.status_code == 422


# --- 10. Booking window ------------------------------------------------------------------------


async def test_booking_window_boundaries(db, world, gala, category):
    opens, closes = NOW + timedelta(days=1), NOW + timedelta(days=2)
    cat = await category(capacity=50, sales_open_at=opens.isoformat(), sales_close_at=closes.isoformat())
    until_start = await category(capacity=50, name="Door")  # no window of its own: the event's, which ends at the start
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}

    async def attempt(moment, chosen=selection):
        async with SessionLocal() as session:
            try:
                quote = await fairseat_service.create_quote(session, acting(world.member), QuoteRequest(**chosen), now=moment)
                booked = await fairseat_service.reserve(session, acting(world.member), ReservationRequest(
                    **chosen, idempotency_key=key(), accepted_quote_id=quote.id), now=moment)
                await session.commit()
                return booked.status
            except AppError as refused:
                assert refused.status_code == 409
                return refused.code

    tick = timedelta(milliseconds=1)
    assert await attempt(opens - tick) == "booking_not_open"
    assert await attempt(opens) == "pending_payment"            # opening instant is included
    assert await attempt(closes - tick) == "pending_payment"
    assert await attempt(closes) == "booking_closed"            # closing instant is excluded
    assert await attempt(closes + tick) == "booking_closed"

    door = selection | {"ticket_category_id": until_start["id"]}
    assert await attempt(gala.starts_at - tick, door) == "pending_payment"
    assert await attempt(gala.starts_at, door) == "booking_closed"  # never once the event has started

    # A quote taken while sales were open cannot be used after they close.
    async with SessionLocal() as session:
        quote = await fairseat_service.create_quote(session, acting(world.outsider), QuoteRequest(**selection), now=closes - tick)
        await session.commit()
        with pytest.raises(AppError) as late:
            await fairseat_service.reserve(session, acting(world.outsider), ReservationRequest(
                **selection, idempotency_key=key(), accepted_quote_id=quote.id), now=closes)
        assert late.value.code == "booking_closed"


async def test_only_published_events_with_a_start_time_can_be_booked(client, db, world, auth, gala, category, book):
    cat = await category(capacity=5)
    closed_cat = await category(capacity=5, name="Closed", active=False)
    assert code(await book(world.member, closed_cat)) == "category_closed"

    for status in ("draft", "cancelled", "completed"):
        await db.execute(text("UPDATE events SET status = :status WHERE id = :id"), {"status": status, "id": gala.id})
        await db.commit()
        refused = await book(world.member, cat)
        assert refused.status_code == 409 and code(refused) == "event_not_bookable", status

    # An older event marked published without a start time is refused cleanly, not with a 500.
    await db.execute(text("UPDATE events SET status = 'published', starts_at = NULL, ends_at = NULL WHERE id = :id"), {"id": gala.id})
    await db.commit()
    undated = await book(world.member, cat)
    assert undated.status_code == 409 and code(undated) == "event_not_bookable"
    listed = (await client.get(f"{API}/ticket-categories?event_id={gala.id}", headers=auth(world.member))).json()
    assert all(item["on_sale"] is False for item in listed)
    assert await occupied(db, cat) == 0


# --- 11. Club and user authorization ---------------------------------------------------------


@pytest.fixture
async def rival(db):
    club = Club(id=uuid.uuid4(), name="Rival Club")
    db.add(club)
    await db.flush()
    people = SimpleNamespace(
        president=User(id=uuid.uuid4(), club_id=club.id, full_name="Rita Rival", email="rita@rival.example",
                       role="president", is_active_member=True),
        member=User(id=uuid.uuid4(), club_id=club.id, full_name="Ravi Rival", email="ravi@rival.example",
                    role="member", is_active_member=True),
    )
    db.add_all(vars(people).values())
    await db.commit()
    return people


async def test_other_clubs_and_signed_out_callers_get_nothing(client, db, world, auth, gala, category, book, rival):
    cat = await category(capacity=5)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
    mine = (await book(world.member, cat)).json()

    for outsider in (rival.member, rival.president):
        assert (await client.get(f"{API}/ticket-categories?event_id={gala.id}", headers=auth(outsider))).status_code == 404
        assert (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(outsider), json=selection)).status_code == 404
        assert (await client.get(f"{API}/ticket-bookings/{mine['booking_id']}", headers=auth(outsider))).status_code == 404
        assert (await client.post(f"{API}/ticket-bookings/{mine['booking_id']}/cancel", headers=auth(outsider))).status_code == 404
        assert (await client.get(f"{API}/ticket-bookings", headers=auth(outsider))).json() == []
    # Even holding a real quote id from the right club, another club's user cannot reserve.
    quote = (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)).json()
    stolen = await client.post(f"{API}/ticket-bookings", headers=auth(rival.member), json=selection | {
        "idempotency_key": key(), "accepted_quote_id": quote["id"]})
    assert stolen.status_code == 404
    # Rival president cannot configure this club's event either.
    setup = {"event_id": str(gala.id), "name": "Sneaky", "base_price_paise": 1, "capacity": 1,
             "sales_open_at": NOW.isoformat(), "sales_close_at": (NOW + timedelta(days=1)).isoformat()}
    assert (await client.post(f"{API}/ticket-categories", headers=auth(rival.president), json=setup)).status_code == 404
    assert (await client.patch(f"{API}/ticket-categories/{cat['id']}", headers=auth(rival.president), json={"capacity": 1})).status_code == 404

    for method, path in (("get", f"/ticket-categories?event_id={gala.id}"), ("get", "/ticket-bookings"),
                         ("post", "/ticket-bookings"), ("post", "/ticket-bookings/quotes"), ("post", "/ticket-categories")):
        assert (await getattr(client, method)(API + path)).status_code == 401
    assert await occupied(db, cat) == 1


async def test_identity_price_and_status_cannot_come_from_the_request(client, db, world, auth, category):
    cat = await category(capacity=5, price=25000)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
    quote = (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)).json()
    request = selection | {"idempotency_key": key(), "accepted_quote_id": quote["id"]}

    for extra in ({"purchaser_user_id": str(world.president.id)}, {"club_id": str(world.club.id)}, {"role": "president"},
                  {"total_price_paise": 1}, {"unit_price_paise": 1}, {"payment_status": "paid"}, {"status": "confirmed"},
                  {"is_active_member": True}):
        refused = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request | extra)
        assert refused.status_code == 422, extra
    for bad in ({"quantity": 0}, {"quantity": -1}, {"quantity": "2"}, {"quantity": 1.5}, {"idempotency_key": "short"},
                {"idempotency_key": "has spaces in it !!"}):
        assert (await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request | bad)).status_code == 422
    assert await bookings(db, cat) == []

    # Someone else's quote is not accepted, and a made-up one does not exist.
    other = await client.post(f"{API}/ticket-bookings", headers=auth(world.outsider), json=request)
    assert other.status_code == 409 and code(other) == "invalid_quote"
    made_up = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request | {"accepted_quote_id": str(uuid.uuid4())})
    assert code(made_up) == "invalid_quote"
    # A quote for one ticket cannot be stretched to two.
    assert code(await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request | {"quantity": 2})) == "invalid_quote"

    booked = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=request)
    assert booked.status_code == 201 and booked.json()["total_price_paise"] == 25000
    assert booked.json()["purchaser_user_id"] == str(world.member.id)


async def test_who_can_set_up_and_see_tickets(client, db, world, auth, gala, category, book):
    """The per-category routes are a second door to the one category writer."""
    setup = {"event_id": str(gala.id), "name": "Balcony", "base_price_paise": 40000, "capacity": 20}
    # No ticket settings yet: the event's capacity and booking window come first.
    early = await client.post(f"{API}/ticket-categories", headers=auth(world.president), json=setup)
    assert early.status_code == 409 and code(early) == "ticketing_not_configured"

    general = await category(capacity=30, name="General")          # settings now exist, sales on, 30 of 30 allocated
    for person in (world.member, world.volunteer, world.treasurer, world.lead2):  # lead2 leads a different event
        assert (await client.post(f"{API}/ticket-categories", headers=auth(person), json=setup)).status_code == 403

    # While sales are on the categories must add up to the event's capacity exactly, by either door.
    unbalanced = await client.post(f"{API}/ticket-categories", headers=auth(world.lead), json=setup)
    assert unbalanced.status_code == 400 and code(unbalanced) == "over_allocated"
    config_url = f"{API}/events/{gala.id}/tickets"
    current = (await client.get(config_url, headers=auth(world.lead))).json()
    paused = {key: current[key] for key in ("capacity", "opens_at", "closes_at", "max_per_booking")} | {
        "sales_enabled": False, "capacity": 50,
        "categories": [{key: c[key] for key in ("id", "name", "price_paise", "capacity")} for c in current["categories"]]}
    assert (await client.put(config_url, headers=auth(world.lead), json=paused)).status_code == 200

    by_lead = await client.post(f"{API}/ticket-categories", headers=auth(world.lead), json=setup)
    assert by_lead.status_code == 201, by_lead.text
    balcony = by_lead.json()
    assert (balcony["capacity"], balcony["sort_order"], balcony["member_discount_eligible"], balcony["on_sale"]) == (20, 1, True, False)
    duplicate = await client.post(f"{API}/ticket-categories", headers=auth(world.president), json=setup | {"name": " balcony "})
    assert duplicate.status_code == 409 and code(duplicate) == "duplicate_category"
    too_big = await client.post(f"{API}/ticket-categories", headers=auth(world.president), json=setup | {"name": "Extra", "capacity": 1})
    assert too_big.status_code == 400 and code(too_big) == "over_allocated"
    outside = await client.post(f"{API}/ticket-categories", headers=auth(world.president), json=setup | {
        "name": "Late", "capacity": 1, "sales_open_at": NOW.isoformat(),
        "sales_close_at": (gala.starts_at + timedelta(minutes=1)).isoformat()})
    assert outside.status_code == 400 and code(outside) in ("over_allocated", "category_window_outside_event")
    naive = await client.post(f"{API}/ticket-categories", headers=auth(world.president), json=setup | {
        "name": "Naive", "sales_open_at": "2026-10-01T10:00:00", "sales_close_at": "2026-10-02T10:00:00"})
    assert naive.status_code == 422
    assert (await client.put(config_url, headers=auth(world.lead), json=paused | {"sales_enabled": True, "categories": [
        {"id": general["id"], "name": "General", "price_paise": 25000, "capacity": 30},
        {"id": balcony["id"], "name": "Balcony", "price_paise": 40000, "capacity": 20}]})).status_code == 200

    update_url = f"{API}/ticket-categories/{balcony['id']}"
    assert (await client.patch(update_url, headers=auth(world.member), json={"capacity": 500})).status_code == 403
    assert (await client.patch(update_url, headers=auth(world.lead2), json={"capacity": 500})).status_code == 403
    # A category window can narrow the event's, never widen it.
    wider = await client.patch(update_url, headers=auth(world.lead), json={
        "sales_open_at": (NOW - timedelta(days=30)).isoformat(), "sales_close_at": gala.starts_at.isoformat()})
    assert wider.status_code == 400 and code(wider) == "category_window_outside_event"
    narrowed = await client.patch(update_url, headers=auth(world.lead), json={
        "sales_open_at": (NOW - timedelta(hours=1)).isoformat(), "sales_close_at": (gala.starts_at - timedelta(hours=2)).isoformat()})
    assert narrowed.status_code == 200 and narrowed.json()["on_sale"] is True

    held = (await book(world.member, balcony, quantity=3)).json()
    # The purchaser, the event's lead and the president can read the booking; others cannot.
    for person, expected in ((world.member, 200), (world.lead, 200), (world.president, 200),
                             (world.lead2, 404), (world.treasurer, 404), (world.outsider, 404)):
        seen = await client.get(f"{API}/ticket-bookings/{held['booking_id']}", headers=auth(person))
        assert seen.status_code == expected, person.full_name

    # A category cannot shrink below the seats taken in it, by either door.
    by_config = await client.put(config_url, headers=auth(world.lead), json=paused | {"sales_enabled": True, "capacity": 32, "categories": [
        {"id": general["id"], "name": "General", "price_paise": 25000, "capacity": 30},
        {"id": balcony["id"], "name": "Balcony", "price_paise": 40000, "capacity": 2}]})
    assert by_config.status_code == 409 and code(by_config) in ("category_locked", "capacity_below_occupied")
    # Shrinking alone breaks the partition while sales are on; with the event's capacity it is fine.
    alone = await client.patch(update_url, headers=auth(world.lead), json={"capacity": 3})
    assert alone.status_code == 400 and code(alone) == "unallocated_seats"
    exact = await client.put(config_url, headers=auth(world.lead), json=paused | {"sales_enabled": True, "capacity": 33, "categories": [
        {"id": general["id"], "name": "General", "price_paise": 25000, "capacity": 30},
        {"id": balcony["id"], "name": "Balcony", "price_paise": 40000, "capacity": 3}]})
    assert exact.status_code == 200, exact.text
    assert code(await book(world.outsider, balcony)) == "sold_out"
    assert await occupied(db, balcony) == 3

    await db.execute(text("UPDATE events SET status = 'cancelled' WHERE id = :id"), {"id": gala.id})
    await db.commit()
    closed = await client.patch(update_url, headers=auth(world.president), json={"name": "Renamed"})
    assert closed.status_code == 400 and code(closed) == "event_closed"


# --- Purchaser limit -------------------------------------------------------------------------


async def test_six_tickets_per_purchaser_per_event(client, db, world, auth, gala, category, book):
    general, vip = await category(capacity=50), await category(capacity=50, name="VIP", price=90000)
    seven = await book(world.member, general, quantity=7)
    assert seven.status_code == 400 and code(seven) == "invalid_quantity"

    assert (await book(world.member, general, quantity=4)).status_code == 201
    over = await book(world.member, vip, quantity=3)  # the limit spans every category of the event
    assert over.status_code == 409 and code(over) == "purchaser_limit"
    assert over.json()["error"]["details"] == {"limit": 6, "held": 4, "requested": 3}
    second = await book(world.member, vip, quantity=2)
    assert second.status_code == 201
    assert code(await book(world.member, general)) == "purchaser_limit"
    assert (await book(world.outsider, general, quantity=6)).status_code == 201  # other people are unaffected

    # Giving seats back makes room again.
    await client.post(f"{API}/ticket-bookings/{second.json()['booking_id']}/cancel", headers=auth(world.member))
    assert (await book(world.member, general, quantity=2)).status_code == 201


async def test_purchaser_limit_holds_under_concurrent_requests(client, db, world, auth, category, book):
    cat = await category(capacity=50)
    results = await asyncio.gather(*(book(world.member, cat, quantity=4) for _ in range(4)))
    assert sorted(response.status_code for response in results) == [201, 409, 409, 409]
    assert {code(response) for response in results if response.status_code == 409} == {"purchaser_limit"}
    assert await occupied(db, cat) == 4


# --- Quotes and pricing ----------------------------------------------------------------------


async def test_quote_expires_and_goes_stale_when_the_price_changes(client, db, world, auth, category):
    cat = await category(capacity=5, price=25000)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 2}

    async def quote():
        response = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)
        assert response.status_code == 201
        return response.json()

    first = await quote()
    assert (first["unit_price_paise"], first["total_price_paise"], first["discount_reason"]) == (25000, 50000, "missing_membership")
    assert (first["calculator_version"], first["category_pricing_version"]) == (fairseat_pricing.CALCULATOR_VERSION, 1)
    life = datetime.fromisoformat(first["expires_at"]) - datetime.fromisoformat(first["issued_at"])
    assert life == timedelta(seconds=300)

    repriced = await client.patch(f"{API}/ticket-categories/{cat['id']}", headers=auth(world.president), json={"base_price_paise": 30000})
    assert repriced.json()["pricing_version"] == 2
    stale = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=selection | {
        "idempotency_key": key(), "accepted_quote_id": first["id"]})
    assert stale.status_code == 409 and code(stale) == "quote_stale"

    second = await quote()
    assert second["total_price_paise"] == 60000
    await db.execute(text("UPDATE ticket_quotes SET issued_at = now() - interval '10 minutes', "
                          "expires_at = now() - interval '5 minutes' WHERE id = :id"), {"id": second["id"]})
    await db.commit()
    expired = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=selection | {
        "idempotency_key": key(), "accepted_quote_id": second["id"]})
    assert expired.status_code == 409 and code(expired) == "quote_expired"
    assert await bookings(db, cat) == []

    # A quote can be issued while nothing can be booked only if the event is on sale.
    closed = await category(capacity=5, name="Closed", active=False)
    refused = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection | {"ticket_category_id": closed["id"]})
    assert refused.status_code == 409 and code(refused) == "category_closed"


async def test_active_member_flag_alone_gives_no_discount(client, world, auth, category):
    cat = await category(capacity=5, price=79900)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
    prices = {}
    for person in (world.member, world.outsider):  # an active member and a non-member
        quote = (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(person), json=selection)).json()
        prices[person.is_active_member] = (quote["unit_price_paise"], quote["discount_applied"], quote["discount_reason"])
    assert prices == {True: (79900, False, "missing_membership"), False: (79900, False, "missing_membership")}


class FakeMemberships:
    """Stands in for MemberCore's records, to exercise the discount path end to end."""

    def __init__(self):
        self.records: dict[uuid.UUID, fairseat_pricing.Membership] = {}

    async def membership_for(self, session, *, user_id, club_id, now):
        return self.records.get(user_id)


@pytest.fixture
def memberships():
    source = FakeMemberships()
    original = fairseat_pricing.get_membership_source()
    fairseat_pricing.set_membership_source(source)
    yield source
    fairseat_pricing.set_membership_source(original)


async def test_member_discount_flows_from_a_server_source_and_is_rechecked(client, db, world, auth, category, memberships):
    cat = await category(capacity=10, price=79900)
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 3}
    start = fairseat_pricing.to_ms(NOW - timedelta(days=30))
    record = fairseat_pricing.Membership("active", str(world.club.id), start, start + 90 * 86_400_000, 1500)
    memberships.records[world.member.id] = record

    async def quote(person):
        return (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(person), json=selection)).json()

    discounted = await quote(world.member)
    assert (discounted["unit_price_paise"], discounted["total_price_paise"]) == (67915, 203745)  # 15% off 799.00
    assert (discounted["member_eligible"], discounted["applied_discount_bps"], discounted["discount_reason"]) == (True, 1500, "discount_applied")
    assert (await quote(world.outsider))["unit_price_paise"] == 79900

    # The membership ends between the quote and the booking: the old price is not honoured.
    lapsed = await quote(world.member)
    memberships.records[world.member.id] = fairseat_pricing.Membership("cancelled", str(world.club.id), start, start + 90 * 86_400_000, 1500)
    refused = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=selection | {
        "idempotency_key": key(), "accepted_quote_id": lapsed["id"]})
    assert refused.status_code == 409 and code(refused) == "quote_stale"

    memberships.records[world.member.id] = record
    booked = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json=selection | {
        "idempotency_key": key(), "accepted_quote_id": discounted["id"]})
    assert booked.status_code == 201
    row = (await bookings(db, cat))[0]
    assert (row.unit_price_paise, row.total_price_paise, row.applied_discount_bps, row.member_eligible) == (67915, 203745, 1500, True)

    # A full discount makes a free, immediately confirmed booking.
    memberships.records[world.volunteer.id] = fairseat_pricing.Membership("active", str(world.club.id), start, start + 90 * 86_400_000, 10000)
    free_quote = await quote(world.volunteer)
    free = await client.post(f"{API}/ticket-bookings", headers=auth(world.volunteer), json=selection | {
        "idempotency_key": key(), "accepted_quote_id": free_quote["id"]})
    assert (free.json()["status"], free.json()["payment_status"], free.json()["total_price_paise"]) == ("confirmed", "not_required", 0)


# --- Event cancellation hook, constraints, switch --------------------------------------------


async def test_release_bookings_for_event_is_safe_to_call_on_cancellation(client, db, world, auth, gala, category, book):
    paid_cat, free_cat = await category(capacity=5), await category(capacity=5, price=0, name="Free")
    held = (await book(world.member, paid_cat, quantity=2)).json()
    await book(world.outsider, free_cat, quantity=2)

    async with SessionLocal() as session:
        assert await fairseat_service.release_bookings_for_event(session, gala.id) == 2
        await session.commit()
    assert await occupied(db, paid_cat) == 0 and await occupied(db, free_cat) == 0
    assert {ticket.status for ticket in await db.scalars(select(EventTicket))} == {"revoked"}
    assert {row.status for row in await bookings(db, paid_cat)} == {"cancelled"}

    # With a paid booking present it refuses and changes nothing: that needs the refund workflow.
    again = (await book(world.member, paid_cat)).json()
    kept = (await book(world.volunteer, paid_cat)).json()
    await db.execute(text("UPDATE ticket_bookings SET status = 'confirmed', payment_status = 'paid', hold_expires_at = NULL "
                          "WHERE id = :id"), {"id": again["booking_id"]})
    await db.commit()
    async with SessionLocal() as session:
        with pytest.raises(AppError) as blocked:
            await fairseat_service.release_bookings_for_event(session, gala.id)
        assert (blocked.value.status_code, blocked.value.code) == (409, "paid_bookings_exist")
    assert await occupied(db, paid_cat) == 2
    assert (await client.get(f"{API}/ticket-bookings/{kept['booking_id']}", headers=auth(world.volunteer))).json()["status"] == "pending_payment"
    assert held["booking_id"] != again["booking_id"]


async def test_database_refuses_inconsistent_booking_states(db, world, category, book):
    cat = await category(capacity=5)
    booking = (await book(world.member, cat)).json()
    impossible = [
        "status = 'confirmed', payment_status = 'pending', hold_expires_at = NULL",       # confirmed but unpaid
        "status = 'confirmed', payment_status = 'paid'",                                   # confirmed yet still on a hold
        "status = 'pending_payment', payment_status = 'paid'",                             # paid yet still waiting
        "status = 'pending_payment', hold_expires_at = NULL",                              # a hold with no deadline
        "status = 'confirmed', payment_status = 'not_required', hold_expires_at = NULL",   # "free" with a price
        "status = 'expired', payment_status = 'refunded', hold_expires_at = NULL",         # refund of nothing
        "total_price_paise = 1",                                                           # total is not unit x quantity
    ]
    for change in impossible:
        with pytest.raises(IntegrityError):
            await db.execute(text(f"UPDATE ticket_bookings SET {change} WHERE id = :id"), {"id": booking["booking_id"]})
        await db.rollback()
    allowed = "status = 'pending_payment', payment_status = 'failed'"  # a declined card keeps the hold
    await db.execute(text(f"UPDATE ticket_bookings SET {allowed} WHERE id = :id"), {"id": booking["booking_id"]})
    await db.commit()
    assert await occupied(db, cat) == 1


async def test_booking_is_off_unless_switched_on(client, world, auth, gala, monkeypatch):
    monkeypatch.setattr(get_settings(), "fairseat_enabled", False)
    for method, path in (("get", f"/ticket-categories?event_id={gala.id}"), ("get", "/ticket-bookings"),
                         ("post", "/ticket-bookings"), ("post", "/ticket-bookings/quotes")):
        response = await getattr(client, method)(API + path, headers=auth(world.president))
        assert response.status_code == 404 and code(response) == "fairseat_disabled"


def test_demo_sign_in_cannot_be_combined_with_booking_on_a_shared_database():
    remote = "postgresql+asyncpg://user:secret@db.example.supabase.co:5432/postgres"
    with pytest.raises(RuntimeError, match="FAIRSEAT_ENABLED"):
        Settings(database_url=remote, demo_auth=True, fairseat_enabled=True, jwt_secret="x" * 40).check()
    Settings(database_url=remote, demo_auth=True, fairseat_enabled=False, jwt_secret="x" * 40).check()
    Settings(database_url=remote, demo_auth=False, fairseat_enabled=True, jwt_secret="x" * 40).check()
    Settings(database_url="postgresql+asyncpg://localhost/whodoo", demo_auth=True, fairseat_enabled=True).check()
    assert Settings().fairseat_enabled is True  # set by the test environment; the shipped default is off
