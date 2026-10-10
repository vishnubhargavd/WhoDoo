"""FairSeat event-side service (Member 2): ticket settings, event discovery, and the
lifecycle guards Event Management calls before risky changes.

Bookings, holds, payments and refunds are not handled here (Member 3). Seat counts come
only from ticket_inventory.committed_seats; nothing is inferred or invented.

Authorization: routes resolve the event through event_service (club-scoped, organizer
check) and pass it in. Every function here also re-checks the caller's club.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser
from app.core.errors import bad_request, conflict, not_found
from app.models.shared import Event
from app.models.tickets import EventTicketCategory, EventTicketSettings
from app.schemas.tickets import (
    DiscoveryCategoryOut,
    DiscoveryEventOut,
    ProblemOut,
    TicketCategoryOut,
    TicketConfigIn,
    TicketConfigOut,
)
from app.services import audit_service, ticket_inventory, ticket_rules
from app.services.ticket_rules import CategorySpec, ConfigSpec, EventTimes


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _times(event: Event) -> EventTimes:
    return EventTimes(event.status, event.starts_at, event.ends_at)


async def _settings(db: AsyncSession, event_id: uuid.UUID, lock: bool = False) -> EventTicketSettings | None:
    """The event's settings row. lock=True takes the same row lock the reservation service uses."""
    stmt = select(EventTicketSettings).where(EventTicketSettings.event_id == event_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def _categories(db: AsyncSession, event_ids: list[uuid.UUID]) -> list[EventTicketCategory]:
    if not event_ids:
        return []
    stmt = (select(EventTicketCategory).where(EventTicketCategory.event_id.in_(event_ids))
            .order_by(EventTicketCategory.sort_order, EventTicketCategory.name))
    return list(await db.scalars(stmt))


def _spec(settings: EventTicketSettings, categories: list[EventTicketCategory]) -> ConfigSpec:
    return ConfigSpec(
        settings.sales_enabled, settings.capacity, settings.opens_at, settings.closes_at,
        tuple(CategorySpec(c.id, c.name, c.price_paise, c.capacity, c.is_visible) for c in categories),
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


# --- Organizer settings -------------------------------------------------------------------


async def config_out(db: AsyncSession, event: Event, can_manage: bool, now: datetime | None = None) -> TicketConfigOut:
    now = now or utcnow()
    settings = await _settings(db, event.id)
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
        locked=locked, categories=[TicketCategoryOut.model_validate(c) for c in categories],
        # A stored configuration can only become invalid through outside changes (e.g. the event
        # was cancelled); show those as warnings rather than failing the read.
        warnings=[ProblemOut(code=p.code, message=p.message) for p in errors + warnings],
        seats_committed=ticket_rules.total(committed), can_manage=can_manage,
        updated_at=settings.updated_at, updated_by=settings.updated_by,
    )


async def save_config(
    db: AsyncSession, user: CurrentUser, event: Event, data: TicketConfigIn, now: datetime | None = None
) -> None:
    """Replace the event's ticket configuration. The caller has checked the user manages the event."""
    now = now or utcnow()
    if event.club_id != user.club_id:
        raise not_found("Event")
    # Serialize with status changes and with the reservation service: event row, then settings row.
    await db.execute(select(Event.id).where(Event.id == event.id).with_for_update())
    settings = await _settings(db, event.id, lock=True)
    existing = await _categories(db, [event.id]) if settings else []
    by_id = {c.id: c for c in existing}

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
    if settings is not None:
        locked, committed = await _locked_and_committed(db, settings, now)
        problems = ticket_rules.structural_problems(_spec(settings, existing), new, locked, committed)
        if problems:
            raise _problem_error(problems, status=409)

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
    if data.sales_enabled and event.status == "published" and settings.sales_activated_at is None:
        settings.sales_activated_at = now
    await db.flush()

    # Removals first so a removed name can be reused by a new category in the same save.
    keep = {c.id for c in data.categories if c.id}
    removed = [c.id for c in existing if c.id not in keep]
    if removed:
        await db.execute(delete(EventTicketCategory).where(EventTicketCategory.id.in_(removed)))
    saved = []
    for order, item in enumerate(data.categories):
        category = by_id.get(item.id) if item.id else None
        if category is None:
            category = EventTicketCategory(event_id=event.id, club_id=event.club_id)
            db.add(category)
        category.name = item.name
        category.description = item.description
        category.price_paise = item.price_paise
        category.capacity = item.capacity
        category.member_discount_eligible = item.member_discount_eligible
        category.is_visible = item.is_visible
        category.sort_order = order
        saved.append(category)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "event_ticket_categories_name_key" in str(exc.orig):
            raise conflict("duplicate_category", "Two ticket categories would have the same name") from exc
        raise
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="ticket_config_updated",
                         entity_type="event", entity_id=event.id,
                         details={"before": before, "after": _summary(settings, saved)})


def _summary(settings: EventTicketSettings | None, categories: list[EventTicketCategory]) -> dict | None:
    if settings is None:
        return None
    return {
        "sales_enabled": settings.sales_enabled, "capacity": settings.capacity,
        "opens_at": settings.opens_at.isoformat(), "closes_at": settings.closes_at.isoformat(),
        "max_per_booking": settings.max_per_booking,
        "categories": [{"name": c.name, "price_paise": c.price_paise, "capacity": c.capacity,
                        "is_visible": c.is_visible} for c in categories],
    }


# --- Lifecycle guards (called by event_service) ---------------------------------------------


async def guard_status_change(db: AsyncSession, event: Event, new_status: str, now: datetime) -> None:
    """Refuse status changes that would strand bookings; validate tickets when publishing."""
    settings = await _settings(db, event.id, lock=True)
    if settings is None:
        return
    locked, committed = await _locked_and_committed(db, settings, now)
    problem = ticket_rules.status_problem(new_status, locked, committed)
    if problem:
        raise conflict(problem.code, problem.message)
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
    if starts_at == event.starts_at and ends_at == event.ends_at:
        return
    settings = await _settings(db, event.id, lock=True)
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
    categories: dict[uuid.UUID, list[EventTicketCategory]] = {}
    for category in await _categories(db, [event.id for event, _ in rows]):
        if category.is_visible:
            categories.setdefault(category.event_id, []).append(category)

    result = []
    for event, settings in rows:
        if not categories.get(event.id):
            continue
        state = ticket_rules.sale_state(True, settings.opens_at, settings.closes_at, _times(event), now)
        result.append(DiscoveryEventOut(
            id=event.id, club_id=event.club_id, name=event.name, description=event.description,
            venue=event.venue, starts_at=event.starts_at, ends_at=event.ends_at, status="published",
            booking_opens_at=settings.opens_at, booking_closes_at=settings.closes_at,
            sale_state=state if state in ("not_open", "open") else "closed",
            categories=[
                DiscoveryCategoryOut(
                    id=c.id, name=c.name, description=c.description, price_paise=c.price_paise,
                    member_discount_eligible=c.member_discount_eligible, capacity=c.capacity,
                    max_per_booking=min(settings.max_per_booking, c.capacity),
                )
                for c in categories[event.id]
            ],
        ))
    return result


async def discover_one(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> DiscoveryEventOut:
    found = await discover(db, user, event_id=event_id)
    if not found:
        raise not_found("Event")  # also for drafts and other clubs' events: nothing is revealed
    return found[0]
