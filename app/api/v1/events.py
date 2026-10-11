import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import not_found
from app.models.crewpulse import CrewTask
from app.schemas.crewpulse import AssignmentReviewIn, AssignmentReviewOut, TaskCreate, TaskOut
from app.schemas.events import (
    EventCreate,
    EventOut,
    EventStatusChange,
    EventUpdate,
    ReadinessOut,
    ScheduleCheckOut,
)
from app.services import assignment_review, event_service, fairseat_payments, registration_service, task_service

router = APIRouter(prefix="/events", tags=["Event Management"])


async def _one(db: AsyncSession, user: CurrentUser, event) -> EventOut:
    return (await event_service.to_out(db, user, [event]))[0]


@router.get("", response_model=list[EventOut])
async def list_events(
    scope: Literal["upcoming", "past", "all"] = "upcoming",
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EventOut]:
    return await event_service.to_out(db, user, await event_service.list_events(db, user, scope))


@router.post("", response_model=EventOut, status_code=201)
async def create_event(
    body: EventCreate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> EventOut:
    event = await event_service.create_event(db, user, body)
    await db.commit()
    return await _one(db, user, event)


@router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> EventOut:
    return await _one(db, user, await event_service.get_visible_event(db, user, event_id))


@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    background: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventOut:
    event = await event_service.get_visible_event(db, user, event_id)
    before = (event.starts_at, event.ends_at, event.venue)
    await event_service.update_event(db, user, event, body)
    await db.commit()
    if (event.starts_at, event.ends_at, event.venue) != before:
        # Everyone who registered is told about a new date, time or venue.
        background.add_task(registration_service.notify_participants, event.id, "Event update",
                            f"the details of {event.name} have changed. Please note the new date, time and place below.")
    return await _one(db, user, event)


class HandOver(BaseModel):
    lead_id: uuid.UUID


@router.get("/{event_id}/handover")
async def handover_options(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Who could take this event over: leads with nothing else on at that time. If nobody is free,
    the answer says so and offers other times instead."""
    event = await event_service.get_managed_event(db, user, event_id)
    free = await event_service.available_leads(db, event)
    schedule = await event_service.schedule_check(db, user, event)
    return {
        "double_booked": await event_service.lead_is_double_booked(db, event),
        "available_leads": [{"id": lead.id, "full_name": lead.full_name, "role": lead.role} for lead in free],
        "suggested_slots": [] if free else [{"starts_at": s.starts_at, "ends_at": s.ends_at} for s in schedule.suggestions],
    }


@router.post("/{event_id}/handover", response_model=EventOut)
async def hand_over(
    event_id: uuid.UUID, body: HandOver, background: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> EventOut:
    """Hand the event to another lead who is free then. Everyone registered is emailed."""
    event = await event_service.get_visible_event(db, user, event_id)
    lead = await event_service.hand_over(db, user, event, body.lead_id)
    await db.commit()
    background.add_task(registration_service.notify_participants, event.id, "New organiser",
                        f"{event.name} is now being organised by {lead.full_name}. Nothing else has changed.")
    return await _one(db, user, event)


@router.post("/{event_id}/status", response_model=EventOut)
async def change_status(
    event_id: uuid.UUID,
    body: EventStatusChange,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventOut:
    event = await event_service.get_visible_event(db, user, event_id)
    await event_service.change_status(db, user, event, body.status)
    await db.commit()
    if body.status == "cancelled":
        # Paid tickets were released inside that transaction and now owe refunds. The payment
        # provider is called only here, after the commit, with no locks held.
        await fairseat_payments.refund_cancelled_event(db, event_id)
    return await _one(db, user, event)


# --- Event Autopilot and SyncSphere (organizers only) --------------------------------


@router.get("/{event_id}/readiness", response_model=ReadinessOut, tags=["Event Autopilot"])
async def readiness(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReadinessOut:
    event = await event_service.get_managed_event(db, user, event_id)
    return await event_service.readiness(db, user, event)


@router.get("/{event_id}/schedule-check", response_model=ScheduleCheckOut, tags=["SyncSphere"])
async def schedule_check(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ScheduleCheckOut:
    event = await event_service.get_managed_event(db, user, event_id)
    return await event_service.schedule_check(db, user, event)


# --- CrewPulse tasks of one event ----------------------------------------------------


@router.get("/{event_id}/tasks", response_model=list[TaskOut], tags=["CrewPulse"])
async def event_tasks(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[TaskOut]:
    event = await event_service.get_visible_event(db, user, event_id)
    return await task_service.to_out(db, user, await task_service.list_for_event(db, event))


@router.post("/{event_id}/tasks/review", response_model=AssignmentReviewOut, tags=["CrewPulse"])
async def review_assignment(
    event_id: uuid.UUID,
    body: AssignmentReviewIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssignmentReviewOut:
    """Organizers: duplicate and clash checks for an assignment before saving it. Changes nothing.
    Saving runs the same checks again; a flagged assignment then needs `override_reason`."""
    event = await event_service.get_managed_event(db, user, event_id)
    if body.task_id is not None:
        task = await db.get(CrewTask, body.task_id)
        if task is None or task.event_id != event.id:
            raise not_found("Task")
    shift = (body.shift_starts_at, body.shift_ends_at) if body.shift_starts_at and body.shift_ends_at else None
    if shift is None and body.task_id is not None:
        shift = await assignment_review.shift_of(db, body.task_id)
    offered = {person.id for person in await task_service.assignable_people(db, user.club_id, event.id)}
    return await assignment_review.review(db, club_id=user.club_id, event=event, assignee_id=body.assignee_id,
                                          title=body.title, shift=shift, exclude_task_id=body.task_id, eligible=offered)


@router.post("/{event_id}/tasks", response_model=TaskOut, status_code=201, tags=["CrewPulse"])
async def create_task(
    event_id: uuid.UUID,
    body: TaskCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    event = await event_service.get_visible_event(db, user, event_id)
    task = await task_service.create_task(db, user, event, body)
    await db.commit()
    return (await task_service.to_out(db, user, [task]))[0]
