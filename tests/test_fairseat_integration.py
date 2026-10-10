"""Event side and reservation side together, on real PostgreSQL: the lock order, the
lifecycle guard racing bookings, and the rules that only exist once the two are joined."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.database import SessionLocal, asyncpg_dsn
from app.models.fairseat import TicketBooking
from app.models.shared import Event
from app.schemas.fairseat import QuoteRequest, ReservationRequest
from app.schemas.tickets import TicketCategoryIn, TicketConfigIn
from app.services import event_service, fairseat_pricing, fairseat_service, ticket_locks, ticket_service
from tests.test_fairseat import API, NOW, acting, book, bookings, category, code, gala, key, occupied  # noqa: F401
from tests.test_fairseat import FakeMemberships, memberships  # noqa: F401

LOCK_ORDER = ["events", "event_ticket_settings", "ticket_categories", "ticket_bookings"]


async def status(client, auth, user, event_id, new_status):
    return await client.post(f"{API}/events/{event_id}/status", headers=auth(user), json={"status": new_status})


# --- Lock order ------------------------------------------------------------------------------


async def test_every_seat_changing_operation_locks_in_the_agreed_order(db, world, gala, category, book):
    """For each operation: hold one row from another connection and watch where the operation
    stops. Whatever comes before that row in the order must already be locked by it, and
    nothing after may be. That pins the order: event, settings, category, booking."""
    cat = await category(capacity=10)
    other = (await book(world.outsider, cat)).json()
    lapsed = (await book(world.member, cat)).json()   # a hold past its deadline, for the sweep to find
    await db.execute(text("UPDATE ticket_bookings SET hold_expires_at = now() - interval '1 second' WHERE id = :id"),
                     {"id": lapsed["booking_id"]})
    await db.commit()
    dsn = asyncpg_dsn(get_settings().database_url)
    rows = {"events": ("id", gala.id), "event_ticket_settings": ("event_id", gala.id),
            "ticket_categories": ("id", uuid.UUID(cat["id"])), "ticket_bookings": ("id", uuid.UUID(other["booking_id"]))}
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}

    quote_ids: list[uuid.UUID] = []

    async def new_quote():
        # Taken before anything is blocked: storing a quote references the category row itself.
        async with SessionLocal() as session:
            quote = await fairseat_service.create_quote(session, acting(world.volunteer), QuoteRequest(**selection))
            await session.commit()
            quote_ids.append(quote.id)

    async def reserve(session):
        await fairseat_service.reserve(session, acting(world.volunteer), ReservationRequest(
            **selection, idempotency_key=key(), accepted_quote_id=quote_ids[-1]))

    async def cancel(session):
        await fairseat_service.cancel_booking(session, acting(world.outsider), uuid.UUID(other["booking_id"]))

    async def sweep(session):
        await fairseat_service.expire_holds(session)

    async with SessionLocal() as session:   # read once, up front, as plain data
        saved = await ticket_service.config_out(session, await session.get(Event, gala.id), can_manage=True)
    same_config = TicketConfigIn(
        sales_enabled=True, capacity=saved.capacity, opens_at=saved.opens_at, closes_at=saved.closes_at,
        max_per_booking=6, categories=[TicketCategoryIn(
            id=c.id, name=c.name, price_paise=c.price_paise, capacity=c.capacity) for c in saved.categories])

    async def save_settings(session):
        event = await session.get(Event, gala.id)   # a plain read: takes no row lock
        await ticket_service.save_config(session, acting(world.president), event, same_config)

    async def cancel_event(session):
        event = await session.get(Event, gala.id)
        await event_service.change_status(session, acting(world.president), event, "cancelled")

    async def locked_tables(probe) -> set[str]:
        found = set()
        for table, (column, value) in rows.items():
            try:
                async with probe.transaction():
                    await probe.execute(f"SELECT 1 FROM {table} WHERE {column} = $1 FOR UPDATE NOWAIT", value)
            except asyncpg.exceptions.LockNotAvailableError:
                found.add(table)
        return found

    # (operation, the last table it locks)
    operations = [(reserve, "ticket_categories"), (cancel, "ticket_bookings"), (sweep, "ticket_categories"),
                  (save_settings, "ticket_categories"), (cancel_event, "ticket_bookings")]
    for operation, last in operations:
        reach = LOCK_ORDER[: LOCK_ORDER.index(last) + 1]
        for blocked_at in reach:
            column, value = rows[blocked_at]
            await new_quote()
            blocker, probe = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
            try:
                hold = blocker.transaction()
                await hold.start()
                await blocker.execute(f"SELECT 1 FROM {blocked_at} WHERE {column} = $1 FOR UPDATE", value)
                async with SessionLocal() as session:
                    task = asyncio.create_task(operation(session))
                    done, _ = await asyncio.wait({task}, timeout=0.6)
                    assert not done, f"{operation.__name__} did not wait for {blocked_at}"
                    earlier = set(LOCK_ORDER[: LOCK_ORDER.index(blocked_at)])
                    seen = await locked_tables(probe)
                    if seen != earlier | {blocked_at}:
                        await hold.rollback()
                        await asyncio.gather(task, return_exceptions=True)
                    assert seen == earlier | {blocked_at}, (operation.__name__, blocked_at, seen)
                    await hold.rollback()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
                    await session.rollback()   # nothing from these probes is kept
            finally:
                await blocker.close()
                await probe.close()


async def test_mixed_operations_at_once_never_deadlock_or_oversell(client, db, world, auth, gala, category, book):
    cat = await category(capacity=4)
    config_url = f"{API}/events/{gala.id}/tickets"
    current = (await client.get(config_url, headers=auth(world.president))).json()
    body = {key_: current[key_] for key_ in ("sales_enabled", "capacity", "opens_at", "closes_at", "max_per_booking")} | {
        "categories": [{"id": c["id"], "name": c["name"], "price_paise": c["price_paise"], "capacity": c["capacity"]}
                       for c in current["categories"]]}
    buyers = [world.treasurer, world.lead, world.lead2, world.volunteer, world.member, world.outsider]

    async def sweep():
        async with SessionLocal() as session:
            count = await fairseat_service.expire_holds(session)
            await session.commit()
            return count

    for _ in range(3):
        first = (await book(world.president, cat)).json()
        jobs = [book(buyer, cat) for buyer in buyers]
        jobs += [client.put(config_url, headers=auth(world.president), json=body) for _ in range(3)]
        jobs += [client.post(f"{API}/ticket-bookings/{first['booking_id']}/cancel", headers=auth(world.president))]
        jobs += [sweep(), sweep()]
        results = await asyncio.wait_for(asyncio.gather(*jobs), timeout=20)
        codes = [r.status_code for r in results if hasattr(r, "status_code")]
        assert all(status_code in (200, 201, 409) for status_code in codes), codes   # no 500, so no deadlock abort
        assert await occupied(db, cat) <= 4
        await db.execute(text("UPDATE ticket_bookings SET status = 'cancelled', hold_expires_at = NULL "
                              "WHERE status = 'pending_payment'"))
        await db.commit()


# --- Lifecycle guard racing bookings -----------------------------------------------------------


async def test_booking_and_event_cancellation_at_the_same_moment(client, db, world, auth, category, book):
    """Either the booking lands first and the cancellation releases it, or the cancellation
    lands first and the booking is refused. Never a live booking on a cancelled event."""
    buyers = [world.member, world.outsider, world.volunteer, world.lead2]
    for round_number in range(4):
        event = Event(id=uuid.uuid4(), club_id=world.club.id, name=f"Race {round_number}", lead_id=world.lead.id,
                      starts_at=NOW + timedelta(days=7), ends_at=NOW + timedelta(days=7, hours=2),
                      venue="Hall", status="published", created_by=world.president.id)
        db.add(event)
        await db.commit()
        cat = await category(capacity=5, event=event)
        quotes = []
        for buyer in buyers:
            selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
            quote = (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(buyer), json=selection)).json()
            quotes.append((buyer, selection | {"idempotency_key": key(), "accepted_quote_id": quote["id"]}))

        results = await asyncio.gather(
            status(client, auth, world.president, event.id, "cancelled"),
            *(client.post(f"{API}/ticket-bookings", headers=auth(buyer), json=request) for buyer, request in quotes))
        assert results[0].status_code == 200, results[0].text
        for response in results[1:]:
            assert response.status_code in (201, 409), response.text
            if response.status_code == 409:
                assert code(response) == "event_not_bookable"
        live = await db.scalar(select(func.count()).select_from(TicketBooking).where(
            TicketBooking.event_id == event.id, TicketBooking.status.in_(("pending_payment", "confirmed"))))
        assert live == 0 and await occupied(db, cat) == 0


async def test_booking_and_capacity_reduction_at_the_same_moment(client, db, world, auth, gala, category, book):
    cat = await category(capacity=3)
    config_url = f"{API}/events/{gala.id}/tickets"
    shrunk = {"sales_enabled": True, "capacity": 1, "max_per_booking": 6,
              "opens_at": (NOW - timedelta(days=2)).isoformat(), "closes_at": gala.starts_at.isoformat(),
              "categories": [{"id": cat["id"], "name": "General", "price_paise": 25000, "capacity": 1}]}
    buyers = [world.member, world.outsider, world.volunteer]
    results = await asyncio.gather(client.put(config_url, headers=auth(world.president), json=shrunk),
                                   *(book(buyer, cat) for buyer in buyers))
    saved = (await client.get(config_url, headers=auth(world.president))).json()
    taken = await occupied(db, cat)
    # Whatever order they landed in, the seats taken fit the capacity that was finally saved.
    assert taken <= saved["capacity"] == saved["allocated"]
    assert results[0].status_code in (200, 409)
    if results[0].status_code == 200:
        assert taken <= 1
    else:
        assert code(results[0]) in ("capacity_locked", "category_locked", "capacity_below_occupied") and saved["capacity"] == 3


async def test_status_change_notices_a_concurrent_edit(db, world, gala, category):
    await category(capacity=2)
    async with SessionLocal() as a, SessionLocal() as b:
        event = await a.get(Event, gala.id)                      # read as published
        other = await b.get(Event, gala.id)
        await event_service.change_status(b, acting(world.president), other, "draft")
        await b.commit()                                          # someone else moved it meanwhile
        with pytest.raises(AppError) as stale:
            await event_service.change_status(a, acting(world.president), event, "cancelled")
        assert (stale.value.status_code, stale.value.code) == (409, "event_changed")


# --- Rules that need both sides ------------------------------------------------------------------


async def test_booking_needs_ticket_sales_switched_on(client, db, world, auth, gala, category, book):
    cat = await category(capacity=5)
    config_url = f"{API}/events/{gala.id}/tickets"
    current = (await client.get(config_url, headers=auth(world.president))).json()
    body = {name: current[name] for name in ("capacity", "opens_at", "closes_at", "max_per_booking")} | {
        "categories": [{"id": c["id"], "name": c["name"], "price_paise": c["price_paise"], "capacity": c["capacity"]}
                       for c in current["categories"]]}
    assert (await client.put(config_url, headers=auth(world.president), json=body | {"sales_enabled": False})).status_code == 200
    paused = await book(world.member, cat)
    assert paused.status_code == 409 and code(paused) == "sales_not_enabled"
    listed = (await client.get(f"{API}/ticket-categories?event_id={gala.id}", headers=auth(world.member))).json()
    assert [item["on_sale"] for item in listed] == [False]
    assert (await client.get(f"{API}/discovery/events", headers=auth(world.member))).json() == []

    # The event's own per-booking limit applies when it is tighter than the app's.
    assert (await client.put(config_url, headers=auth(world.president), json=body | {"sales_enabled": True, "max_per_booking": 2})).status_code == 200
    three = await book(world.member, cat, quantity=3)
    assert three.status_code == 400 and code(three) == "invalid_quantity" and three.json()["error"]["details"] == {"limit": 2}
    assert (await book(world.member, cat, quantity=2)).status_code == 201
    [event] = (await client.get(f"{API}/discovery/events", headers=auth(world.member))).json()
    assert (event["categories"][0]["max_per_booking"], event["categories"][0]["seats_remaining"]) == (2, 3)


async def test_event_window_bounds_every_category_window(db, world, gala, category):
    """A category with its own narrower window is bookable only inside both."""
    opens, closes = NOW + timedelta(days=1), NOW + timedelta(days=2)
    early = await category(capacity=5, name="Early bird", sales_open_at=opens.isoformat(), sales_close_at=closes.isoformat())
    selection = {"event_id": early["event_id"], "ticket_category_id": early["id"], "quantity": 1}

    async def attempt(moment):
        async with SessionLocal() as session:
            try:
                await fairseat_service.create_quote(session, acting(world.member), QuoteRequest(**selection), now=moment)
                return "ok"
            except AppError as refused:
                return refused.code

    assert await attempt(NOW) == "booking_not_open"                       # event window open, category's not yet
    assert await attempt(opens) == "ok"
    assert await attempt(closes) == "booking_closed"                      # category's closed, event's still open
    # Move the event's window to end before the category's would: the event's wins.
    async with SessionLocal() as session:
        await session.execute(text("UPDATE event_ticket_settings SET closes_at = :closes WHERE event_id = :id"),
                              {"closes": opens + timedelta(hours=1), "id": gala.id})
        await session.commit()
    assert await attempt(opens + timedelta(minutes=30)) == "ok"
    assert await attempt(opens + timedelta(hours=1)) == "booking_closed"
    assert (early["sales_open_at"], early["sales_close_at"]) == (opens.isoformat().replace("+00:00", "Z"), closes.isoformat().replace("+00:00", "Z"))


async def test_category_marked_not_discount_eligible_is_sold_at_base_price(client, db, world, auth, gala, category, memberships):
    start = fairseat_pricing.to_ms(NOW - timedelta(days=30))
    memberships.records[world.member.id] = fairseat_pricing.Membership("active", str(world.club.id), start, start + 90 * 86_400_000, 2000)
    eligible = await category(capacity=5, price=50000, name="General")
    excluded = await category(capacity=5, price=50000, name="Front row", member_discount_eligible=False)

    async def quote(cat):
        selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 1}
        return (await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)).json()

    assert ((await quote(eligible))["unit_price_paise"], (await quote(excluded))["unit_price_paise"]) == (40000, 50000)
    assert (await quote(excluded))["discount_applied"] is False

    # Switching eligibility off makes earlier discounted quotes stale.
    held = await quote(eligible)
    off = await client.patch(f"{API}/ticket-categories/{eligible['id']}", headers=auth(world.president), json={"member_discount_eligible": False})
    assert off.status_code == 200 and off.json()["pricing_version"] == 2
    stale = await client.post(f"{API}/ticket-bookings", headers=auth(world.member), json={
        "event_id": eligible["event_id"], "ticket_category_id": eligible["id"], "quantity": 1,
        "idempotency_key": key(), "accepted_quote_id": held["id"]})
    assert stale.status_code == 409 and code(stale) == "quote_stale"


async def test_with_fairseat_off_events_never_touch_ticket_tables(client, db, world, auth, monkeypatch):
    """The ticket tables are a pending migration. With the switch off, nothing may query them."""
    monkeypatch.setattr(get_settings(), "fairseat_enabled", False)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("a ticket table was touched while FairSeat is off")

    for name in ("lock_event", "lock_settings", "lock_event_categories"):
        monkeypatch.setattr(ticket_locks, name, must_not_run)
    monkeypatch.setattr(ticket_service, "get_settings_row", must_not_run)

    start = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0)
    created = await client.post(f"{API}/events", headers=auth(world.lead), json={
        "name": "Plain event", "description": "No tickets.", "venue": "Room 1",
        "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=2)).isoformat()})
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    for new_status in ("published", "draft", "published", "cancelled"):
        assert (await status(client, auth, world.lead, event_id, new_status)).status_code == 200
    moved = await client.patch(f"{API}/events/{world.bake_sale.id}", headers=auth(world.president), json={"venue": "Elsewhere"})
    assert moved.status_code == 200

    from app.core.security import create_access_token
    client.cookies.set(get_settings().session_cookie, create_access_token(world.lead.id))
    page = await client.get(f"/events/{event_id}")
    assert page.status_code == 200 and "FairSeat" not in page.text
    assert (await client.get(f"/events/{event_id}/tickets")).status_code == 404
    for path in (f"/events/{event_id}/tickets", "/discovery/events", f"/ticket-categories?event_id={event_id}"):
        off = await client.get(API + path, headers=auth(world.lead))
        assert off.status_code == 404 and code(off) == "fairseat_disabled"


async def test_agreed_limits_hold_at_every_door_and_in_the_database(client, db, world, auth, gala, category):
    """Names 1 to 120 characters, at most 6 tickets per booking, at most 10,000,000 paise."""
    from sqlalchemy.exc import IntegrityError

    cat = await category(capacity=10, name="G")                       # a one-character name is fine
    config_url = f"{API}/events/{gala.id}/tickets"
    current = (await client.get(config_url, headers=auth(world.president))).json()
    assert current["max_per_booking"] == 6
    base = {name: current[name] for name in ("sales_enabled", "capacity", "opens_at", "closes_at")}

    def with_category(**changes) -> dict:
        return base | {"categories": [{"id": cat["id"], "name": "G", "price_paise": 25000, "capacity": 10} | changes]}

    async def put(body):
        return await client.put(config_url, headers=auth(world.president), json=body)

    defaulted = await put(with_category())                               # max_per_booking left out
    assert defaulted.status_code == 200 and defaulted.json()["max_per_booking"] == 6
    assert (await put(with_category(name="N" * 120))).status_code == 200
    for bad in ({"name": ""}, {"name": "N" * 121}, {"price_paise": 10_000_001}):
        assert (await put(with_category(**bad))).status_code == 422, bad
    assert (await put(with_category(price_paise=10_000_000))).status_code == 200
    for too_many in (7, 10, 100):
        assert (await put(with_category() | {"max_per_booking": too_many})).status_code == 422

    # The per-category door applies the same limits.
    patch_url = f"{API}/ticket-categories/{cat['id']}"
    assert (await client.patch(patch_url, headers=auth(world.president), json={"name": "X"})).status_code == 200
    for bad in ({"name": ""}, {"name": "N" * 121}, {"base_price_paise": 10_000_001}):
        assert (await client.patch(patch_url, headers=auth(world.president), json=bad)).status_code == 422, bad

    # Seven in one booking is refused by the booking API itself.
    selection = {"event_id": cat["event_id"], "ticket_category_id": cat["id"], "quantity": 7}
    seven = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(world.member), json=selection)
    assert seven.status_code == 400 and code(seven) == "invalid_quantity" and seven.json()["error"]["details"] == {"limit": 6}

    # And the database refuses them if anything ever bypasses the service.
    for statement in ("UPDATE event_ticket_settings SET max_per_booking = 7",
                      "UPDATE ticket_categories SET base_price_paise = 10000001",
                      "UPDATE ticket_categories SET name = repeat('N', 121)",
                      "UPDATE ticket_categories SET name = '  '"):
        with pytest.raises(IntegrityError):
            await db.execute(text(statement))
        await db.rollback()
