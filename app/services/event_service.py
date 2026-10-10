"""Event Management: create, edit, list and move events through their lifecycle.

Authorization rules (checked here, never trusted from the request):
- Everything is scoped to the caller's own club; other clubs' events are "not found".
- Presidents manage every event. An event lead manages the events they lead.
- Only presidents and event leads create events. An event lead's new event is led by them.
- Only presidents change who leads an event.
- Drafts are visible to presidents, the event lead and the creator; everyone in the
  club sees published, cancelled and completed events.
- Events with ticketing: status and schedule changes first pass ticket_service's
  lifecycle guards (no draft, cancellation or rescheduling once tickets may be booked).
"""

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import ROLE_EVENT_LEAD, ROLE_PRESIDENT, CurrentUser
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.models.crewpulse import CrewTask
from app.models.expense import ExpenseClaim
from app.models.fairseat import TicketBooking, TicketCategory, TicketWaitlistEntry
from app.models.ledger import LedgerEntry
from app.models.shared import Event, User
from app.schemas.events import (
    CategoryAvailabilityOut,
    CrewMember,
    EventBudgetDashboardOut,
    EventCreate,
    EventOut,
    EventRegistrationStatsOut,
    EventUpdate,
    ReadinessOut,
    ScheduleCheckOut,
    TaskProgress,
    check_window,
)
from app.services import audit_service, autopilot, shared_directory, syncsphere, ticket_service
from app.services.syncsphere import IST

TRANSITIONS = {
    "draft": ("published", "cancelled"),
    "published": ("draft", "cancelled", "completed"),
    "cancelled": (),
    "completed": (),
}
LEAD_ROLES = (ROLE_EVENT_LEAD, ROLE_PRESIDENT)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Permissions -------------------------------------------------------------------


def can_create(user: CurrentUser) -> bool:
    return user.role in LEAD_ROLES


def can_manage(user: CurrentUser, event: Event) -> bool:
    if event.club_id != user.club_id:
        return False
    return user.role == ROLE_PRESIDENT or (user.role == ROLE_EVENT_LEAD and event.lead_id == user.id)


def can_view(user: CurrentUser, event: Event) -> bool:
    if event.club_id != user.club_id:
        return False
    return event.status != "draft" or can_manage(user, event) or event.created_by == user.id


def allowed_statuses(user: CurrentUser, event: Event, now: datetime) -> list[str]:
    if not can_manage(user, event):
        return []
    options = list(TRANSITIONS.get(event.status, ()))
    if "completed" in options and not (event.starts_at and event.starts_at <= now):
        options.remove("completed")
    return options


# --- Reads -------------------------------------------------------------------------


async def get_visible_event(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> Event:
    event = await db.get(Event, event_id)
    if event is None or not can_view(user, event):
        raise not_found("Event")
    return event


async def get_managed_event(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> Event:
    event = await get_visible_event(db, user, event_id)
    if not can_manage(user, event):
        raise forbidden("Only the president or this event's lead can do this")
    return event


async def progress_by_event(
    db: AsyncSession, event_ids: list[uuid.UUID], now: datetime
) -> tuple[dict[uuid.UUID, TaskProgress], dict[uuid.UUID, dict[uuid.UUID, list[int]]]]:
    """Task counts per event, and per event the [total, done, at risk] of each assignee."""
    result = {event_id: TaskProgress() for event_id in event_ids}
    crew: dict[uuid.UUID, dict[uuid.UUID, list[int]]] = {event_id: {} for event_id in event_ids}
    if not event_ids:
        return result, crew
    tasks = await db.scalars(select(CrewTask).where(CrewTask.event_id.in_(event_ids)))
    starts = dict((await db.execute(select(Event.id, Event.starts_at).where(Event.id.in_(event_ids)))).all())
    for task in tasks:
        progress = result[task.event_id]
        progress.total += 1
        setattr(progress, task.status, getattr(progress, task.status) + 1)
        risky = bool(autopilot.task_risks(task_snapshot(task), starts.get(task.event_id), now))
        progress.at_risk += risky
        if task.assignee_id:
            counts = crew[task.event_id].setdefault(task.assignee_id, [0, 0, 0])
            counts[0] += 1
            counts[1] += task.status == "done"
            counts[2] += risky
    return result, crew


async def to_out(
    db: AsyncSession, user: CurrentUser, events: list[Event], now: datetime | None = None
) -> list[EventOut]:
    now = now or utcnow()
    progress, crew = await progress_by_event(db, [event.id for event in events], now)
    people = await shared_directory.get_users(
        db, [e.lead_id for e in events] + [e.created_by for e in events] + [a for c in crew.values() for a in c]
    )
    name = lambda user_id: people[user_id].full_name if user_id in people else None  # noqa: E731
    return [
        EventOut.model_validate(event).model_copy(update={
            "lead_name": name(event.lead_id),
            "created_by_name": name(event.created_by),
            "can_manage": can_manage(user, event),
            "allowed_statuses": allowed_statuses(user, event, now),
            "progress": progress[event.id],
            "crew": sorted(
                (CrewMember(id=uid, full_name=people[uid].full_name, role=people[uid].role,
                            tasks_total=t, tasks_done=d, tasks_at_risk=r)
                 for uid, (t, d, r) in crew[event.id].items() if uid in people),
                key=lambda m: (-m.tasks_at_risk, m.full_name),
            ),
        })
        for event in events
    ]


async def list_events(
    db: AsyncSession, user: CurrentUser, scope: str = "upcoming", now: datetime | None = None
) -> list[Event]:
    now = now or utcnow()
    stmt = select(Event).where(Event.club_id == user.club_id)
    if user.role != ROLE_PRESIDENT:
        stmt = stmt.where(or_(Event.status != "draft", Event.lead_id == user.id, Event.created_by == user.id))
    # Written so no branch evaluates to NULL, which would drop a row from both lists.
    # Events without times (created before Event Management) fall back to event_date.
    not_over = or_(
        and_(Event.ends_at.isnot(None), Event.ends_at >= now),
        and_(Event.ends_at.is_(None), or_(Event.event_date.is_(None), Event.event_date >= now.astimezone(IST).date())),
    )
    active = Event.status.in_(("draft", "published")) & not_over
    if scope == "upcoming":
        stmt = stmt.where(active).order_by(Event.starts_at.asc().nulls_last(), Event.name)
    elif scope == "past":
        stmt = stmt.where(~active).order_by(Event.starts_at.desc().nulls_last(), Event.name)
    else:
        stmt = stmt.order_by(Event.starts_at.desc().nulls_last(), Event.name)
    return list(await db.scalars(stmt))


async def list_leads(db: AsyncSession, club_id: uuid.UUID) -> list[User]:
    return [u for u in await shared_directory.list_users(db, club_id) if u.role in LEAD_ROLES]


# --- Writes ------------------------------------------------------------------------


async def _check_lead(db: AsyncSession, club_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    lead = await shared_directory.get_user(db, lead_id)
    if lead is None or lead.club_id != club_id:
        raise bad_request("invalid_lead", "The event lead must be a member of this club")
    if lead.role not in LEAD_ROLES:
        raise bad_request("invalid_lead", "The event lead must be an event lead or the president")


async def _flush_unique(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "events_club_name_start_key" in str(exc.orig):
            raise conflict("duplicate_event", "An event with this name already starts at that time") from exc
        raise


def _sync_event_date(event: Event) -> None:
    event.event_date = event.starts_at.astimezone(IST).date() if event.starts_at else event.event_date


async def create_event(db: AsyncSession, user: CurrentUser, data: EventCreate, now: datetime | None = None) -> Event:
    now = now or utcnow()
    if not can_create(user):
        raise forbidden("Only the president or an event lead can create events")
    if data.starts_at <= now:
        raise bad_request("start_in_past", "The event must start in the future")
    lead_id = data.lead_id or (user.id if user.role == ROLE_EVENT_LEAD else None)
    if user.role == ROLE_EVENT_LEAD and lead_id != user.id:
        raise forbidden("Event leads create events they lead themselves; ask the president to assign someone else")
    if lead_id:
        await _check_lead(db, user.club_id, lead_id)

    venue = check_venue_approved(data.venue) if data.venue else None

    event = Event(
        club_id=user.club_id, name=data.name, description=data.description, starts_at=data.starts_at,
        ends_at=data.ends_at, venue=venue, lead_id=lead_id, status="draft", created_by=user.id,
        budget_inr=data.budget_inr,
        registration_capacity=data.registration_capacity,
        registration_opens_at=data.registration_opens_at,
        registration_closes_at=data.registration_closes_at,
    )
    _sync_event_date(event)
    db.add(event)
    await _flush_unique(db)
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="event_created",
                         entity_type="event", entity_id=event.id, details={"name": event.name})
    return event


def check_venue_approved(venue: str | None) -> str | None:
    if not venue:
        return None
    trimmed = venue.strip()
    from app.core.config import get_settings
    approved = get_settings().get_approved_venues()
    match = next((v for v in approved if v.lower() == trimmed.lower()), None)
    if not match:
        raise bad_request(
            "unapproved_venue",
            f"'{venue}' is not an approved campus venue. Please select an approved venue: {', '.join(approved)}"
        )
    return match


async def update_event(
    db: AsyncSession, user: CurrentUser, event: Event, data: EventUpdate, now: datetime | None = None
) -> Event:
    now = now or utcnow()
    if not can_manage(user, event):
        raise forbidden("Only the president or this event's lead can edit it")
    if event.status in ("cancelled", "completed"):
        raise conflict("event_closed", f"A {event.status} event can no longer be edited")
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return event

    if "venue" in changes and changes["venue"]:
        changes["venue"] = check_venue_approved(changes["venue"])

    if "lead_id" in changes and changes["lead_id"] != event.lead_id:
        if user.role != ROLE_PRESIDENT:
            raise forbidden("Only the president can change who leads an event")
        if changes["lead_id"]:
            await _check_lead(db, user.club_id, changes["lead_id"])

    starts_at = changes.get("starts_at", event.starts_at)
    ends_at = changes.get("ends_at", event.ends_at)
    if "starts_at" in changes or "ends_at" in changes:
        try:
            check_window(starts_at, ends_at)
        except ValueError as exc:
            raise bad_request("invalid_schedule", str(exc)) from exc
        if "starts_at" in changes and starts_at != event.starts_at and starts_at and starts_at <= now:
            raise bad_request("start_in_past", "The event must start in the future")
        await ticket_service.guard_schedule_change(db, event, starts_at, ends_at, now)

    before = {key: str(getattr(event, key)) for key in changes}
    for key, value in changes.items():
        setattr(event, key, value)
    if event.status == "published" and missing_for_publish(event):
        raise bad_request("missing_details", "A published event needs: " + ", ".join(missing_for_publish(event)))
    _sync_event_date(event)
    await _flush_unique(db)
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="event_updated", entity_type="event",
                         entity_id=event.id, details={"before": before, "after": {k: str(v) for k, v in changes.items()}})
    return event


def missing_for_publish(event: Event) -> list[str]:
    required = {"start time": event.starts_at, "end time": event.ends_at, "venue": event.venue, "event lead": event.lead_id}
    return [name for name, value in required.items() if not value]


async def change_status(
    db: AsyncSession, user: CurrentUser, event: Event, status: str, now: datetime | None = None
) -> Event:
    now = now or utcnow()
    if not can_manage(user, event):
        raise forbidden("Only the president or this event's lead can change its status")
    if status == event.status:
        return event  # repeated click: nothing to do
    if status not in TRANSITIONS.get(event.status, ()):
        raise conflict("invalid_transition", f"A {event.status} event cannot become {status}")
    if status == "published":
        missing = missing_for_publish(event)
        if missing:
            raise bad_request("missing_details", "Add these before publishing: " + ", ".join(missing), missing)
        if event.starts_at <= now:
            raise bad_request("start_in_past", "An event that has already started cannot be published")
    if status == "completed" and not (event.starts_at and event.starts_at <= now):
        raise bad_request("not_started", "An event can only be completed after it starts")
    await ticket_service.guard_status_change(db, event, status, now)

    previous, event.status = event.status, status
    await db.flush()
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="event_status_changed",
                         entity_type="event", entity_id=event.id, details={"from": previous, "to": status})
    return event


# --- SyncSphere and Event Autopilot --------------------------------------------------


def scheduled(event: Event) -> syncsphere.ScheduledEvent:
    return syncsphere.ScheduledEvent(
        event.id, event.name, event.starts_at, event.ends_at, event.venue, event.lead_id, event.status
    )


def task_snapshot(task: CrewTask) -> autopilot.TaskSnapshot:
    return autopilot.TaskSnapshot(task.id, task.title, task.status, task.is_critical, task.assignee_id, task.due_at)


HIDDEN_NAME = "an unpublished draft event"


async def _club_schedule(db: AsyncSession, viewer: CurrentUser) -> list[syncsphere.ScheduledEvent]:
    """The club's calendar as `viewer` may see it: drafts they cannot open still count
    for clashes, but their names are not revealed."""
    rows = await db.scalars(select(Event).where(Event.club_id == viewer.club_id, Event.status != "cancelled"))
    return [
        scheduled(e) if can_view(viewer, e) else replace(scheduled(e), name=HIDDEN_NAME)
        for e in rows
    ]


async def schedule_check(
    db: AsyncSession, user: CurrentUser, event: Event, now: datetime | None = None
) -> ScheduleCheckOut:
    now = now or utcnow()
    others = await _club_schedule(db, user)
    target = scheduled(event)
    report = syncsphere.find_conflicts(target, others)
    suggestions, note = ([], "")
    if report.conflicts:
        suggestions, note = syncsphere.suggest_slots(target, others, now)
    return ScheduleCheckOut(
        event_id=event.id, checked=report.checked, message=report.message,
        conflicts=[{**vars(c), "other_visible": c.other_event_name != HIDDEN_NAME} for c in report.conflicts],
        unchecked_events=report.unchecked_others,
        suggestions=[vars(s) for s in suggestions], suggestion_note=note,
    )


async def readiness(db: AsyncSession, user: CurrentUser, event: Event, now: datetime | None = None) -> ReadinessOut:
    now = now or utcnow()
    tasks = await db.scalars(select(CrewTask).where(CrewTask.event_id == event.id))
    schedule = syncsphere.find_conflicts(scheduled(event), await _club_schedule(db, user))
    snapshot = autopilot.EventSnapshot(
        event.id, event.name, event.status, event.starts_at, event.ends_at, event.venue, event.lead_id,
        event.description,
    )
    report = autopilot.assess(snapshot, [task_snapshot(t) for t in tasks], schedule, now)
    return ReadinessOut(
        event_id=event.id, applicable=report.applicable, score=report.score, band=report.band,
        summary=report.summary, factors=[vars(f) for f in report.factors], stats=report.stats, generated_at=now,
    )


async def get_event_budget_dashboard(
    db: AsyncSession, user: CurrentUser, event_id: uuid.UUID
) -> EventBudgetDashboardOut:
    """Authoritative event-specific financial summary: budget allocated, expenses approved, paid, pending, remaining, and over-budget warning."""
    event = await get_visible_event(db, user, event_id)

    # 1. Authoritative Expense Claims for this event
    claims_res = await db.execute(
        select(ExpenseClaim).where(ExpenseClaim.event_id == event.id)
    )
    claims = list(claims_res.scalars().all())

    expenses_approved = sum((c.amount for c in claims if c.status == "approved"), Decimal("0.00"))
    expenses_paid = sum((c.amount for c in claims if c.status == "paid"), Decimal("0.00"))
    expenses_pending = sum(
        (c.amount for c in claims if c.status in ("draft", "pending_approval", "change_requested")),
        Decimal("0.00"),
    )

    # 2. Check direct ledger outflows if any (not already tracked as claims)
    claim_ids = {c.id for c in claims}
    ledger_res = await db.execute(
        select(LedgerEntry).where(
            LedgerEntry.event_id == event.id,
            LedgerEntry.direction == "outflow",
        )
    )
    for entry in ledger_res.scalars().all():
        if entry.reference_id not in claim_ids:
            if entry.payment_status == "paid":
                expenses_paid += entry.amount
            elif entry.payment_status == "approved":
                expenses_approved += entry.amount
            elif entry.payment_status == "pending":
                expenses_pending += entry.amount

    budget_allocated = event.budget_inr
    remaining = budget_allocated - (expenses_approved + expenses_paid)
    is_over = remaining < Decimal("0.00")
    over_amount = abs(remaining) if is_over else Decimal("0.00")

    return EventBudgetDashboardOut(
        event_id=event.id,
        event_name=event.name,
        budget_allocated_inr=budget_allocated,
        expenses_approved_inr=expenses_approved,
        expenses_paid_inr=expenses_paid,
        expenses_pending_inr=expenses_pending,
        remaining_budget_inr=remaining,
        is_over_budget=is_over,
        over_budget_amount_inr=over_amount,
    )


async def get_event_registration_stats(
    db: AsyncSession, user: CurrentUser, event_id: uuid.UUID, now: datetime | None = None
) -> EventRegistrationStatsOut:
    """Calculated registration counts, category availability, low-seat warnings, and waitlist state from real database records."""
    now = now or utcnow()
    event = await get_visible_event(db, user, event_id)

    # Query all ticket categories for the event
    cat_res = await db.execute(
        select(TicketCategory).where(TicketCategory.event_id == event.id)
        .order_by(TicketCategory.sort_order, TicketCategory.name)
    )
    categories = list(cat_res.scalars().all())

    category_stats: list[CategoryAvailabilityOut] = []
    total_confirmed = 0
    total_held = 0

    for cat in categories:
        # Confirmed
        conf_stmt = select(func.coalesce(func.sum(TicketBooking.quantity), 0)).where(
            TicketBooking.event_id == event.id,
            TicketBooking.ticket_category_id == cat.id,
            TicketBooking.status == "confirmed",
        )
        conf_count = int(await db.scalar(conf_stmt) or 0)

        # Held (pending payment with unexpired hold)
        held_stmt = select(func.coalesce(func.sum(TicketBooking.quantity), 0)).where(
            TicketBooking.event_id == event.id,
            TicketBooking.ticket_category_id == cat.id,
            TicketBooking.status == "pending_payment",
            TicketBooking.hold_expires_at > now,
        )
        held_count = int(await db.scalar(held_stmt) or 0)

        total_confirmed += conf_count
        total_held += held_count

        remaining = max(0, cat.capacity - (conf_count + held_count))
        # Threshold: 10% remaining or 5 seats, whichever alerts earlier
        threshold = min(max(1, int(cat.capacity * 0.10)), 5)
        is_low = (0 < remaining <= threshold)
        is_sold_out = (remaining == 0)

        if event.registration_closes_at and now >= event.registration_closes_at:
            cat_status = "REGISTRATION_CLOSED"
        elif event.registration_opens_at and now < event.registration_opens_at:
            cat_status = "REGISTRATION_NOT_OPEN"
        elif is_sold_out:
            cat_status = "SOLD_OUT"
        elif is_low:
            cat_status = "LOW_SEATS"
        else:
            cat_status = "AVAILABLE"

        category_stats.append(
            CategoryAvailabilityOut(
                category_id=cat.id,
                name=cat.name,
                base_price_paise=cat.base_price_paise,
                capacity=cat.capacity,
                confirmed=conf_count,
                held=held_count,
                remaining=remaining,
                is_sold_out=is_sold_out,
                is_low_seats=is_low,
                status=cat_status,
            )
        )

    # Waitlist count
    wl_stmt = select(func.count(TicketWaitlistEntry.id)).where(
        TicketWaitlistEntry.event_id == event.id,
        TicketWaitlistEntry.status == "waiting",
    )
    waitlist_count = int(await db.scalar(wl_stmt) or 0)

    # Event-level totals
    overall_cap = event.registration_capacity or (sum(c.capacity for c in categories) if categories else None)
    total_allocated = total_confirmed + total_held
    remaining_overall = max(0, overall_cap - total_allocated) if overall_cap is not None else None

    is_sold_out_overall = (remaining_overall == 0) if remaining_overall is not None else False
    if overall_cap:
        thresh_overall = min(max(1, int(overall_cap * 0.10)), 5)
        is_low_overall = (0 < remaining_overall <= thresh_overall)
    else:
        is_low_overall = any(c.is_low_seats for c in category_stats)

    is_reg_closed = bool(event.registration_closes_at and now >= event.registration_closes_at)
    is_reg_not_open = bool(event.registration_opens_at and now < event.registration_opens_at)

    if is_reg_closed:
        event_status = "REGISTRATION_CLOSED"
        status_label = "Registration Closed"
    elif is_reg_not_open:
        event_status = "REGISTRATION_NOT_OPEN"
        status_label = "Registration Not Open"
    elif is_sold_out_overall:
        event_status = "SOLD_OUT"
        status_label = "Sold Out"
    elif is_low_overall:
        event_status = "LOW_SEATS"
        status_label = f"Low Seats Left ({remaining_overall} remaining)"
    else:
        event_status = "AVAILABLE"
        status_label = "Open for Registrations"

    return EventRegistrationStatsOut(
        event_id=event.id,
        capacity=overall_cap,
        confirmed_registrations=total_confirmed,
        pending_held_seats=total_held,
        total_allocated=total_allocated,
        remaining_seats=remaining_overall,
        is_low_seats=is_low_overall,
        is_sold_out=is_sold_out_overall,
        is_registration_closed=is_reg_closed,
        is_registration_not_open=is_reg_not_open,
        status=event_status,
        status_label=status_label,
        categories=category_stats,
        waitlist_count=waitlist_count,
    )

