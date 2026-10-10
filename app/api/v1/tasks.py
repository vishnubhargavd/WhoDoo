import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.crewpulse import TaskActivityOut, TaskOut, TaskStatusChange, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["CrewPulse"])


@router.get("/mine", response_model=list[TaskOut])
async def my_tasks(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[TaskOut]:
    return await task_service.to_out(db, user, await task_service.list_mine(db, user))


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    task = await task_service.update_task(db, user, task_id, body)
    await db.commit()
    return (await task_service.to_out(db, user, [task]))[0]


@router.post("/{task_id}/status", response_model=TaskOut)
async def set_status(
    task_id: uuid.UUID,
    body: TaskStatusChange,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    task = await task_service.set_status(db, user, task_id, body)
    await db.commit()
    return (await task_service.to_out(db, user, [task]))[0]


@router.get("/{task_id}/activity", response_model=list[TaskActivityOut])
async def task_activity(
    task_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[TaskActivityOut]:
    return await task_service.activity(db, user, task_id)
