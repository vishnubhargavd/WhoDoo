"""CrewPulse assignment review: duplicate and clash checks before a volunteer gets a task.

What is flagged, against the person's unfinished tasks in events that are still open:

    exact_duplicate     the same task (same event, same title) already sits with them. Never
                        overridable: the second copy is simply wrong.
    near_duplicate      a task in the same event whose title is nearly the same ("Book DJ" and
                        "Book the DJ"). Likely a double entry.
    overlapping_shift   another task in the same event whose shift overlaps this one.
    event_clash         a task in a different event at an overlapping time. A task's time is
                        its shift, or else its event's start and end.
    over_limit          they would hold more than CREW_MAX_OPEN_TASKS unfinished tasks (if set).

Workflow. The organizer's page asks `review` first and shows what was found, with people who
are free at that time as suggestions. Saving runs the same checks again on the server, under a
per-person advisory lock so two organizers assigning the same volunteer at once are checked one
after the other. A flagged assignment is saved only with an organizer's override reason, which
is written to the audit log and to the task's activity trail. Nothing is ever deleted or moved
automatically.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import conflict
from app.models.crewpulse import CrewTask
from app.models.ledger import AuditLog
from app.models.member2 import CrewTaskShift
from app.models.shared import Event, User
from app.schemas.crewpulse import AssignmentIssue, AssignmentReviewOut, AssignmentSuggestion
from app.services import modules

OPEN_EVENTS = ("draft", "published")
NEAR_DUPLICATE_RATIO = 0.75
STOP_WORDS = {"the", "a", "an", "for", "to", "of", "and", "on", "at", "in"}


def normalise(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


def _words(title: str) -> set[str]:
    return {w for w in normalise(title).split() if w not in STOP_WORDS}


def similar(a: str, b: str) -> bool:
    """Nearly the same title: same words once filler is dropped, or a close spelling."""
    if normalise(a) == normalise(b):
        return True
    wa, wb = _words(a), _words(b)
    if wa and wa == wb:
        return True
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio() >= NEAR_DUPLICATE_RATIO


def overlaps(a: tuple[datetime, datetime] | None, b: tuple[datetime, datetime] | None) -> bool:
    return bool(a and b) and a[0] < b[1] and b[0] < a[1]


@dataclass
class _Held:
    task: CrewTask
    event: Event
    shift: tuple[datetime, datetime] | None

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        if self.shift:
            return self.shift
        if self.event.starts_at and self.event.ends_at:
            return self.event.starts_at, self.event.ends_at
        return None


async def _open_tasks(db: AsyncSession, club_id: uuid.UUID, exclude: uuid.UUID | None) -> list[_Held]:
    """Every unfinished, assigned task of the club in an open event, with its shift."""
    shifts = modules.migrated("crew_shifts")
    stmt = (
        select(CrewTask, Event, *( (CrewTaskShift.starts_at, CrewTaskShift.ends_at) if shifts else ()))
        .join(Event, Event.id == CrewTask.event_id)
        .where(CrewTask.club_id == club_id, CrewTask.assignee_id.isnot(None), CrewTask.status != "done",
               Event.status.in_(OPEN_EVENTS))
    )
    if shifts:
        stmt = stmt.outerjoin(CrewTaskShift, CrewTaskShift.task_id == CrewTask.id)
    if exclude is not None:
        stmt = stmt.where(CrewTask.id != exclude)
    held = []
    for row in (await db.execute(stmt)).all():
        shift = (row[2], row[3]) if shifts and row[2] is not None else None
        held.append(_Held(row[0], row[1], shift))
    return held


def _issues(held: list[_Held], *, assignee_id: uuid.UUID, event: Event, title: str,
            shift: tuple[datetime, datetime] | None) -> list[AssignmentIssue]:
    mine = [h for h in held if h.task.assignee_id == assignee_id]
    own_window = shift or ((event.starts_at, event.ends_at) if event.starts_at and event.ends_at else None)
    found: list[AssignmentIssue] = []
    for h in mine:
        where = dict(task_id=h.task.id, task_title=h.task.title, event_id=h.event.id, event_name=h.event.name)
        if h.event.id == event.id:
            if normalise(h.task.title) == normalise(title):
                found.append(AssignmentIssue(code="exact_duplicate", blocking=True,
                                             message=f"They already have “{h.task.title}” for this event.", **where))
                continue
            if similar(h.task.title, title):
                found.append(AssignmentIssue(code="near_duplicate", blocking=False,
                                             message=f"They already have a very similar task: “{h.task.title}”.", **where))
            if shift and h.shift and overlaps(shift, h.shift):
                found.append(AssignmentIssue(
                    code="overlapping_shift", blocking=False,
                    message=f"Its shift overlaps “{h.task.title}” ({_span(h.shift)}).", **where))
        elif overlaps(own_window, h.window):
            found.append(AssignmentIssue(
                code="event_clash", blocking=False,
                message=f"At the same time they are on “{h.task.title}” for {h.event.name} ({_span(h.window)}).", **where))
    limit = get_settings().crew_max_open_tasks
    if limit and len(mine) + 1 > limit:
        found.append(AssignmentIssue(code="over_limit", blocking=False,
                                     message=f"They would have {len(mine) + 1} unfinished tasks; the limit is {limit}."))
    return found


def _span(window: tuple[datetime, datetime] | None) -> str:
    from app.services.syncsphere import IST

    if not window:
        return "time not set"
    a, b = (w.astimezone(IST) for w in window)
    same_day = a.date() == b.date()
    return f"{a:%d %b %H:%M}–{b:%H:%M}" if same_day else f"{a:%d %b %H:%M} – {b:%d %b %H:%M}"


async def review(db: AsyncSession, *, club_id: uuid.UUID, event: Event, assignee_id: uuid.UUID, title: str,
                 shift: tuple[datetime, datetime] | None, exclude_task_id: uuid.UUID | None = None,
                 eligible: set[uuid.UUID] | None = None) -> AssignmentReviewOut:
    """`eligible` limits who may be suggested instead: the people the "Allocate task to" list
    offers (members and volunteers not attending the event). None means anyone in the club."""
    held = await _open_tasks(db, club_id, exclude_task_id)
    issues = _issues(held, assignee_id=assignee_id, event=event, title=title, shift=shift)
    suggestions: list[AssignmentSuggestion] = []
    if issues:
        people = [p for p in await db.scalars(select(User).where(User.club_id == club_id, User.id != assignee_id))
                  if eligible is None or p.id in eligible]
        load = {p.id: sum(1 for h in held if h.task.assignee_id == p.id) for p in people}
        free = [p for p in people
                if not _issues(held, assignee_id=p.id, event=event, title=title, shift=shift)]
        # Volunteers first, then the least busy.
        free.sort(key=lambda p: (p.role != "volunteer", load[p.id], p.full_name))
        suggestions = [AssignmentSuggestion(id=p.id, full_name=p.full_name, role=p.role, open_tasks=load[p.id])
                       for p in free[:3]]
    blocking = any(i.blocking for i in issues)
    return AssignmentReviewOut(ok=not issues, can_override=bool(issues) and not blocking, issues=issues,
                               suggestions=suggestions)


async def scan(db: AsyncSession, club_id: uuid.UUID, lead_id: uuid.UUID | None = None) -> list[dict]:
    """Every assignment in the club's open events that would be flagged if it were made now.

    Each unfinished task is checked against the same person's other unfinished tasks, with the same
    rules as `review`. `lead_id` keeps only tasks in events that person leads. Read-only: this is
    the standing list an organiser works through, not a gate."""
    held = await _open_tasks(db, club_id, None)
    people = {p.id: p for p in await db.scalars(select(User).where(User.club_id == club_id))}
    found = []
    for mine in held:
        if lead_id is not None and mine.event.lead_id != lead_id:
            continue
        others = [h for h in held if h.task.id != mine.task.id]
        issues = _issues(others, assignee_id=mine.task.assignee_id, event=mine.event, title=mine.task.title,
                         shift=mine.shift)
        # A pair is reported once, from the task created later (the one that caused it).
        issues = [i for i in issues if i.task_id is None or _later(mine.task, i.task_id, held)]
        if issues:
            person = people.get(mine.task.assignee_id)
            found.append({"task": mine.task, "event": mine.event, "person": person, "shift": mine.shift, "issues": issues})
    # Exact duplicates first, then by how soon the event is.
    found.sort(key=lambda row: (
        not any(i.blocking for i in row["issues"]),
        row["event"].starts_at.timestamp() if row["event"].starts_at else float("inf"),
        row["task"].title,
    ))
    return found


async def overrides(db: AsyncSession, club_id: uuid.UUID, limit: int = 20) -> list[dict]:
    """The latest assignments an organiser made despite a flag, with the reason they gave."""
    rows = (await db.execute(
        select(AuditLog, CrewTask.title, Event.name, Event.id)
        .join(CrewTask, CrewTask.id == AuditLog.entity_id)
        .join(Event, Event.id == CrewTask.event_id)
        .where(AuditLog.club_id == club_id, AuditLog.action == "crewpulse.assignment_override")
        .order_by(AuditLog.created_at.desc()).limit(limit)
    )).all()
    people = {p.id: p.full_name for p in await db.scalars(select(User).where(User.club_id == club_id))}
    return [
        {"when": log.created_at, "by": people.get(log.actor_id, "Someone"), "task": title, "event": event_name,
         "event_id": event_id, "reason": (log.details or {}).get("reason", ""),
         "codes": sorted({i.get("code", "") for i in (log.details or {}).get("issues", [])})}
        for log, title, event_name, event_id in rows
    ]


def _later(task: CrewTask, other_id: uuid.UUID, held: list[_Held]) -> bool:
    other = next((h.task for h in held if h.task.id == other_id), None)
    return other is None or (task.created_at, str(task.id)) > (other.created_at, str(other.id))


async def lock_person(db: AsyncSession, assignee_id: uuid.UUID) -> None:
    """Serialise assignments to one person for the rest of this transaction."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"crewpulse-assignee:{assignee_id}"})


async def enforce(db: AsyncSession, *, club_id: uuid.UUID, event: Event, assignee_id: uuid.UUID, title: str,
                  shift: tuple[datetime, datetime] | None, override_reason: str | None,
                  exclude_task_id: uuid.UUID | None = None) -> AssignmentReviewOut:
    """The server-side gate before an assignment is saved. Raises unless it is clean or overridden."""
    await lock_person(db, assignee_id)
    result = await review(db, club_id=club_id, event=event, assignee_id=assignee_id, title=title, shift=shift,
                          exclude_task_id=exclude_task_id)
    details = result.model_dump(mode="json")
    if any(i.blocking for i in result.issues):
        raise conflict("duplicate_assignment", result.issues[0].message + " An exact duplicate cannot be overridden.",
                       details)
    if result.issues and not override_reason:
        raise conflict("assignment_conflicts",
                       "Review before assigning: " + " ".join(i.message for i in result.issues), details)
    return result


async def shift_of(db: AsyncSession, task_id: uuid.UUID) -> tuple[datetime, datetime] | None:
    if not modules.migrated("crew_shifts"):
        return None
    row = await db.get(CrewTaskShift, task_id)
    return (row.starts_at, row.ends_at) if row else None


async def shifts_for(db: AsyncSession, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[datetime, datetime]]:
    if not task_ids or not modules.migrated("crew_shifts"):
        return {}
    rows = await db.scalars(select(CrewTaskShift).where(CrewTaskShift.task_id.in_(task_ids)))
    return {r.task_id: (r.starts_at, r.ends_at) for r in rows}

