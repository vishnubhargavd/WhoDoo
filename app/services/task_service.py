"""CrewPulse: volunteer tasks for an event.

Who can do what:
- The president and the event's lead create, edit and assign tasks (organizers).
- The assignee can move their own task between statuses and report a blocker.
- Anyone in the club who can see the event can see its tasks.
Tasks of cancelled or completed events are frozen.
"""

import uuid
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.models.crewpulse import CrewTask, CrewTaskActivity, CrewTaskClashOverride
from app.models.shared import Event, User
from app.schemas.crewpulse import (
    AssigneeOut,
    ClashCheckOut,
    ClashCheckRequest,
    TaskActivityOut,
    TaskCreate,
    TaskOut,
    TaskStatusChange,
    TaskUpdate,
)
from app.services import autopilot, event_service, shared_directory
from app.services.event_service import utcnow

OPEN_EVENT_STATUSES = ("draft", "published")


def _are_near_duplicates(t1: str, t2: str) -> bool:
    s1 = t1.strip().lower()
    s2 = t2.strip().lower()
    if s1 == s2:
        return False
    w1 = set(s1.split())
    w2 = set(s2.split())
    if w1 and w2:
        overlap = len(w1 & w2)
        union = len(w1 | w2)
        if overlap / union >= 0.6:
            return True
    return SequenceMatcher(None, s1, s2).ratio() >= 0.75


async def check_volunteer_clashes(
    db: AsyncSession,
    user: CurrentUser,
    event: Event,
    assignee_id: uuid.UUID | None,
    candidate_title: str,
    starts_at: datetime | None,
    ends_at: datetime | None,
    exclude_task_id: uuid.UUID | None = None,
) -> tuple[list[str], bool, list[dict]]:
    """
    Evaluates:
      1. Exact duplicate assignments for the volunteer in this event.
      2. Duplicate and near-duplicate task titles within the event.
      3. Overlapping duty times for the same volunteer (same event).
      4. Cross-event duty conflicts for the same volunteer (preserving privacy across clubs).
    """
    clashes: list[str] = []
    is_exact_duplicate = False
    clash_details: list[dict] = []

    # 1. Exact duplicate assignment & near-duplicate task titles in the same event
    event_tasks_stmt = select(CrewTask).where(
        CrewTask.event_id == event.id,
        CrewTask.status != "done",
    )
    if exclude_task_id:
        event_tasks_stmt = event_tasks_stmt.where(CrewTask.id != exclude_task_id)
    event_tasks = list(await db.scalars(event_tasks_stmt))

    cand_title_norm = candidate_title.strip().lower()

    for t in event_tasks:
        other_title_norm = t.title.strip().lower()

        # Exact duplicate assignment: same volunteer assigned to identical task title
        if assignee_id and t.assignee_id == assignee_id and other_title_norm == cand_title_norm:
            is_exact_duplicate = True
            msg = f"Exact duplicate assignment: volunteer is already assigned to identical task '{t.title}' in this event"
            clashes.append(msg)
            clash_details.append({
                "type": "exact_duplicate_assignment",
                "task_id": str(t.id),
                "title": t.title,
                "message": msg,
            })

        # Near-duplicate task titles in the same event
        if _are_near_duplicates(candidate_title, t.title):
            msg = f"Near-duplicate task in this event: '{t.title}' warrants organizer review"
            clashes.append(msg)
            clash_details.append({
                "type": "near_duplicate_task",
                "task_id": str(t.id),
                "title": t.title,
                "message": msg,
            })

    if not assignee_id:
        return clashes, is_exact_duplicate, clash_details

    # Concurrency safe lock on volunteer user row to prevent race conditions during assignment
    await db.execute(select(User.id).where(User.id == assignee_id).with_for_update())

    # 2. Overlapping scheduled duties (same event and cross-event)
    volunteer_tasks_stmt = select(CrewTask).where(
        CrewTask.assignee_id == assignee_id,
        CrewTask.status != "done",
    )
    if exclude_task_id:
        volunteer_tasks_stmt = volunteer_tasks_stmt.where(CrewTask.id != exclude_task_id)
    vol_tasks = list(await db.scalars(volunteer_tasks_stmt))

    if starts_at and ends_at:
        for t in vol_tasks:
            if t.starts_at and t.ends_at:
                # Duty time interval overlap: candidate.starts_at < t.ends_at AND candidate.ends_at > t.starts_at
                if starts_at < t.ends_at and ends_at > t.starts_at:
                    start_str = t.starts_at.strftime("%H:%M")
                    end_str = t.ends_at.strftime("%H:%M")
                    if t.event_id == event.id:
                        msg = f"Conflicting duty within this event: overlaps with '{t.title}' ({start_str} - {end_str})"
                        clashes.append(msg)
                        clash_details.append({
                            "type": "same_event_duty_overlap",
                            "task_id": str(t.id),
                            "title": t.title,
                            "starts_at": t.starts_at.isoformat(),
                            "ends_at": t.ends_at.isoformat(),
                            "message": msg,
                        })
                    else:
                        if t.club_id == user.club_id:
                            other_event = await db.get(Event, t.event_id)
                            event_label = other_event.name if other_event else "another event"
                            msg = f"Cross-event scheduling conflict: volunteer is already scheduled for '{t.title}' in '{event_label}' ({start_str} - {end_str})"
                        else:
                            # Protect privacy across clubs: mask specific event and task names
                            msg = f"Cross-club scheduling conflict: volunteer has another duty scheduled at this time ({start_str} - {end_str})"
                        clashes.append(msg)
                        clash_details.append({
                            "type": "cross_event_schedule_clash",
                            "task_id": str(t.id),
                            "is_cross_club": (t.club_id != user.club_id),
                            "starts_at": t.starts_at.isoformat(),
                            "ends_at": t.ends_at.isoformat(),
                            "message": msg,
                        })

    return clashes, is_exact_duplicate, clash_details


async def preview_clashes(
    db: AsyncSession, user: CurrentUser, event: Event, req: ClashCheckRequest
) -> ClashCheckOut:
    if not event_service.can_manage(user, event):
        raise forbidden("Only the president or this event's lead can review task clashes")
    if req.assignee_id:
        await _check_assignee(db, user.club_id, req.assignee_id)
    clashes, is_exact_dup, details = await check_volunteer_clashes(
        db, user, event, req.assignee_id, req.title or "", req.starts_at, req.ends_at, exclude_task_id=req.task_id
    )
    return ClashCheckOut(
        has_clashes=bool(clashes),
        is_exact_duplicate=is_exact_dup,
        can_override=bool(clashes and not is_exact_dup),
        clashes=clashes,
        clash_details=details,
    )


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
    overrides_stmt = select(CrewTaskClashOverride).where(CrewTaskClashOverride.task_id.in_({t.id for t in tasks})) if tasks else None
    overrides = {o.task_id: o for o in await db.scalars(overrides_stmt)} if overrides_stmt is not None else {}

    result = []
    for task in tasks:
        event = events[task.event_id]
        organizer = event_service.can_manage(user, event)
        task_clashes: list[str] = []
        if task.id in overrides:
            task_clashes.append(f"Override: {overrides[task.id].override_reason} ({overrides[task.id].clash_summary})")

        result.append(TaskOut.model_validate(task).model_copy(update={
            "event_name": event.name,
            "event_starts_at": event.starts_at,
            "assignee_name": people[task.assignee_id].full_name if task.assignee_id in people else None,
            "risks": autopilot.task_risks(event_service.task_snapshot(task), event.starts_at, now),
            "clashes": task_clashes,
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
    if data.assignee_id:
        await _check_assignee(db, user.club_id, data.assignee_id)

    # Server-side clash and duplicate checks
    clashes, is_exact_dup, details = await check_volunteer_clashes(
        db, user, event, data.assignee_id, data.title, data.starts_at, data.ends_at
    )
    if clashes:
        if is_exact_dup:
            raise conflict("exact_duplicate_assignment", "Cannot assign volunteer: exact duplicate assignment cannot be overridden")
        if not data.override_clash:
            raise conflict("volunteer_clash", f"Volunteer has scheduling conflicts or duplicates: {'; '.join(clashes)}")
        if not data.override_reason or len(data.override_reason.strip()) < 5:
            raise bad_request("missing_override_reason", "A valid reason (at least 5 characters) is required to override volunteer clashes")

    data_dict = data.model_dump(exclude={"override_clash", "override_reason"})
    task = CrewTask(club_id=user.club_id, event_id=event.id, created_by=user.id, status="todo", **data_dict)
    db.add(task)
    await _flush_unique(db)

    _log(db, task, user, "created", new=task.title)
    if task.assignee_id:
        if data.override_clash and clashes:
            override_rec = CrewTaskClashOverride(
                task_id=task.id,
                event_id=event.id,
                club_id=user.club_id,
                volunteer_id=task.assignee_id,
                overridden_by=user.id,
                clash_summary="; ".join(clashes),
                override_reason=data.override_reason.strip(),
            )
            db.add(override_rec)
            _log(db, task, user, "assigned", new=task.assignee_id, note=f"OVERRIDE: {data.override_reason.strip()}")
        else:
            _log(db, task, user, "assigned", new=task.assignee_id)
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
    override_clash = changes.pop("override_clash", False)
    override_reason = changes.pop("override_reason", None)
    if changes.pop("unassign", False):
        changes["assignee_id"] = None
    if "due_at" in changes and changes["due_at"] and changes["due_at"] != task.due_at and changes["due_at"] <= now:
        raise bad_request("due_in_past", "The deadline must be in the future")

    target_assignee_id = changes.get("assignee_id", task.assignee_id)
    if target_assignee_id:
        await _check_assignee(db, user.club_id, target_assignee_id)

    target_title = changes.get("title", task.title)
    target_starts_at = changes.get("starts_at", task.starts_at)
    target_ends_at = changes.get("ends_at", task.ends_at)

    clashes, is_exact_dup, details = await check_volunteer_clashes(
        db, user, event, target_assignee_id, target_title, target_starts_at, target_ends_at, exclude_task_id=task.id
    )
    if clashes:
        if is_exact_dup:
            raise conflict("exact_duplicate_assignment", "Cannot assign volunteer: exact duplicate assignment cannot be overridden")
        if not override_clash:
            raise conflict("volunteer_clash", f"Volunteer has scheduling conflicts or duplicates: {'; '.join(clashes)}")
        if not override_reason or len(override_reason.strip()) < 5:
            raise bad_request("missing_override_reason", "A valid reason (at least 5 characters) is required to override volunteer clashes")

        override_rec = CrewTaskClashOverride(
            task_id=task.id,
            event_id=event.id,
            club_id=user.club_id,
            volunteer_id=target_assignee_id,
            overridden_by=user.id,
            clash_summary="; ".join(clashes),
            override_reason=override_reason.strip(),
        )
        db.add(override_rec)
        _log(db, task, user, "assigned", old=task.assignee_id, new=target_assignee_id, note=f"OVERRIDE: {override_reason.strip()}")

    if "assignee_id" in changes and changes["assignee_id"] != task.assignee_id and not (override_clash and clashes):
        _log(db, task, user, "assigned", task.assignee_id, changes["assignee_id"])

    edited = [k for k, v in changes.items() if k != "assignee_id" and v is not None and v != getattr(task, k)]
    for key, value in changes.items():
        if key == "assignee_id" or value is not None:
            setattr(task, key, value)
    if edited:
        _log(db, task, user, "updated", note=", ".join(edited))
    await _flush_unique(db)
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
