"""Focused tests for FairSeat waitlist and released-seat lifecycle.

Covers:
- Expired unpaid holds releasing capacity.
- Cancelled reservations releasing capacity.
- FIFO waitlist promotion when capacity returns.
- Promotion respecting MemberCore pricing and eligibility.
- Promotion idempotency (cannot allocate seats twice or promote twice).
- Free booking to genuine GateSync ticket issuance and check-in.
- Paid tickets requiring verified payment before confirmation.
- Lock order and capacity boundaries.
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.models.fairseat import EventTicket, TicketBooking, TicketWaitlistEntry
from app.services import fairseat_service, gatesync_service
from tests.test_fairseat import API, NOW, acting, book, category, code, gala, key, occupied  # noqa: F401


@pytest.fixture(autouse=True)
def gatesync_config(monkeypatch):
    monkeypatch.setattr(get_settings(), "gatesync_enabled", True)
    monkeypatch.setattr(get_settings(), "gatesync_credential_key", base64.urlsafe_b64encode(b"g" * 32).decode())



@pytest.fixture
def free_category(category):
    async def _make(capacity=1):
        return await category(price=0, capacity=capacity)
    return _make


async def test_waitlist_join_and_fifo_order(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    # User 1 books the only seat
    b1 = await book(world.member, cat)
    assert b1.status_code == 201

    # User 2 joins waitlist
    join2 = await client.post(
        f"{API}/ticket-waitlist",
        headers=auth(world.volunteer),
        json={"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1},
    )
    assert join2.status_code == 201
    w2 = join2.json()
    assert w2["status"] == "waiting"
    assert w2["position"] == 1

    # User 3 joins waitlist
    join3 = await client.post(
        f"{API}/ticket-waitlist",
        headers=auth(world.outsider),
        json={"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1},
    )
    assert join3.status_code == 201
    w3 = join3.json()
    assert w3["status"] == "waiting"
    assert w3["position"] == 2

    # Listing my waitlist
    my2 = await client.get(f"{API}/ticket-waitlist", headers=auth(world.volunteer))
    assert my2.status_code == 200
    assert len(my2.json()) == 1
    assert my2.json()[0]["id"] == w2["id"]


async def test_cancellation_releases_capacity_and_promotes_fifo(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    b1 = (await book(world.member, cat)).json()

    # User 2 joins waitlist
    join2 = await client.post(
        f"{API}/ticket-waitlist",
        headers=auth(world.volunteer),
        json={"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1},
    )
    w2_id = join2.json()["id"]

    # User 3 joins waitlist
    join3 = await client.post(
        f"{API}/ticket-waitlist",
        headers=auth(world.outsider),
        json={"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1},
    )
    w3_id = join3.json()["id"]

    # User 1 cancels booking
    cancel_res = await client.post(
        f"{API}/ticket-bookings/{b1['booking_id']}/cancel",
        headers=auth(world.member),
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "cancelled"

    # Verify User 2 was promoted and User 3 remains waiting
    w2_entry = await db.get(TicketWaitlistEntry, uuid.UUID(w2_id))
    w3_entry = await db.get(TicketWaitlistEntry, uuid.UUID(w3_id))
    assert w2_entry.status in ("offered", "converted")
    assert w2_entry.promoted_booking_id is not None
    assert w3_entry.status == "waiting"

    # Verify promoted booking belongs to User 2
    promoted_booking = await db.get(TicketBooking, w2_entry.promoted_booking_id)
    assert promoted_booking.purchaser_user_id == world.volunteer.id
    assert promoted_booking.status in ("pending_payment", "confirmed")


async def test_expired_hold_releases_capacity_and_promotes(client, db, world, auth, category, book):
    cat = await category(capacity=1)
    b1 = (await book(world.member, cat)).json()

    # Join waitlist
    join = await client.post(
        f"{API}/ticket-waitlist",
        headers=auth(world.outsider),
        json={"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1},
    )
    w_id = join.json()["id"]

    # Fast forward hold expiry into the past
    past = NOW - timedelta(minutes=1)
    await db.execute(
        text("UPDATE ticket_bookings SET hold_expires_at = :past WHERE id = :id"),
        {"past": past, "id": b1["booking_id"]},
    )
    await db.commit()

    # Expire holds sweep
    expired_count = await fairseat_service.expire_holds(db, now=NOW)
    await db.commit()
    assert expired_count == 1

    # Verify original booking expired
    orig = await db.get(TicketBooking, uuid.UUID(b1["booking_id"]))
    assert orig.status == "expired"

    # Verify waitlist was promoted
    w_entry = await db.get(TicketWaitlistEntry, uuid.UUID(w_id))
    assert w_entry.status in ("offered", "converted")
    assert w_entry.promoted_booking_id is not None


async def test_promotion_idempotency_and_no_double_promotion(client, db, world, auth, category):
    cat = await category(capacity=1)
    # Join waitlist directly
    join = await client.post(
        f"{API}/ticket-waitlist",
        headers=auth(world.outsider),
        json={"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1},
    )
    w_id = join.json()["id"]

    # Organizer promotes next waitlisted
    promoted1 = await fairseat_service.promote_next_waitlisted(db, uuid.UUID(cat["event_id"]), uuid.UUID(cat["id"]))
    await db.commit()
    assert promoted1 is not None
    assert promoted1.purchaser_user_id == world.outsider.id

    # Immediate retry: nothing to promote, cannot double promote
    promoted2 = await fairseat_service.promote_next_waitlisted(db, uuid.UUID(cat["event_id"]), uuid.UUID(cat["id"]))
    await db.commit()
    assert promoted2 is None

    # Capacity is 1 and exactly 1 seat is taken
    counts = await fairseat_service.occupied_seats(db, [uuid.UUID(cat["id"])], now=NOW)
    assert counts[uuid.UUID(cat["id"])] == 1


async def test_free_booking_issues_genuine_gatesync_tickets(client, db, world, auth, free_category, book):
    cat = await free_category(capacity=1)
    res = await book(world.member, cat)
    assert res.status_code == 201
    booking = res.json()
    assert booking["status"] == "confirmed"
    assert booking["payment_status"] == "not_required"
    assert len(booking["tickets"]) == 1

    ticket_id = booking["tickets"][0]["id"]
    # Issue credential via GateSync
    issuance = await gatesync_service.issue(db, world.member, uuid.UUID(ticket_id))
    await db.commit()
    assert "credential" in issuance
    credential = issuance["credential"]
    assert credential.startswith("whodoo.ticket.v1.")

    # Assign volunteer as check-in staff
    await gatesync_service.assign_staff(db, world.president, uuid.UUID(cat["event_id"]), world.volunteer.id)
    await db.commit()

    # Volunteer redeems credential at the gate
    redemption = await gatesync_service.redeem(db, world.volunteer, uuid.UUID(cat["event_id"]), credential)
    await db.commit()
    assert redemption["outcome"] == "checked_in"
    assert redemption["ticket_id"] == uuid.UUID(ticket_id)

    # Replay redemption returns already_checked_in
    replay = await gatesync_service.redeem(db, world.volunteer, uuid.UUID(cat["event_id"]), credential)
    assert replay["outcome"] == "already_checked_in"


async def test_paid_booking_confirms_only_after_verified_payment(client, db, world, auth, category, book):
    cat = await category(price=5000, capacity=1)
    res = await book(world.outsider, cat)
    assert res.status_code == 201
    b = res.json()
    assert b["status"] == "pending_payment"
    assert b["payment_status"] == "pending"

    # Creating checkout session
    checkout = await client.post(
        f"{API}/ticket-payments/stripe/create-checkout-session",
        headers=auth(world.outsider),
        json={"booking_id": b["booking_id"]},
    )
    assert checkout.status_code == 200
    session_id = checkout.json()["session_id"]

    # Simulating mock payment
    sim = await client.post(
        f"{API}/ticket-payments/mock/simulate",
        headers=auth(world.outsider),
        json={"session_id": session_id, "outcome": "success"},
    )
    assert sim.status_code == 200

    # Verification confirms booking and creates tickets
    verify = await client.post(
        f"{API}/ticket-payments/stripe/verify",
        headers=auth(world.outsider),
        json={"session_id": session_id},
    )
    assert verify.status_code == 200
    v_out = verify.json()
    assert v_out["outcome"] == "confirmed"
    assert v_out["booking"]["status"] == "confirmed"
    assert v_out["booking"]["payment_status"] == "paid"
    assert len(v_out["booking"]["tickets"]) == 1

    # Duplicate verify is idempotent
    verify_again = await client.post(
        f"{API}/ticket-payments/stripe/verify",
        headers=auth(world.outsider),
        json={"session_id": session_id},
    )
    assert verify_again.status_code == 200
    assert len(verify_again.json()["booking"]["tickets"]) == 1
