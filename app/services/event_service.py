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

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import ROLE_EVENT_LEAD, ROLE_PRESIDENT, CurrentUser
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.models.crewpulse import CrewTask
from app.models.registration import LIVE, EventRegistration
from app.models.shared import Event, User
from app.schemas.events import (
    CrewMember,
    EventCreate,
    EventOut,
    EventUpdate,
    ReadinessOut,
    ScheduleCheckOut,
    TaskProgress,
    check_window,
)
from app.services import audit_service, autopilot, shared_directory, syncsphere, ticket_service, venue_service
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

    event = Event(
        club_id=user.club_id, name=data.name, description=data.description, starts_at=data.starts_at,
        ends_at=data.ends_at, venue=data.venue, lead_id=lead_id, status="draft", created_by=user.id,
        registration_mode="none", price_paise=0,
    )
    await _apply_registration(db, event, data.model_dump(exclude_unset=True), now)
    _sync_event_date(event)
    db.add(event)
    await _flush_unique(db)
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="event_created",
                         entity_type="event", entity_id=event.id, details={"name": event.name})
    return event


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
        if key not in REGISTRATION_FIELDS:
            setattr(event, key, value)
    await _apply_registration(db, event, changes, now)
    if event.status == "published" and missing_for_publish(event):
        raise bad_request("missing_details", "A published event needs: " + ", ".join(missing_for_publish(event)))
    if event.status == "published":
        await venue_service.check_registration_setup(db, event)
    _sync_event_date(event)
    await _flush_unique(db)
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="event_updated", entity_type="event",
                         entity_id=event.id, details={"before": before, "after": {k: str(v) for k, v in changes.items()}})
    return event


REGISTRATION_FIELDS = ("venue_id", "registration_mode", "max_participants", "price_paise")


async def _apply_registration(db: AsyncSession, event: Event, changes: dict, now: datetime) -> None:
    """Apply the venue and public-registration fields of a create or update, keeping them consistent.

    The venue's name is copied into `event.venue`, which is what schedule clash detection compares.
    A free event has no price; an event without registration has no limit. Whether the setup is
    complete (a venue big enough, a price if paid) is checked when the event is published.
    """
    if changes.get("venue_id"):
        venue = await venue_service.get_venue(db, changes["venue_id"])
        event.venue_id, event.venue = venue.id, venue.name
    elif "venue_id" in changes or "venue" in changes:
        event.venue_id = None   # a venue typed by hand is not one from the list
    if not any(field in changes for field in REGISTRATION_FIELDS[1:]):
        return
    mode = changes.get("registration_mode") or event.registration_mode or "none"
    limit = changes["max_participants"] if "max_participants" in changes else event.max_participants
    price = changes["price_paise"] if changes.get("price_paise") is not None else (event.price_paise or 0)

    taken = await db.scalar(
        select(func.count()).select_from(EventRegistration)
        .where(EventRegistration.event_id == event.id, EventRegistration.status.in_(LIVE))
    ) if event.id else 0
    if taken:
        if mode != event.registration_mode:
            raise conflict("registrations_exist", f"{taken} people have registered, so free/paid can no longer be changed.")
        if mode == "paid" and price != event.price_paise:
            raise conflict("registrations_exist", f"{taken} people have registered, so the price can no longer be changed.")
        if limit is not None and limit < taken:
            raise conflict("below_registered", f"{taken} people have already registered; the limit cannot go below that.")
    if mode == "none":
        limit, price = None, 0
    else:
        if not limit:
            raise bad_request("participants_required", "Say how many participants this event can take.")
        if mode == "paid" and not price:
            raise bad_request("price_required", "A paid event needs a ticket price.")
        if mode == "free":
            price = 0
        if ticket_service.enabled():
            settings = await ticket_service.get_settings_row(db, event.id) if event.id else None
            if settings is not None and settings.sales_enabled:
                raise conflict("fairseat_in_use", "This event sells FairSeat tickets. Switch those off before "
                                                  "opening public registration.")
    event.registration_mode, event.max_participants, event.price_paise = mode, limit, price


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
        await venue_service.check_registration_setup(db, event)
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
