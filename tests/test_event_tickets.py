"""FairSeat event side (Member 2): ticket settings, discovery and lifecycle protection.

Joined to the reservation service: seat counts are real (confirmed seats plus unexpired
holds, from ticket_inventory), and bookings in these tests are made through the booking
API. Two tests still state a seat count by hand, to exercise the guard logic alone."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.models.ledger import AuditLog
from app.models.shared import Club, Event, User
from app.models.fairseat import TicketCategory as EventTicketCategory
from app.models.tickets import EventTicketSettings
from app.services import ticket_inventory, ticket_rules
from app.services.ticket_rules import EventTimes

API = "/api/v1/events"
DISCOVERY = "/api/v1/discovery/events"
BOOKINGS = "/api/v1/ticket-bookings"


async def book(client, auth, user, event_id, category_id, quantity=1):
    """Reserve seats through the reservation service: a server quote, then the booking."""
    selection = {"event_id": event_id, "ticket_category_id": category_id, "quantity": quantity}
    quote = await client.post(f"{BOOKINGS}/quotes", headers=auth(user), json=selection)
    assert quote.status_code == 201, quote.text
    return await client.post(BOOKINGS, headers=auth(user), json=selection | {
        "idempotency_key": f"test-{uuid.uuid4().hex}", "accepted_quote_id": quote.json()["id"]})


def at(days: int, hour: int = 18) -> str:
    """`days` from now at `hour`:00 IST, without an offset (as a datetime-local input sends it)."""
    day = (datetime.now(timezone.utc) + timedelta(days=days)).date()
    return f"{day.isoformat()}T{hour:02d}:00"


def config(**changes) -> dict:
    body = {
        "sales_enabled": True, "capacity": 100, "opens_at": at(1), "closes_at": at(19), "max_per_booking": 6,
        "categories": [
            {"name": "General", "price_paise": 20000, "capacity": 80},
            {"name": "Front row", "price_paise": 50000, "capacity": 20, "member_discount_eligible": False},
        ],
    }
    body.update(changes)
    return body


async def new_event(client, auth, user, publish=False, **changes) -> str:
    body = {"name": "Spring Gala", "description": "Our biggest night of the year.", "starts_at": at(20),
            "ends_at": at(20, 22), "venue": "Main Hall"}
    body.update(changes)
    response = await client.post(API, headers=auth(user), json=body)
    assert response.status_code == 201, response.text
    event_id = response.json()["id"]
    if publish:
        await set_status(client, auth, user, event_id, "published", expect=200)
    return event_id


async def set_status(client, auth, user, event_id, status, expect=None):
    response = await client.post(f"{API}/{event_id}/status", headers=auth(user), json={"status": status})
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


async def put(client, auth, user, event_id, body):
    return await client.put(f"{API}/{event_id}/tickets", headers=auth(user), json=body)


async def selling(client, auth, world) -> tuple[str, dict]:
    """A published event whose sales opened yesterday: bookings may exist, so it is locked."""
    event_id = await new_event(client, auth, world.lead)
    saved = await put(client, auth, world.lead, event_id, config(opens_at=at(-1)))
    assert saved.status_code == 200, saved.text
    await set_status(client, auth, world.lead, event_id, "published", expect=200)
    current = (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()
    assert current["locked"] and current["sales_activated_at"]
    return event_id, current


def resend(current: dict, **changes) -> dict:
    """The current configuration as a PUT body, with changes."""
    body = {k: current[k] for k in ("sales_enabled", "capacity", "opens_at", "closes_at", "max_per_booking")}
    body["categories"] = [
        {k: c[k] for k in ("id", "name", "price_paise", "capacity", "member_discount_eligible", "is_visible")}
        for c in current["categories"]
    ]
    body.update(changes)
    return body


# 1 -----------------------------------------------------------------------------------------


async def test_create_ticket_configuration(client, db, world, auth):
    event_id = await new_event(client, auth, world.lead)
    before = await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))
    assert before.status_code == 200 and before.json()["configured"] is False
    assert before.json()["sale_state"] == "not_configured"

    response = await put(client, auth, world.lead, event_id, config())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] and body["sales_enabled"] and body["capacity"] == body["allocated"] == 100
    assert [c["name"] for c in body["categories"]] == ["General", "Front row"]
    assert [c["sort_order"] for c in body["categories"]] == [0, 1]
    assert body["categories"][1]["member_discount_eligible"] is False
    assert body["sale_state"] == "unpublished" and not body["locked"]
    assert body["seats_committed"] == 0  # from the reservation service: nothing booked yet
    assert "not_published" in [w["code"] for w in body["warnings"]]

    audit = await db.scalar(select(AuditLog).where(AuditLog.action == "ticket_config_updated"))
    assert audit.entity_id == uuid.UUID(event_id) and audit.details["after"]["capacity"] == 100


# 2 -----------------------------------------------------------------------------------------


async def test_update_capacity_keeps_category_ids(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    first = (await put(client, auth, world.lead, event_id, config())).json()
    general, front = first["categories"]

    body = resend(first, capacity=150)
    body["categories"][0]["capacity"] = 130
    body["categories"][1]["price_paise"] = 45000
    second = await put(client, auth, world.lead, event_id, body)
    assert second.status_code == 200, second.text
    out = second.json()
    assert out["capacity"] == out["allocated"] == 150
    assert [c["id"] for c in out["categories"]] == [general["id"], front["id"]]  # ids stay stable
    assert out["categories"][1]["price_paise"] == 45000

    # Removing a category before sales start is fine; a new one gets a new id.
    body = resend(out, categories=[{"id": general["id"], "name": "General", "price_paise": 20000, "capacity": 140},
                                   {"name": "Student", "price_paise": 10000, "capacity": 10}])
    third = (await put(client, auth, world.lead, event_id, body)).json()
    assert [c["name"] for c in third["categories"]] == ["General", "Student"]
    assert third["categories"][0]["id"] == general["id"] and third["categories"][1]["id"] != front["id"]


# 3 -----------------------------------------------------------------------------------------


async def test_invalid_capacity_rejected(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    for capacity in (-1, 0, 100_001, "100", 99.5, True, None):
        response = await put(client, auth, world.lead, event_id, config(capacity=capacity))
        assert response.status_code == 422, capacity
        assert response.json()["error"]["code"] == "validation_error"
    bad_category = config()
    bad_category["categories"][0]["capacity"] = -5
    assert (await put(client, auth, world.lead, event_id, bad_category)).status_code == 422
    for max_per_booking in (0, 7, 101):  # at most 6 tickets in one booking
        assert (await put(client, auth, world.lead, event_id, config(max_per_booking=max_per_booking))).status_code == 422


# 4 -----------------------------------------------------------------------------------------


async def test_category_capacity_cannot_contradict_event_capacity(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    url = f"{API}/{event_id}/tickets"

    one_too_big = config(capacity=50, categories=[{"name": "General", "price_paise": 0, "capacity": 60}])
    response = await put(client, auth, world.lead, event_id, one_too_big)
    assert response.status_code == 400 and response.json()["error"]["code"] == "category_over_capacity"

    over = config(capacity=100, categories=[{"name": "Stalls", "price_paise": 0, "capacity": 70},
                                            {"name": "Balcony", "price_paise": 0, "capacity": 40}])
    response = await put(client, auth, world.lead, event_id, over)
    assert response.status_code == 400 and response.json()["error"]["code"] == "over_allocated"
    assert response.json()["error"]["message"] == "The categories hold 110 seats but the event has only 100"

    under = config(categories=[{"name": "Stalls", "price_paise": 0, "capacity": 70}])
    response = await put(client, auth, world.lead, event_id, under)
    assert response.status_code == 400 and response.json()["error"]["code"] == "unallocated_seats"

    # While sales are off, an incomplete split is a warning, never an error.
    draft = await put(client, auth, world.lead, event_id, {**under, "sales_enabled": False})
    assert draft.status_code == 200
    assert {"unallocated_seats", "sales_disabled"} <= {w["code"] for w in draft.json()["warnings"]}
    none = await put(client, auth, world.lead, event_id, config(sales_enabled=True, categories=[]))
    assert none.status_code == 400 and "no_categories" in [d["code"] for d in none.json()["error"]["details"]]
    assert (await client.get(url, headers=auth(world.lead))).json()["allocated"] == 70  # failed saves changed nothing

    duplicate = config(categories=[{"name": "VIP", "price_paise": 0, "capacity": 50},
                                   {"name": " vip ", "price_paise": 0, "capacity": 50}])
    assert (await put(client, auth, world.lead, event_id, duplicate)).status_code == 422


async def test_database_enforces_the_partition(db, world):
    """The deferred trigger is the backstop if something bypasses the service."""
    bake_sale, fest, club, lead = world.bake_sale.id, world.fest.id, world.club.id, world.lead.id
    now = datetime.now(timezone.utc)
    window = dict(opens_at=now, closes_at=now + timedelta(days=1), created_by=lead, updated_by=lead)
    db.add(EventTicketSettings(event_id=bake_sale, club_id=club, sales_enabled=True, capacity=10, **window))
    await db.flush()
    db.add(EventTicketCategory(event_id=bake_sale, name="General", base_price_paise=0, capacity=8, created_by=lead))
    with pytest.raises(Exception, match="event_ticket_partition|partition"):
        await db.commit()
    await db.rollback()

    # And a club id that is not the event's club is refused by the composite foreign key.
    other = Club(id=uuid.uuid4(), name="Elsewhere")
    db.add(other)
    await db.flush()
    db.add(EventTicketSettings(event_id=fest, club_id=other.id, sales_enabled=False, capacity=10, **window))
    with pytest.raises(Exception, match="event_ticket_settings_event_fk"):
        await db.flush()
    await db.rollback()


# 5 -----------------------------------------------------------------------------------------


async def test_invalid_price_rejected(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    for price in (-1, 10_000_001, 199.99, "20000", None):
        body = config()
        body["categories"][0]["price_paise"] = price
        response = await put(client, auth, world.lead, event_id, body)
        assert response.status_code == 422, price
    free = config()
    free["categories"][0]["price_paise"] = 0
    assert (await put(client, auth, world.lead, event_id, free)).status_code == 200  # free tickets are fine


# 6 -----------------------------------------------------------------------------------------


async def test_booking_window_rules(client, world, auth):
    event_id = await new_event(client, auth, world.lead)  # starts at(20) 18:00 IST
    late = await put(client, auth, world.lead, event_id, config(closes_at=at(20, 19)))
    assert late.status_code == 400 and late.json()["error"]["code"] == "window_after_start"

    at_start = await put(client, auth, world.lead, event_id, config(closes_at=at(20, 18)))
    assert at_start.status_code == 200  # closing exactly at the start is allowed

    backwards = await put(client, auth, world.lead, event_id, config(opens_at=at(5), closes_at=at(4)))
    assert backwards.status_code == 422
    assert backwards.json()["error"]["message"] == "Booking must close after it opens"

    # Moving the event earlier than the booking close time is refused, not silently clamped.
    moved = await client.patch(f"{API}/{event_id}", headers=auth(world.lead),
                               json={"starts_at": at(10), "ends_at": at(10, 22)})
    assert moved.status_code == 409 and moved.json()["error"]["code"] == "booking_window_conflict"


async def test_event_without_schedule_cannot_have_tickets(client, world, auth):
    response = await put(client, auth, world.president, str(world.bake_sale.id), config())
    assert response.status_code == 400 and response.json()["error"]["code"] == "missing_schedule"


# 7 -----------------------------------------------------------------------------------------


async def test_only_organizers_manage_tickets(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    url = f"{API}/{event_id}/tickets"
    assert (await client.get(url, headers=auth(world.lead2))).status_code == 404  # someone else's draft
    await set_status(client, auth, world.lead, event_id, "published", expect=200)

    for person in (world.lead2, world.volunteer, world.member, world.treasurer):
        assert (await client.get(url, headers=auth(person))).status_code == 403, person.full_name
        assert (await put(client, auth, person, event_id, config())).status_code == 403, person.full_name
    assert (await client.put(url, json=config())).status_code == 401

    assert (await put(client, auth, world.president, event_id, config())).status_code == 200
    assert (await client.get(url, headers=auth(world.lead))).json()["capacity"] == 100

    # club_id, sales_activated_at and other server-owned fields are never accepted.
    for extra in ({"club_id": str(uuid.uuid4())}, {"sales_activated_at": at(1)}, {"seats_committed": 0}):
        assert (await put(client, auth, world.lead, event_id, config(**extra))).status_code == 422


# 8 -----------------------------------------------------------------------------------------


async def test_cross_club_access_rejected(client, db, world, auth):
    event_id = await new_event(client, auth, world.lead)
    first = (await put(client, auth, world.lead, event_id, config(opens_at=at(-1)))).json()
    await set_status(client, auth, world.lead, event_id, "published", expect=200)

    other = Club(id=uuid.uuid4(), name="Other Club")
    db.add(other)
    await db.flush()
    rival = User(id=uuid.uuid4(), club_id=other.id, full_name="Rita Rival", email="rita@other.example",
                 role="president", is_active_member=True)
    db.add(rival)
    await db.commit()

    assert (await client.get(f"{API}/{event_id}/tickets", headers=auth(rival))).status_code == 404
    assert (await put(client, auth, rival, event_id, config())).status_code == 404
    assert (await client.get(f"{DISCOVERY}/{event_id}", headers=auth(rival))).status_code == 404
    assert (await client.get(DISCOVERY, headers=auth(rival))).json() == []

    # A category id from another event cannot be pulled into this one.
    other_event = await new_event(client, auth, world.lead, name="Other Night")
    body = config(categories=[{"id": first["categories"][0]["id"], "name": "Stolen", "price_paise": 0, "capacity": 100}])
    response = await put(client, auth, world.lead, other_event, body)
    assert response.status_code == 400 and response.json()["error"]["code"] == "unknown_category"


# 9 -----------------------------------------------------------------------------------------


async def test_published_event_is_discoverable(client, world, auth):
    event_id, current = await selling(client, auth, world)
    hidden = resend(current, capacity=110)
    hidden["categories"].append({"name": "Crew passes", "price_paise": 0, "capacity": 10, "is_visible": False})
    assert (await put(client, auth, world.lead, event_id, hidden)).status_code == 200

    listing = await client.get(DISCOVERY, headers=auth(world.member))
    assert listing.status_code == 200
    [event] = listing.json()
    assert event["id"] == event_id and event["club_id"] == str(world.club.id)
    assert event["name"] == "Spring Gala" and event["venue"] == "Main Hall" and event["status"] == "published"
    assert event["sale_state"] == "open" and event["availability"] == "live"
    assert [c["name"] for c in event["categories"]] == ["General", "Front row"]  # hidden category left out
    general = event["categories"][0]
    assert general["price_paise"] == 20000 and general["currency"] == "INR" and general["capacity"] == 80
    assert general["max_per_booking"] == 6 and general["seats_remaining"] == 80  # counted, not invented
    assert (general["sale_state"], general["booking_opens_at"]) == ("open", event["booking_opens_at"])
    assert (await client.get(f"{DISCOVERY}/{event_id}", headers=auth(world.volunteer))).json() == event

    # Holds and confirmed seats come off straight away.
    assert (await book(client, auth, world.member, event_id, general["id"], quantity=4)).status_code == 201
    after = (await client.get(f"{DISCOVERY}/{event_id}", headers=auth(world.volunteer))).json()
    assert [c["seats_remaining"] for c in after["categories"]] == [76, 20]


# 10 ----------------------------------------------------------------------------------------


async def test_unpublished_or_unsold_events_are_not_discoverable(client, world, auth):
    draft = await new_event(client, auth, world.lead, name="Secret Draft")
    assert (await put(client, auth, world.lead, draft, config())).status_code == 200
    off = await new_event(client, auth, world.lead, publish=True, name="Free Talk", starts_at=at(22), ends_at=at(22, 20))
    assert (await put(client, auth, world.lead, off, config(sales_enabled=False, closes_at=at(21)))).status_code == 200
    plain = await new_event(client, auth, world.lead, publish=True, name="No Tickets", starts_at=at(23), ends_at=at(23, 20))

    for viewer in (world.member, world.president, world.lead):  # not even organizers: discovery is the sales view
        assert (await client.get(DISCOVERY, headers=auth(viewer))).json() == []
    for event_id in (draft, off, plain):
        assert (await client.get(f"{DISCOVERY}/{event_id}", headers=auth(world.member))).status_code == 404
    assert (await client.get(DISCOVERY)).status_code == 401  # no anonymous reads yet


# 11 ----------------------------------------------------------------------------------------


async def test_events_without_tickets_behave_as_before(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    for status in ("published", "draft", "published", "cancelled"):
        await set_status(client, auth, world.lead, event_id, status, expect=200)
    rescheduled = await new_event(client, auth, world.lead, publish=True, name="Movable")
    moved = await client.patch(f"{API}/{rescheduled}", headers=auth(world.lead),
                               json={"starts_at": at(3), "ends_at": at(3, 21)})
    assert moved.status_code == 200


# 12 ----------------------------------------------------------------------------------------


async def test_draft_and_rescheduling_blocked_once_seats_are_booked(client, db, world, auth):
    event_id, current = await selling(client, auth, world)
    assert (await book(client, auth, world.member, event_id, current["categories"][0]["id"], quantity=2)).status_code == 201

    draft = await set_status(client, auth, world.president, event_id, "draft")
    assert draft.status_code == 409 and draft.json()["error"]["code"] == "ticket_sales_started"
    moved = await client.patch(f"{API}/{event_id}", headers=auth(world.lead), json={"starts_at": at(21), "ends_at": at(21, 22)})
    assert moved.status_code == 409 and moved.json()["error"]["code"] == "ticket_sales_started"
    venue = await client.patch(f"{API}/{event_id}", headers=auth(world.lead), json={"venue": "Open Air Theatre"})
    assert venue.status_code == 200  # details that don't invalidate tickets can still change
    assert (await client.get(f"{API}/{event_id}", headers=auth(world.lead))).json()["status"] == "published"

    # Completing after the event started is always allowed.
    event = await db.get(Event, uuid.UUID(event_id))
    event.starts_at = datetime.now(timezone.utc) - timedelta(hours=2)
    event.ends_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.commit()
    await set_status(client, auth, world.lead, event_id, "completed", expect=200)


async def test_with_no_seats_booked_a_selling_event_can_still_be_changed(client, world, auth):
    """The locks are about bookings. With the reservation service connected and reporting
    none, going back to draft, rescheduling and cancelling are all allowed."""
    event_id, _ = await selling(client, auth, world)
    moved = await client.patch(f"{API}/{event_id}", headers=auth(world.lead), json={"starts_at": at(21), "ends_at": at(21, 22)})
    assert moved.status_code == 200
    await set_status(client, auth, world.president, event_id, "draft", expect=200)
    await set_status(client, auth, world.lead, event_id, "cancelled", expect=200)


async def test_cancellation_allowed_before_sales_open(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    assert (await put(client, auth, world.lead, event_id, config(opens_at=at(5)))).status_code == 200
    await set_status(client, auth, world.lead, event_id, "published", expect=200)
    current = (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()
    assert current["sales_activated_at"] and not current["locked"] and current["sale_state"] == "not_open"
    await set_status(client, auth, world.lead, event_id, "draft", expect=200)
    await set_status(client, auth, world.lead, event_id, "cancelled", expect=200)
    frozen = await put(client, auth, world.lead, event_id, config())
    assert frozen.status_code == 400 and frozen.json()["error"]["code"] == "event_closed"


async def test_cancellation_allowed_when_reservation_service_reports_no_seats(client, world, auth, monkeypatch):
    event_id, _ = await selling(client, auth, world)

    async def nothing_booked(db, event_id):
        return {}

    monkeypatch.setattr(ticket_inventory, "committed_seats", nothing_booked)
    await set_status(client, auth, world.lead, event_id, "cancelled", expect=200)


async def test_publishing_rechecks_ticket_settings(client, db, world, auth):
    event_id = await new_event(client, auth, world.lead)
    assert (await put(client, auth, world.lead, event_id, config())).status_code == 200
    # The API cannot produce this (it checks on save and on reschedule); publishing re-checks anyway.
    settings = await db.get(EventTicketSettings, uuid.UUID(event_id))
    settings.closes_at = datetime.now(timezone.utc) + timedelta(days=30)
    await db.commit()
    refused = await set_status(client, auth, world.lead, event_id, "published")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "ticket_config_invalid"
    assert refused.json()["error"]["details"] == ["window_after_start"]


async def test_publishing_with_a_closed_window_only_warns(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    assert (await put(client, auth, world.lead, event_id, config(opens_at=at(-3), closes_at=at(-2)))).status_code == 200
    await set_status(client, auth, world.lead, event_id, "published", expect=200)
    current = (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()
    assert current["sale_state"] == "closed" and "window_over" in [w["code"] for w in current["warnings"]]


# 13 ----------------------------------------------------------------------------------------


async def test_structural_changes_locked_once_seats_are_booked(client, world, auth):
    event_id, current = await selling(client, auth, world)
    general = current["categories"][0]["id"]
    for buyer in (world.member, world.outsider):
        assert (await book(client, auth, buyer, event_id, general, quantity=6)).status_code == 201   # 12 seats

    smaller = resend(current, capacity=11)                      # fewer seats than are booked
    smaller["categories"][0]["capacity"] = 6
    smaller["categories"][1]["capacity"] = 5
    response = await put(client, auth, world.lead, event_id, smaller)
    assert response.status_code == 409 and response.json()["error"]["code"] == "capacity_locked"

    removed = resend(current)                                   # drop the category that has bookings
    removed["categories"] = removed["categories"][1:]
    removed["categories"][0]["capacity"] = 100
    response = await put(client, auth, world.lead, event_id, removed)
    assert response.status_code == 409 and response.json()["error"]["code"] == "category_locked"

    reopened = await put(client, auth, world.lead, event_id, resend(current, opens_at=at(2)))
    assert reopened.status_code == 409 and reopened.json()["error"]["code"] == "window_locked"

    # Safe edits still work: more seats, new price for future buyers, hide a category, pause sales.
    grown = resend(current, capacity=120)
    grown["categories"][0]["capacity"] = 100
    grown["categories"][1]["price_paise"] = 55000
    grown["categories"][1]["is_visible"] = False
    assert (await put(client, auth, world.lead, event_id, grown)).status_code == 200
    paused = await put(client, auth, world.lead, event_id, {**grown, "sales_enabled": False})
    assert paused.status_code == 200 and paused.json()["locked"]  # pausing does not unlock
    assert paused.json()["seats_committed"] == 12
    # A category nobody has booked or been quoted for can go, with its seats moved elsewhere.
    regrouped = resend(paused.json())
    regrouped["categories"] = regrouped["categories"][:1]
    regrouped["categories"][0]["capacity"] = 120
    assert (await put(client, auth, world.lead, event_id, regrouped)).status_code == 200


async def test_capacity_reduction_respects_committed_seats(client, world, auth, monkeypatch):
    """The guard logic, with seat counts stated by a stand-in for the reservation service."""
    event_id, current = await selling(client, auth, world)
    general_id = uuid.UUID(current["categories"][0]["id"])

    async def thirty_booked(db, event_id):
        return {general_id: 30}

    monkeypatch.setattr(ticket_inventory, "committed_seats", thirty_booked)
    ok = resend(current, capacity=50)
    ok["categories"][0]["capacity"] = 30
    assert (await put(client, auth, world.lead, event_id, ok)).status_code == 200

    too_small = resend((await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json(), capacity=49)
    too_small["categories"][0]["capacity"] = 29
    response = await put(client, auth, world.lead, event_id, too_small)
    assert response.status_code == 409 and response.json()["error"]["code"] == "category_locked"


async def test_capacity_reduction_with_real_bookings(client, db, world, auth):
    """Book N seats through the reservation service, then reduce capacity below N: expect 409."""
    event_id, current = await selling(client, auth, world)
    general, front = (c["id"] for c in current["categories"])
    for buyer, quantity in ((world.member, 6), (world.outsider, 5)):
        assert (await book(client, auth, buyer, event_id, general, quantity)).status_code == 201     # N = 11 in General
    assert (await book(client, auth, world.volunteer, event_id, front, 2)).status_code == 201
    assert (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()["seats_committed"] == 13

    def sized(general_seats: int, front_seats: int) -> dict:
        body = resend(current, capacity=general_seats + front_seats)
        body["categories"][0]["capacity"], body["categories"][1]["capacity"] = general_seats, front_seats
        return body

    below = await put(client, auth, world.lead, event_id, sized(10, 20))          # one fewer than booked
    assert below.status_code == 409 and below.json()["error"]["code"] == "category_locked"
    exact = await put(client, auth, world.lead, event_id, sized(11, 2))           # exactly the seats taken
    assert exact.status_code == 200, exact.text
    assert exact.json()["capacity"] == exact.json()["allocated"] == 13
    assert [c["seats_committed"] for c in exact.json()["categories"]] == [11, 2]
    full = await book(client, auth, world.lead2, event_id, general)
    assert full.status_code == 409 and full.json()["error"]["code"] == "sold_out"

    # A hold that runs out gives its seats back, and capacity can then follow them down.
    await db.execute(text("UPDATE ticket_bookings SET hold_expires_at = now() - interval '1 second' "
                          "WHERE purchaser_user_id = :user"), {"user": world.outsider.id})
    await db.commit()
    assert (await put(client, auth, world.lead, event_id, sized(6, 2))).status_code == 200


async def test_cancelling_releases_unpaid_bookings_and_is_blocked_by_paid_ones(client, db, world, auth, monkeypatch):
    """The cancellation guard, inside the event's own status change, with ticket payments
    switched off (as shipped): there is no way to refund, so a paid booking blocks it."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "fairseat_payments_enabled", False)
    event_id, current = await selling(client, auth, world)
    free_config = resend(current, capacity=110)
    free_config["categories"].append({"name": "Free entry", "price_paise": 0, "capacity": 10})
    saved = (await put(client, auth, world.lead, event_id, free_config)).json()
    general, free = saved["categories"][0]["id"], saved["categories"][2]["id"]
    held = (await book(client, auth, world.member, event_id, general, 3)).json()
    confirmed = (await book(client, auth, world.outsider, event_id, free, 2)).json()
    assert (confirmed["status"], len(confirmed["tickets"])) == ("confirmed", 2)
    paid = (await book(client, auth, world.volunteer, event_id, general, 1)).json()
    # With payments off nothing can mark a booking paid; the state is set directly to test the guard.
    await db.execute(text("UPDATE ticket_bookings SET status = 'confirmed', payment_status = 'paid', hold_expires_at = NULL, "
                          "stripe_payment_intent_id = 'pi_set_by_test', paid_at = now() WHERE id = :id"), {"id": paid["booking_id"]})
    await db.commit()

    blocked = await set_status(client, auth, world.lead, event_id, "cancelled")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "cancellation_requires_refunds"
    assert blocked.json()["error"]["details"] == {"paid_bookings": 1}
    # Refused as a whole: the event is still on, and nothing was released.
    assert (await client.get(f"{API}/{event_id}", headers=auth(world.lead))).json()["status"] == "published"
    statuses = dict((await db.execute(text("SELECT id::text, status FROM ticket_bookings"))).all())
    assert (statuses[held["booking_id"]], statuses[confirmed["booking_id"]]) == ("pending_payment", "confirmed")
    assert (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()["seats_committed"] == 6

    # Without the paid booking, cancelling goes ahead and gives everything back in the same transaction.
    await db.execute(text("UPDATE ticket_bookings SET status = 'cancelled', payment_status = 'pending', "
                          "stripe_payment_intent_id = NULL, paid_at = NULL WHERE id = :id"), {"id": paid["booking_id"]})
    await db.commit()
    await set_status(client, auth, world.lead, event_id, "cancelled", expect=200)
    statuses = dict((await db.execute(text("SELECT id::text, status FROM ticket_bookings"))).all())
    assert set(statuses.values()) == {"cancelled"}
    tickets = (await db.execute(text("SELECT status, revoked_at IS NOT NULL FROM event_tickets"))).all()
    assert tickets == [("revoked", True), ("revoked", True)]           # free entitlements are revoked
    assert (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()["seats_committed"] == 0
    refused = await client.post(f"{BOOKINGS}/quotes", headers=auth(world.lead2), json={
        "event_id": event_id, "ticket_category_id": general, "quantity": 1})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "event_not_bookable"


async def test_cancellation_through_refund_workflow(client, db, world, auth):
    """Cancel an event with paid bookings via the approved workflow; refunds and ledger entries follow."""
    from app.models.ledger import LedgerEntry
    from app.services import payment_service

    provider = payment_service.get_provider()
    provider.refunds.clear()
    provider.refund_results.clear()
    provider.next_refund_status = "succeeded"
    pay_api = "/api/v1/ticket-payments"
    event_id, current = await selling(client, auth, world)
    general = current["categories"][0]["id"]

    paid = []
    for buyer, quantity in ((world.member, 2), (world.outsider, 1)):
        booking = (await book(client, auth, buyer, event_id, general, quantity)).json()
        session = (await client.post(f"{pay_api}/stripe/create-checkout-session", headers=auth(buyer),
                                     json={"booking_id": booking["booking_id"]})).json()
        await client.post(f"{pay_api}/mock/simulate", headers=auth(buyer), json={"session_id": session["session_id"]})
        verified = await client.post(f"{pay_api}/stripe/verify", headers=auth(buyer), json={"session_id": session["session_id"]})
        assert verified.json()["outcome"] == "confirmed", verified.text
        paid.append(booking["booking_id"])
    held = (await book(client, auth, world.volunteer, event_id, general, 1)).json()
    assert (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()["seats_committed"] == 4

    await set_status(client, auth, world.lead, event_id, "cancelled", expect=200)

    rows = {str(row.id): row for row in (await db.execute(text(
        "SELECT id, status, payment_status, refund_status, refund_reason FROM ticket_bookings"))).all()}
    for booking_id in paid:
        row = rows[booking_id]
        assert (row.status, row.payment_status, row.refund_status, row.refund_reason) == (
            "cancelled", "refunded", "succeeded", "event_cancelled")
    assert (rows[held["booking_id"]].status, rows[held["booking_id"]].refund_status) == ("cancelled", None)
    assert {status for (status,) in (await db.execute(text("SELECT status FROM event_tickets"))).all()} == {"revoked"}
    assert len(provider.refunds) == 2                       # one refund per paid booking, no more
    entries = list(await db.scalars(select(LedgerEntry).where(LedgerEntry.source_module == "event_ticket")))
    assert sorted(entry.entry_type for entry in entries) == ["income", "income", "refund", "refund"]
    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert summary["current_balance"] == 0.0               # everything paid has gone back
    assert (await client.get(f"{API}/{event_id}/tickets", headers=auth(world.lead))).json()["seats_committed"] == 0


# 14 ----------------------------------------------------------------------------------------


async def test_utc_and_ist_handling(client, world, auth):
    event_id = await new_event(client, auth, world.lead)
    day = at(2)[:10]
    body = config(opens_at=f"{day}T10:00", closes_at=f"{day}T23:30:00+00:00")  # IST (no offset) and explicit UTC
    out = (await put(client, auth, world.lead, event_id, body)).json()
    assert out["opens_at"].startswith(f"{day}T04:30:00") and out["opens_at"].endswith(("Z", "+00:00"))
    assert out["closes_at"].startswith(f"{day}T23:30:00")

    page = await client.get(f"/events/{event_id}/tickets", headers=auth(world.lead))
    assert page.status_code == 200 and f'value="{day}T10:00"' in page.text  # shown back in IST


def test_sale_state_boundaries():
    opens = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)
    closes = opens + timedelta(days=5)
    event = EventTimes("published", closes + timedelta(hours=1), closes + timedelta(hours=4))
    state = lambda now, ev=event, enabled=True: ticket_rules.sale_state(enabled, opens, closes, ev, now)  # noqa: E731
    assert state(opens - timedelta(seconds=1)) == "not_open"
    assert state(opens) == "open"  # open is inclusive
    assert state(closes - timedelta(seconds=1)) == "open"
    assert state(closes) == "closed"  # close is exclusive
    assert state(opens, enabled=False) == "off"
    assert state(opens, ev=EventTimes("draft", event.starts_at, event.ends_at)) == "unpublished"
    assert state(opens, ev=EventTimes("cancelled", event.starts_at, event.ends_at)) == "closed"
    assert ticket_rules.sale_state(None, None, None, event, opens) == "not_configured"
    assert not ticket_rules.is_locked(None, opens, closes)
    assert not ticket_rules.is_locked(opens, opens, opens - timedelta(seconds=1))
    assert ticket_rules.is_locked(opens, opens, opens)


# 15 ----------------------------------------------------------------------------------------


EVENT_KEYS = {"id", "club_id", "name", "description", "starts_at", "ends_at", "event_date", "venue", "status",
              "lead_id", "lead_name", "created_by", "created_by_name", "created_at", "updated_at", "can_manage",
              "allowed_statuses", "progress", "crew",
              "budget_inr", "registration_capacity", "registration_opens_at", "registration_closes_at"}



async def test_response_contracts(client, world, auth):
    event_id, current = await selling(client, auth, world)
    event = (await client.get(f"{API}/{event_id}", headers=auth(world.lead))).json()
    assert set(event) == EVENT_KEYS  # the Event API is unchanged

    assert set(current) == {"event_id", "club_id", "configured", "sales_enabled", "capacity", "allocated", "opens_at",
                            "closes_at", "max_per_booking", "sales_activated_at", "sale_state", "locked", "categories",
                            "warnings", "seats_committed", "can_manage", "updated_at", "updated_by"}
    assert set(current["categories"][0]) == {"id", "name", "description", "price_paise", "capacity",
                                             "member_discount_eligible", "is_visible", "sort_order",
                                             "sales_open_at", "sales_close_at", "seats_committed"}  # last three are new

    [listed] = (await client.get(DISCOVERY, headers=auth(world.member))).json()
    assert set(listed) == {"id", "club_id", "name", "description", "venue", "starts_at", "ends_at", "status",
                           "booking_opens_at", "booking_closes_at", "sale_state", "categories", "availability"}
    assert set(listed["categories"][0]) == {"id", "name", "description", "price_paise", "currency",
                                            "member_discount_eligible", "capacity", "max_per_booking", "seats_remaining",
                                            "sale_state", "booking_opens_at", "booking_closes_at"}  # last three are new
    for key in ("starts_at", "ends_at", "booking_opens_at", "booking_closes_at"):
        assert listed[key].endswith(("Z", "+00:00")), key  # always UTC
    assert all(isinstance(c["price_paise"], int) for c in listed["categories"])


async def test_migration_leaves_existing_events_untouched(db, world):
    rows = (await db.execute(text("SELECT count(*) FROM event_ticket_settings"))).scalar()
    assert rows == 0  # events without settings are simply "not configured"
    assert await db.scalar(select(Event.name).where(Event.id == world.bake_sale.id)) == "Bake Sale"


async def test_ticket_pages_are_for_organizers_only(client, world, auth):
    event_id, _ = await selling(client, auth, world)
    page = await client.get(f"/events/{event_id}/tickets", headers=auth(world.lead))
    assert page.status_code == 200 and "Save ticket settings" in page.text and "Sales started" not in page.text
    assert "tickets may already be booked" in page.text  # the lock is explained
    assert (await client.get(f"/events/{event_id}/tickets", headers=auth(world.member))).status_code == 403

    organizer_view = await client.get(f"/events/{event_id}", headers=auth(world.lead))
    assert "FairSeat · tickets" in organizer_view.text and "Sales started" in organizer_view.text
    member_view = await client.get(f"/events/{event_id}", headers=auth(world.member))
    assert member_view.status_code == 200 and "FairSeat · tickets" not in member_view.text
