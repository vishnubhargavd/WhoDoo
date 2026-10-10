"""FairSeat event side: ticket settings, event discovery, and the lifecycle guards Event
Management calls before risky changes. (Member 2's service, joined to the reservation
tables.)

There is one category table, `ticket_categories`, and one place that writes it:
`save_config` below. The organizer page, Member 2's PUT route and the per-category
routes all go through it, so the partition rule, the booking-window rule and the locks
that protect existing bookings are checked the same way whichever door is used.

Seat counts come only from ticket_inventory (confirmed seats plus unexpired holds).
Bookings, holds and tickets themselves are fairseat_service's.

Locks: event row, settings row, category rows in id order, then bookings
(ticket_locks). Like the rest of the app, nothing here commits; the route does.

Authorization: routes resolve the event through event_service (club-scoped, organizer
check) and pass it in. Every function here also re-checks the caller's club.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import bad_request, conflict, not_found
from app.models.fairseat import TicketBooking, TicketCategory, TicketQuote
from app.models.shared import Event
from app.models.tickets import EventTicketSettings
from app.schemas.tickets import (
    DiscoveryCategoryOut,
    DiscoveryEventOut,
    ProblemOut,
    TicketCategoryOut,
    TicketConfigIn,
    TicketConfigOut,
)
from app.services import audit_service, ticket_inventory, ticket_locks, ticket_rules
from app.services.ticket_rules import CategorySpec, ConfigSpec, EventTimes


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    """The ticket tables are a pending migration. With FairSeat off they may not exist, so
    nothing here may touch them: events then behave exactly as they did before tickets."""
    return get_settings().fairseat_enabled


def _times(event: Event) -> EventTimes:
    return EventTimes(event.status, event.starts_at, event.ends_at)


async def get_settings_row(db: AsyncSession, event_id: uuid.UUID) -> EventTicketSettings | None:
    return await db.scalar(select(EventTicketSettings).where(EventTicketSettings.event_id == event_id))


async def _categories(db: AsyncSession, event_ids: list[uuid.UUID]) -> list[TicketCategory]:
    if not event_ids:
        return []
    stmt = (select(TicketCategory).where(TicketCategory.event_id.in_(event_ids))
            .order_by(TicketCategory.sort_order, TicketCategory.name))
    return list(await db.scalars(stmt))


def _spec(settings: EventTicketSettings, categories: list[TicketCategory]) -> ConfigSpec:
    return ConfigSpec(
        settings.sales_enabled, settings.capacity, settings.opens_at, settings.closes_at,
        tuple(CategorySpec(c.id, c.name, c.base_price_paise, c.capacity, c.active) for c in categories),
    )


def _category_out(category: TicketCategory, committed: dict[uuid.UUID, int] | None) -> TicketCategoryOut:
    return TicketCategoryOut(
        id=category.id, name=category.name, description=category.description,
        price_paise=category.base_price_paise, capacity=category.capacity,
        member_discount_eligible=category.member_discount_eligible, is_visible=category.active,
        sort_order=category.sort_order, sales_open_at=category.sales_open_at, sales_close_at=category.sales_close_at,
        seats_committed=None if committed is None else committed.get(category.id, 0),
    )


def _problem_error(problems: list[ticket_rules.Problem], status: int = 400):
    first = problems[0]
    details = [{"code": p.code, "message": p.message} for p in problems]
    message = "; ".join(p.message for p in problems)
    return (bad_request if status == 400 else conflict)(first.code, message, details)


async def _locked_and_committed(
    db: AsyncSession, settings: EventTicketSettings | None, now: datetime
) -> tuple[bool, dict[uuid.UUID, int] | None]:
    if settings is None:
        return False, None
    locked = ticket_rules.is_locked(settings.sales_activated_at, settings.opens_at, now)
    return locked, await ticket_inventory.committed_seats(db, settings.event_id)


def effective_window(settings: EventTicketSettings, category: TicketCategory) -> tuple[datetime, datetime]:
    """When this category can be booked: its own window, never wider than the event's."""
    opens = max(settings.opens_at, category.sales_open_at or settings.opens_at)
    closes = min(settings.closes_at, category.sales_close_at or settings.closes_at)
    return opens, closes


# --- Organizer settings -------------------------------------------------------------------


async def config_out(db: AsyncSession, event: Event, can_manage: bool, now: datetime | None = None) -> TicketConfigOut:
    now = now or utcnow()
    settings = await get_settings_row(db, event.id)
    if settings is None:
        return TicketConfigOut(
            event_id=event.id, club_id=event.club_id, configured=False, sale_state="not_configured",
            can_manage=can_manage,
            warnings=[ProblemOut(code="not_configured", message="Ticketing is not set up for this event")],
        )
    categories = await _categories(db, [event.id])
    spec = _spec(settings, categories)
    errors, warnings = ticket_rules.check_config(spec, _times(event), now)
    locked, committed = await _locked_and_committed(db, settings, now)
    return TicketConfigOut(
        event_id=event.id, club_id=event.club_id, configured=True, sales_enabled=settings.sales_enabled,
        capacity=settings.capacity, allocated=sum(c.capacity for c in categories),
        opens_at=settings.opens_at, closes_at=settings.closes_at, max_per_booking=settings.max_per_booking,
        sales_activated_at=settings.sales_activated_at,
        sale_state=ticket_rules.sale_state(settings.sales_enabled, settings.opens_at, settings.closes_at,
                                           _times(event), now),
        locked=locked, categories=[_category_out(c, committed) for c in categories],
        # A stored configuration can only become invalid through outside changes (e.g. the event
        # was cancelled); show those as warnings rather than failing the read.
        warnings=[ProblemOut(code=p.code, message=p.message) for p in errors + warnings],
        seats_committed=ticket_rules.total(committed), can_manage=can_manage,
        updated_at=settings.updated_at, updated_by=settings.updated_by,
    )


async def _has_history(db: AsyncSession, category_id: uuid.UUID) -> bool:
    """A category that has ever been quoted or booked is referenced by those rows."""
    booked = exists().where(TicketBooking.ticket_category_id == category_id)
    quoted = exists().where(TicketQuote.ticket_category_id == category_id)
    return bool(await db.scalar(select(booked | quoted)))


async def save_config(
    db: AsyncSession, user: CurrentUser, event: Event, data: TicketConfigIn, now: datetime | None = None
) -> list[TicketCategory]:
    """Replace the event's ticket configuration. THE category writer: every route that creates
    or changes a ticket category ends here. The caller has checked the user manages the event.
    Returns the saved categories in order."""
    now = now or utcnow()
    if event.club_id != user.club_id:
        raise not_found("Event")
    # Serialize with bookings and status changes: event row, settings row, category rows.
    await ticket_locks.lock_event(db, event.id)
    settings = await ticket_locks.lock_settings(db, event.id)
    by_id = await ticket_locks.lock_event_categories(db, event.id)
    existing = sorted(by_id.values(), key=lambda c: (c.sort_order, c.name))

    unknown = [str(c.id) for c in data.categories if c.id and c.id not in by_id]
    if unknown:
        raise bad_request("unknown_category", "A ticket category does not belong to this event", unknown)

    new = ConfigSpec(
        data.sales_enabled, data.capacity, data.opens_at, data.closes_at,
        tuple(CategorySpec(c.id, c.name, c.price_paise, c.capacity, c.is_visible) for c in data.categories),
    )
    errors, _ = ticket_rules.check_config(new, _times(event), now)
    if errors:
        raise _problem_error(errors)

    # A category's own window may only narrow the event's.
    windows: dict[int, tuple[datetime | None, datetime | None]] = {}
    for index, item in enumerate(data.categories):
        current = by_id.get(item.id) if item.id else None
        given = "sales_open_at" in item.model_fields_set or "sales_close_at" in item.model_fields_set
        opens, closes = (item.sales_open_at, item.sales_close_at) if given or current is None else (
            current.sales_open_at, current.sales_close_at)
        if opens is not None and (opens < data.opens_at or closes > data.closes_at):
            raise bad_request(
                "category_window_outside_event",
                f"{item.name}'s sales window must lie inside the event's booking window",
            )
        windows[index] = (opens, closes)

    committed: dict[uuid.UUID, int] = {}
    if settings is not None:
        locked, committed = await _locked_and_committed(db, settings, now)
        problems = ticket_rules.structural_problems(_spec(settings, existing), new, locked, committed)
        if problems:
            raise _problem_error(problems, status=409)
    # Whatever the lock state, seats that are taken stay taken.
    taken = sum(committed.values())
    if data.capacity < taken:
        raise conflict("capacity_below_occupied", f"{taken} seats are already taken, so capacity cannot be {data.capacity}",
                       {"occupied": taken, "requested": data.capacity})
    for item in data.categories:
        occupied = committed.get(item.id, 0) if item.id else 0
        if item.capacity < occupied:
            raise conflict(
                "capacity_below_occupied",
                f"{occupied} seats are already taken in {item.name}, so its capacity cannot be {item.capacity}",
                {"occupied": occupied, "requested": item.capacity},
            )
    keep = {c.id for c in data.categories if c.id}
    removed = [c for c in existing if c.id not in keep]
    for category in removed:
        if committed.get(category.id, 0) or await _has_history(db, category.id):
            raise conflict("category_locked", f"{category.name} has quotes or bookings, so it cannot be removed; hide it instead")

    before = _summary(settings, existing)
    if settings is None:
        settings = EventTicketSettings(event_id=event.id, club_id=event.club_id, created_by=user.id)
        db.add(settings)
    settings.sales_enabled = data.sales_enabled
    settings.capacity = data.capacity
    settings.opens_at = data.opens_at
    settings.closes_at = data.closes_at
    settings.max_per_booking = data.max_per_booking
    settings.updated_by = user.id
    settings.updated_at = now
    if data.sales_enabled and event.status == "published" and settings.sales_activated_at is None:
        settings.sales_activated_at = now
    await db.flush()

    # Removals first so a removed name can be reused by a new category in the same save.
    if removed:
        await db.execute(delete(TicketCategory).where(TicketCategory.id.in_([c.id for c in removed])))
    saved = []
    for order, item in enumerate(data.categories):
        category = by_id.get(item.id) if item.id else None
        if category is None:
            category = TicketCategory(id=uuid.uuid4(), event_id=event.id, created_by=user.id, pricing_version=1,
                                      base_price_paise=item.price_paise,
                                      member_discount_eligible=item.member_discount_eligible)
            db.add(category)
        elif (category.base_price_paise != item.price_paise
              or category.member_discount_eligible != item.member_discount_eligible):
            category.pricing_version += 1  # price quotes made under the old terms are refused from now on
        category.name = item.name
        category.description = item.description
        category.base_price_paise = item.price_paise
        category.capacity = item.capacity
        category.member_discount_eligible = item.member_discount_eligible
        category.active = item.is_visible
        category.sales_open_at, category.sales_close_at = windows[order]
        category.sort_order = order
        category.updated_at = now
        saved.append(category)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "ticket_categories_event_name_key" in str(exc.orig):
            raise conflict("duplicate_category", "Two ticket categories would have the same name") from exc
        raise
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="ticket_config_updated",
                         entity_type="event", entity_id=event.id,
                         details={"before": before, "after": _summary(settings, saved)})
    return saved


def _summary(settings: EventTicketSettings | None, categories: list[TicketCategory]) -> dict | None:
    if settings is None:
        return None
    return {
        "sales_enabled": settings.sales_enabled, "capacity": settings.capacity,
        "opens_at": settings.opens_at.isoformat(), "closes_at": settings.closes_at.isoformat(),
        "max_per_booking": settings.max_per_booking,
        "categories": [{"name": c.name, "price_paise": c.base_price_paise, "capacity": c.capacity,
                        "is_visible": c.active} for c in categories],
    }


# --- Lifecycle guards (called by event_service) ---------------------------------------------


async def _guard_locks(db: AsyncSession, event: Event) -> EventTicketSettings | None:
    """Take the event and settings locks for a lifecycle change, and make sure the event was
    not changed by someone else between being read and being locked."""
    seen = (event.status, event.starts_at, event.ends_at)
    await ticket_locks.lock_event(db, event.id)  # refreshes `event` in place
    if (event.status, event.starts_at, event.ends_at) != seen:
        raise conflict("event_changed", "This event was just changed by someone else. Reload and try again.")
    return await ticket_locks.lock_settings(db, event.id)


async def guard_status_change(db: AsyncSession, event: Event, new_status: str, now: datetime) -> None:
    """Refuse status changes that would strand bookings; validate tickets when publishing.
    Runs inside the same transaction as the status change itself.

    Cancelling: every hold and booking is released here and its tickets revoked. Paid bookings
    are marked as owing a refund, which the route sends after it commits (never under these
    locks). With ticket payments switched off there is no way to refund, so a paid booking
    blocks the cancellation.
    """
    if not enabled():
        return
    settings = await _guard_locks(db, event)
    if settings is None:
        return
    committed = await ticket_inventory.committed_seats(db, event.id)
    booked = ticket_rules.total(committed) or 0
    if new_status == "draft" and booked:
        raise conflict("ticket_sales_started", f"{booked} seats are booked, so this event cannot go back to draft")
    if new_status == "cancelled":
        paid = await ticket_inventory.paid_bookings(db, event.id)
        if paid and not get_settings().fairseat_payments_enabled:
            raise conflict(
                "cancellation_requires_refunds",
                f"{paid} booking(s) have been paid for. Cancelling needs ticket refunds, which are switched off",
                {"paid_bookings": paid},
            )
        if booked:
            # Imported here because fairseat_service itself depends on event_service.
            from app.services import fairseat_service

            await fairseat_service.release_bookings_for_event(db, event.id, now)
    if new_status == "published" and settings.sales_enabled:
        spec = _spec(settings, await _categories(db, [event.id]))
        errors, _ = ticket_rules.check_config(spec, EventTimes("published", event.starts_at, event.ends_at), now)
        if errors:
            raise bad_request("ticket_config_invalid", "Fix the ticket settings before publishing: "
                              + "; ".join(p.message for p in errors), [p.code for p in errors])
        settings.sales_activated_at = settings.sales_activated_at or now


async def guard_schedule_change(
    db: AsyncSession, event: Event, starts_at: datetime | None, ends_at: datetime | None, now: datetime
) -> None:
    if not enabled() or (starts_at == event.starts_at and ends_at == event.ends_at):
        return
    settings = await _guard_locks(db, event)
    if settings is None:
        return
    locked, committed = await _locked_and_committed(db, settings, now)
    problem = ticket_rules.schedule_problem(settings.closes_at, locked, committed, True, starts_at)
    if problem:
        raise conflict(problem.code, problem.message)


# --- Discovery (FairSeat) -------------------------------------------------------------------


async def discover(
    db: AsyncSession, user: CurrentUser, now: datetime | None = None, event_id: uuid.UUID | None = None
) -> list[DiscoveryEventOut]:
    """Published, ticketed events of the caller's own club that have not ended yet.

    Policy until a public-read policy is approved: signed-in members of the event's club only.
    Drafts, cancelled and completed events and events without ticketing never appear."""
    now = now or utcnow()
    S = EventTicketSettings
    stmt = (
        select(Event, S).join(S, S.event_id == Event.id)
        .where(Event.club_id == user.club_id, S.club_id == user.club_id, Event.status == "published",
               S.sales_enabled.is_(True), Event.starts_at.isnot(None), Event.ends_at > now)
        .order_by(Event.starts_at, Event.name)
    )
    if event_id is not None:
        stmt = stmt.where(Event.id == event_id)
    rows = (await db.execute(stmt)).all()
    categories: dict[uuid.UUID, list[TicketCategory]] = {}
    for category in await _categories(db, [event.id for event, _ in rows]):
        if category.active:
            categories.setdefault(category.event_id, []).append(category)
    occupied = await ticket_inventory.occupied_seats(
        db, [c.id for group in categories.values() for c in group], now)
    per_booking = get_settings().fairseat_max_per_booking

    result = []
    for event, settings in rows:
        if not categories.get(event.id):
            continue
        times = _times(event)
        state = ticket_rules.sale_state(True, settings.opens_at, settings.closes_at, times, now)
        listed = []
        for c in categories[event.id]:
            opens, closes = effective_window(settings, c)
            own = ticket_rules.sale_state(True, opens, closes, times, now)
            listed.append(DiscoveryCategoryOut(
                id=c.id, name=c.name, description=c.description, price_paise=c.base_price_paise,
                member_discount_eligible=c.member_discount_eligible, capacity=c.capacity,
                max_per_booking=min(settings.max_per_booking, per_booking, c.capacity),
                seats_remaining=max(c.capacity - occupied[c.id], 0),
                sale_state=own if own in ("not_open", "open") else "closed",
                booking_opens_at=opens, booking_closes_at=closes,
            ))
        result.append(DiscoveryEventOut(
            id=event.id, club_id=event.club_id, name=event.name, description=event.description,
            venue=event.venue, starts_at=event.starts_at, ends_at=event.ends_at, status="published",
            booking_opens_at=settings.opens_at, booking_closes_at=settings.closes_at,
            sale_state=state if state in ("not_open", "open") else "closed",
            categories=listed, availability="live",
        ))
    return result


async def discover_one(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> DiscoveryEventOut:
    found = await discover(db, user, event_id=event_id)
    if not found:
        raise not_found("Event")  # also for drafts and other clubs' events: nothing is revealed
    return found[0]
