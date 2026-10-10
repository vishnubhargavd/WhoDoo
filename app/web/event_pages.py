"""Pages for Event Management, CrewPulse, Event Autopilot and SyncSphere (Member 2).

Same pattern as app/web/pages.py: pages read through the services, and every write
on a page is a fetch() call to /api/v1."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import AppError, forbidden
from app.services import event_service, expense_service, task_service, ticket_service
from app.services.syncsphere import IST
from app.web.templating import inr, render, templates

router = APIRouter(include_in_schema=False)


def ist(value: datetime | None, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    return value.astimezone(IST).strftime(fmt) if value else "—"


def ist_input(value: datetime | None) -> str:
    """Value for <input type=datetime-local>, in IST."""
    return value.astimezone(IST).strftime("%Y-%m-%dT%H:%M") if value else ""


def paise(value: int | None) -> str:
    """Integer paise as rupees, e.g. 25050 -> ₹250.50 (exact: no floating point)."""
    return "—" if value is None else inr(f"{value // 100}.{value % 100:02d}")


def paise_input(value: int) -> str:
    """Value for the rupee price input: 25050 -> 250.50"""
    return f"{value // 100}.{value % 100:02d}"


templates.env.filters["ist"] = ist
templates.env.filters["ist_input"] = ist_input
templates.env.filters["paise"] = paise
templates.env.filters["paise_input"] = paise_input


@router.get("/events")
async def events_page(
    request: Request,
    scope: str = "upcoming",
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scope = scope if scope in ("upcoming", "past") else "upcoming"
    events = await event_service.to_out(db, user, await event_service.list_events(db, user, scope))
    return render(request, "events/list.html", user, events=events, scope=scope,
                  can_create=event_service.can_create(user))


@router.get("/events/new")
async def new_event_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not event_service.can_create(user):
        raise forbidden("Only the president or an event lead can create events")
    leads = await event_service.list_leads(db, user.club_id)
    venues = get_settings().get_approved_venues()
    return render(request, "events/form.html", user, event=None, leads=leads, venues=venues)


@router.get("/events/{event_id}/edit")
async def edit_event_page(
    event_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    event = await event_service.get_managed_event(db, user, event_id)
    leads = await event_service.list_leads(db, user.club_id)
    venues = get_settings().get_approved_venues()
    return render(request, "events/form.html", user, event=event, leads=leads, venues=venues)


@router.get("/events/{event_id}")
async def event_detail_page(
    event_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    event = await event_service.get_visible_event(db, user, event_id)
    [out] = await event_service.to_out(db, user, [event])
    tasks = await task_service.to_out(db, user, await task_service.list_for_event(db, event))
    organizer = event_service.can_manage(user, event)
    event_expenses = await expense_service.list_event_expenses(db, user, event_id)
    context = dict(event=out, tasks=tasks, organizer=organizer, readiness=None, schedule=None, people=[], expenses=event_expenses)
    if organizer:
        context.update(
            readiness=await event_service.readiness(db, user, event),
            schedule=await event_service.schedule_check(db, user, event),
            people=await task_service.assignable_people(db, user.club_id),
            missing=event_service.missing_for_publish(event),
            # None while FairSeat is off: its tables may not exist, and the page shows no ticket card.
            tickets=await ticket_service.config_out(db, event, can_manage=True) if ticket_service.enabled() else None,
        )
    return render(request, "events/detail.html", user, **context)


@router.get("/events/{event_id}/tickets")
async def ticket_settings_page(
    event_id: uuid.UUID, request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """FairSeat ticket settings. Organizers only, like the API behind it."""
    if not ticket_service.enabled():
        raise AppError(404, "fairseat_disabled", "Ticket booking is not switched on")
    event = await event_service.get_managed_event(db, user, event_id)
    tickets = await ticket_service.config_out(db, event, can_manage=True)
    return render(request, "events/tickets.html", user, event=event, tickets=tickets,
                  editable=event.status in ("draft", "published"))


@router.get("/my-tasks")
async def my_tasks_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tasks = await task_service.to_out(db, user, await task_service.list_mine(db, user))
    return render(request, "events/my_tasks.html", user, tasks=tasks)
