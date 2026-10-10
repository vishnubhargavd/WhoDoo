"""CrewPulse: volunteer tasks for an event.

Who can do what:
- The president and the event's lead create, edit and assign tasks (organizers).
- The assignee can move their own task between statuses and report a blocker.
- Anyone in the club who can see the event can see its tasks.
Tasks of cancelled or completed events are frozen.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.models.crewpulse import CrewTask, CrewTaskActivity
from app.models.shared import Event, User
from app.schemas.crewpulse import AssigneeOut, TaskActivityOut, TaskCreate, TaskOut, TaskStatusChange, TaskUpdate
from app.models.member2 import CrewTaskShift
from app.services import assignment_review, audit_service, autopilot, event_service, modules, shared_directory
from app.services.event_service import utcnow

OPEN_EVENT_STATUSES = ("draft", "published")


def _log(db, task: CrewTask, actor: CurrentUser, action: str, old=None, new=None, note=None) -> None:
    db.add(CrewTaskActivity(club_id=task.club_id, task_id=task.id, actor_id=actor.id, action=action,
                            from_value=None if old is None else str(old), to_value=None if new is None else str(new),
                            note=note))


def _ensure_open(event: Event) -> None:
    if event.status not in OPEN_EVENT_STATUSES:
        raise conflict("event_closed", f"Tasks of a {event.status} event cannot be changed")


async def _check_assignee(db: AsyncSession, club_id: uuid.UUID, assignee_id: uuid.UUID) -> None:
    person = await shared_directory.get_user(db, assignee_id)
    if person is None or person.club_id != club_id:
        raise bad_request("invalid_assignee", "Tasks can only be assigned to people in this club")


async def _flush_unique(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "crew_tasks_event_title_key" in str(exc.orig):
            raise conflict("duplicate_task", "This event already has a task with that title") from exc
        raise


async def _save_shift(db: AsyncSession, task: CrewTask, shift: tuple[datetime, datetime] | None) -> None:
    current = await db.get(CrewTaskShift, task.id) if modules.migrated("crew_shifts") else None
    if shift is None:
        if current is not None:
            await db.delete(current)
        return
    if not modules.migrated("crew_shifts"):
        raise AppError(404, "module_unavailable", "Task shifts are not switched on in this deployment")
    if current is None:
        db.add(CrewTaskShift(task_id=task.id, club_id=task.club_id, starts_at=shift[0], ends_at=shift[1]))
    else:
        current.starts_at, current.ends_at = shift


async def _review_assignment(db: AsyncSession, user: CurrentUser, event: Event, *, assignee_id: uuid.UUID,
                             title: str, shift, override_reason: str | None, task_id: uuid.UUID | None):
    """Duplicate and clash checks (assignment_review). Returns the flagged issues an override accepted."""
    result = await assignment_review.enforce(
        db, club_id=user.club_id, event=event, assignee_id=assignee_id, title=title, shift=shift,
        override_reason=override_reason, exclude_task_id=task_id)
    return result.issues


def _record_override(db: AsyncSession, user: CurrentUser, task: CrewTask, issues, reason: str) -> None:
    _log(db, task, user, "updated", note=f"Assigned despite review ({', '.join(sorted({i.code for i in issues}))}): {reason}")
    audit_service.record(
        db, club_id=task.club_id, actor_id=user.id, action="crewpulse.assignment_override", entity_type="crew_task",
        entity_id=task.id,
        details={"assignee_id": str(task.assignee_id), "reason": reason,
                 "issues": [{"code": i.code, "task_id": str(i.task_id) if i.task_id else None} for i in issues]},
    )


async def _load(db: AsyncSession, user: CurrentUser, task_id: uuid.UUID) -> tuple[CrewTask, Event]:
    task = await db.get(CrewTask, task_id)
    if task is None or task.club_id != user.club_id:
        raise not_found("Task")
    event = await db.get(Event, task.event_id)
    if event is None or not event_service.can_view(user, event):
        raise not_found("Task")
    return task, event


async def to_out(db: AsyncSession, user: CurrentUser, tasks: list[CrewTask], now: datetime | None = None) -> list[TaskOut]:
    now = now or utcnow()
    people = await shared_directory.get_users(db, [t.assignee_id for t in tasks])
    events = {e.id: e for e in await db.scalars(select(Event).where(Event.id.in_({t.event_id for t in tasks})))} if tasks else {}
    shifts = await assignment_review.shifts_for(db, [t.id for t in tasks])
    result = []
    for task in tasks:
        event = events[task.event_id]
        organizer = event_service.can_manage(user, event)
        result.append(TaskOut.model_validate(task).model_copy(update={
            "event_name": event.name,
            "event_starts_at": event.starts_at,
            "assignee_name": people[task.assignee_id].full_name if task.assignee_id in people else None,
            "risks": autopilot.task_risks(event_service.task_snapshot(task), event.starts_at, now),
            "shift_starts_at": shifts[task.id][0] if task.id in shifts else None,
            "shift_ends_at": shifts[task.id][1] if task.id in shifts else None,
            "can_edit": organizer and event.status in OPEN_EVENT_STATUSES,
            "can_update_status": (organizer or task.assignee_id == user.id) and event.status in OPEN_EVENT_STATUSES,
        }))
    return result


async def list_for_event(db: AsyncSession, event: Event) -> list[CrewTask]:
    stmt = (select(CrewTask).where(CrewTask.event_id == event.id)
            .order_by(CrewTask.is_critical.desc(), CrewTask.due_at.asc().nulls_last(), CrewTask.created_at))
    return list(await db.scalars(stmt))


async def list_mine(db: AsyncSession, user: CurrentUser) -> list[CrewTask]:
    stmt = (select(CrewTask).join(Event, Event.id == CrewTask.event_id)
            .where(CrewTask.assignee_id == user.id, CrewTask.club_id == user.club_id,
                   Event.status.in_(OPEN_EVENT_STATUSES))
            .order_by((CrewTask.status == "done"), CrewTask.due_at.asc().nulls_last(), CrewTask.created_at))
    return list(await db.scalars(stmt))


async def assignable_people(db: AsyncSession, club_id: uuid.UUID) -> list[AssigneeOut]:
    users: list[User] = await shared_directory.list_users(db, club_id)
    return [AssigneeOut(id=u.id, full_name=u.full_name, role=u.role) for u in users]


async def activity(db: AsyncSession, user: CurrentUser, task_id: uuid.UUID) -> list[TaskActivityOut]:
    await _load(db, user, task_id)
    rows = list(await db.scalars(select(CrewTaskActivity).where(CrewTaskActivity.task_id == task_id)
                                 .order_by(CrewTaskActivity.created_at)))
    names = await shared_directory.get_users(db, [r.actor_id for r in rows])
    return [TaskActivityOut.model_validate(r).model_copy(
        update={"actor_name": names[r.actor_id].full_name if r.actor_id in names else None}) for r in rows]


async def create_task(
    db: AsyncSession, user: CurrentUser, event: Event, data: TaskCreate, now: datetime | None = None
) -> CrewTask:
    now = now or utcnow()
    if not event_service.can_manage(user, event):
        raise forbidden("Only the president or this event's lead can add tasks")
    _ensure_open(event)
    if data.due_at and data.due_at <= now:
        raise bad_request("due_in_past", "The deadline must be in the future")
    shift = (data.shift_starts_at, data.shift_ends_at) if data.shift_starts_at else None
    overridden = []
    if data.assignee_id:
        await _check_assignee(db, user.club_id, data.assignee_id)
        overridden = await _review_assignment(db, user, event, assignee_id=data.assignee_id, title=data.title,
                                              shift=shift, override_reason=data.override_reason, task_id=None)

    fields = data.model_dump(exclude={"shift_starts_at", "shift_ends_at", "override_reason"})
    task = CrewTask(club_id=user.club_id, event_id=event.id, created_by=user.id, status="todo", **fields)
    db.add(task)
    await _flush_unique(db)
    await _save_shift(db, task, shift)
    _log(db, task, user, "created", new=task.title)
    if task.assignee_id:
        _log(db, task, user, "assigned", new=task.assignee_id)
    if overridden:
        _record_override(db, user, task, overridden, data.override_reason)
    await db.flush()
    return task


async def update_task(
    db: AsyncSession, user: CurrentUser, task_id: uuid.UUID, data: TaskUpdate, now: datetime | None = None
) -> CrewTask:
    now = now or utcnow()
    task, event = await _load(db, user, task_id)
    if not event_service.can_manage(user, event):
        raise forbidden("Only the president or this event's lead can edit or assign tasks")
    _ensure_open(event)
    changes = data.model_dump(exclude_unset=True)
    override_reason = changes.pop("override_reason", None)
    clear_shift = changes.pop("clear_shift", False)
    new_shift_start, new_shift_end = changes.pop("shift_starts_at", None), changes.pop("shift_ends_at", None)
    old_shift = await assignment_review.shift_of(db, task.id)
    shift = None if clear_shift else (new_shift_start, new_shift_end) if new_shift_start else old_shift
    if changes.pop("unassign", False):
        changes["assignee_id"] = None
    if "due_at" in changes and changes["due_at"] and changes["due_at"] != task.due_at and changes["due_at"] <= now:
        raise bad_request("due_in_past", "The deadline must be in the future")
    if changes.get("assignee_id"):
        await _check_assignee(db, user.club_id, changes["assignee_id"])

    # Review whenever the result is an assignment that is new or changed in what or when.
    assignee = changes["assignee_id"] if "assignee_id" in changes else task.assignee_id
    title = changes.get("title") or task.title
    overridden = []
    if assignee and (assignee != task.assignee_id or title != task.title or shift != old_shift):
        overridden = await _review_assignment(db, user, event, assignee_id=assignee, title=title, shift=shift,
                                              override_reason=override_reason, task_id=task.id)
    if shift != old_shift:
        await _save_shift(db, task, shift)
        _log(db, task, user, "updated", note="shift")

    if "assignee_id" in changes and changes["assignee_id"] != task.assignee_id:
        _log(db, task, user, "assigned", task.assignee_id, changes["assignee_id"])
    edited = [k for k, v in changes.items() if k != "assignee_id" and v is not None and v != getattr(task, k)]
    for key, value in changes.items():
        if key == "assignee_id" or value is not None:
            setattr(task, key, value)
    if edited:
        _log(db, task, user, "updated", note=", ".join(edited))
    await _flush_unique(db)
    if overridden:
        _record_override(db, user, task, overridden, override_reason)
        await db.flush()
    return task


async def set_status(
    db: AsyncSession, user: CurrentUser, task_id: uuid.UUID, data: TaskStatusChange, now: datetime | None = None
) -> CrewTask:
    now = now or utcnow()
    task, event = await _load(db, user, task_id)
    if not (event_service.can_manage(user, event) or task.assignee_id == user.id):
        raise forbidden("Only the assignee or the event's organizers can update this task")
    _ensure_open(event)
    if data.status == task.status and data.blocker_note == task.blocker_note:
        return task  # repeated submit: nothing changed

    previous = task.status
    task.status = data.status
    task.blocker_note = data.blocker_note if data.status == "blocked" else None
    task.completed_at = now if data.status == "done" else None
    await db.flush()
    _log(db, task, user, "status_changed", previous, data.status, data.blocker_note)
    return task
