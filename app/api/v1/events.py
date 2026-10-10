import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.crewpulse import TaskCreate, TaskOut
from app.schemas.events import (
    EventCreate,
    EventOut,
    EventStatusChange,
    EventUpdate,
    ReadinessOut,
    ScheduleCheckOut,
)
from app.services import event_service, fairseat_payments, task_service

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
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventOut:
    event = await event_service.get_visible_event(db, user, event_id)
    await event_service.update_event(db, user, event, body)
    await db.commit()
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
