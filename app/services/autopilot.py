"""Event Autopilot: an explainable readiness score for an upcoming event.

Deterministic rules over real event, task and schedule data. Each rule that fires
becomes a factor with the points it cost, the evidence behind it and a suggested
next action. Nothing here changes data; organizers decide what to do.

Score starts at 100; factors subtract points (each rule is capped); the result is
clamped to 0..100. Bands: 80+ on track, 50-79 needs attention, below 50 at risk;
any high-severity issue (e.g. a blocked task) also rules out "on track".
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services.syncsphere import IST, ScheduleReport

ACTIVE_STATUSES = ("draft", "published")
CRITICAL_WINDOW = timedelta(hours=48)
PROGRESS_WINDOW = timedelta(hours=72)
PUBLISH_WINDOW = timedelta(days=7)
NO_TASKS_WINDOW = timedelta(days=14)
PROGRESS_TARGET = 0.7
MIN_DESCRIPTION = 20


@dataclass(frozen=True)
class TaskSnapshot:
    id: uuid.UUID
    title: str
    status: str
    is_critical: bool = False
    assignee_id: uuid.UUID | None = None
    due_at: datetime | None = None

    @property
    def done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class EventSnapshot:
    id: uuid.UUID
    name: str
    status: str
    starts_at: datetime | None
    ends_at: datetime | None
    venue: str | None
    lead_id: uuid.UUID | None
    description: str | None


@dataclass(frozen=True)
class Factor:
    code: str
    severity: str  # "high" | "medium" | "low"
    points: int
    title: str
    explanation: str
    action: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class ReadinessReport:
    applicable: bool
    score: int | None
    band: str | None
    factors: list[Factor]
    summary: str
    stats: dict


def task_risks(task: TaskSnapshot, event_starts_at: datetime | None, now: datetime) -> list[str]:
    """Why a single task needs attention (empty list when it does not)."""
    if task.done:
        return []
    risks = []
    if task.status == "blocked":
        risks.append("blocked")
    if task.due_at and task.due_at < now:
        risks.append("overdue")
    if task.assignee_id is None:
        risks.append("unassigned")
    if task.is_critical and event_starts_at and event_starts_at - now <= CRITICAL_WINDOW:
        risks.append("critical_due_soon")
    return risks


def _capped(code, severity, each, cap, items, title, explanation, action) -> Factor | None:
    if not items:
        return None
    return Factor(code, severity, min(each * len(items), cap), title, explanation, action, items)


def _band(score: int) -> str:
    return "on_track" if score >= 80 else "needs_attention" if score >= 50 else "at_risk"


def assess(
    event: EventSnapshot, tasks: list[TaskSnapshot], schedule: ScheduleReport | None, now: datetime
) -> ReadinessReport:
    done = sum(task.done for task in tasks)
    stats = {"tasks_total": len(tasks), "tasks_done": done, "completion": round(done / len(tasks), 2) if tasks else None}

    if event.status not in ACTIVE_STATUSES:
        return ReadinessReport(False, None, None, [], f"Readiness is not tracked for {event.status} events.", stats)
    if event.ends_at and event.ends_at <= now:
        return ReadinessReport(False, None, None, [], "This event has already ended.", stats)

    time_left = event.starts_at - now if event.starts_at else None
    stats["hours_to_start"] = round(time_left.total_seconds() / 3600, 1) if time_left else None
    soon = lambda window: time_left is not None and time_left <= window  # noqa: E731
    open_tasks = [t for t in tasks if not t.done]
    factors: list[Factor | None] = []

    # Missing event information
    if not event.lead_id:
        factors.append(Factor("missing_lead", "high", 15, "No event lead",
            "Nobody is accountable for this event.", "Assign an event lead."))
    if not event.venue:
        factors.append(Factor("missing_venue", "medium", 10, "No venue",
            "Attendees and volunteers do not know where to go.", "Add the venue."))
    if not event.starts_at or not event.ends_at:
        factors.append(Factor("missing_schedule", "medium", 10, "Start or end time missing",
            "Without both times the event cannot be checked for clashes or deadlines.", "Set the start and end time."))
    if len((event.description or "").strip()) < MIN_DESCRIPTION:
        factors.append(Factor("missing_description", "low", 5, "Description is too short",
            f"The description has fewer than {MIN_DESCRIPTION} characters.", "Describe what the event is and who it is for."))
    if event.status == "draft" and soon(PUBLISH_WINDOW):
        factors.append(Factor("not_published", "medium", 10, "Still a draft",
            "The event starts within 7 days but members cannot see it yet.", "Publish the event once details are final."))

    # Volunteer work
    if not tasks and soon(NO_TASKS_WINDOW):
        factors.append(Factor("no_tasks", "high", 20, "No tasks planned",
            "The event starts within 14 days and has no volunteer tasks.", "Break the work into tasks and assign them."))
    if soon(CRITICAL_WINDOW):
        factors.append(_capped("critical_incomplete", "high", 15, 30,
            [t.title for t in open_tasks if t.is_critical], "Critical tasks unfinished",
            "These critical tasks are not done and the event starts within 48 hours.",
            "Check in with the owners today or reassign the work."))
    factors.append(_capped("overdue_tasks", "high", 8, 24,
        [f"{t.title} (due {t.due_at.astimezone(IST):%d %b %H:%M})" for t in open_tasks if t.due_at and t.due_at < now],
        "Overdue tasks", "These tasks are past their deadline and not done.",
        "Ask the owners for an update and set a realistic new deadline."))
    factors.append(_capped("blocked_tasks", "high", 10, 20,
        [t.title for t in open_tasks if t.status == "blocked"], "Blocked tasks",
        "A volunteer reported a blocker on these tasks.", "Read the blocker note and remove the obstacle."))
    factors.append(_capped("unassigned_tasks", "medium", 5, 15,
        [t.title for t in open_tasks if t.assignee_id is None], "Unassigned tasks",
        "Nobody owns these tasks.", "Assign each task to a volunteer."))
    if tasks and soon(PROGRESS_WINDOW) and done / len(tasks) < PROGRESS_TARGET:
        factors.append(Factor("low_progress", "medium", 10, "Behind on tasks",
            f"Only {done} of {len(tasks)} tasks are done with under 72 hours to go (target {PROGRESS_TARGET:.0%}).",
            "Focus volunteers on the remaining tasks.", [f"{done}/{len(tasks)} done"]))

    # Schedule
    if schedule and schedule.checked:
        by_kind = {kind: [c for c in schedule.conflicts if c.kind == kind] for kind in ("venue", "lead", "time")}
        factors.append(_capped("venue_clash", "high", 20, 20, [c.explanation for c in by_kind["venue"]],
            "Venue double-booked", "Another event in this club uses the same venue at the same time.",
            "Move one event or change its venue (see SyncSphere suggestions)."))
        factors.append(_capped("lead_clash", "medium", 10, 10, [c.explanation for c in by_kind["lead"]],
            "Event lead double-booked", "The same lead is responsible for overlapping events.",
            "Hand one event to another lead or move it."))
        factors.append(_capped("time_overlap", "low", 5, 10, [c.explanation for c in by_kind["time"]],
            "Overlaps another club event", "Members may have to choose between two of your events.",
            "Consider one of the suggested alternative slots."))

    found = sorted((f for f in factors if f), key=lambda f: -f.points)
    score = max(0, 100 - sum(f.points for f in found))
    band = _band(score)
    if band == "on_track" and any(f.severity == "high" for f in found):
        band = "needs_attention"
    if not found:
        summary = "No risks found by the readiness rules."
    else:
        summary = f"{len(found)} issue(s) found. Biggest: {found[0].title.lower()}."
    return ReadinessReport(True, score, band, found, summary, stats)
