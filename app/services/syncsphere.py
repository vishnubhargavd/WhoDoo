"""SyncSphere: schedule conflict detection for a club's events.

Pure functions over plain snapshots; no database access, and the current time is
always passed in. Every conflict and suggestion carries a plain-language reason.

Rules (stated, not inferred):
- Two events overlap when a.start < b.end and b.start < a.end. Back-to-back events
  (one ends exactly when the next starts) do not overlap.
- Cancelled events never conflict.
- Only events of the same club are compared. Venue and lead clashes are therefore
  only known within the club; nothing here claims a venue is free club-wide.
- An event without both a start and an end cannot be checked, and says so.
- Venues match when equal after trimming, lower-casing and collapsing spaces.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

# Asia/Kolkata has no daylight saving, so a fixed offset is exact and needs no tz database.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

DAY_START = time(9, 0)  # earliest suggested start, IST
DAY_END = time(21, 0)  # latest suggested end, IST
SLOT_STEP = timedelta(minutes=30)
BUFFER = timedelta(minutes=30)  # gap kept free before and after other events
SEARCH_DAYS = 7
MAX_SUGGESTIONS = 3

SEVERITY = {"venue": 3, "lead": 2, "time": 1}


@dataclass(frozen=True)
class ScheduledEvent:
    id: uuid.UUID
    name: str
    starts_at: datetime | None
    ends_at: datetime | None
    venue: str | None = None
    lead_id: uuid.UUID | None = None
    status: str = "published"

    @property
    def is_schedulable(self) -> bool:
        return self.starts_at is not None and self.ends_at is not None and self.ends_at > self.starts_at


@dataclass(frozen=True)
class Conflict:
    kind: str  # "venue" | "lead" | "time" — the most serious reason
    other_event_id: uuid.UUID
    other_event_name: str
    overlap_start: datetime
    overlap_end: datetime
    reasons: list[str]
    explanation: str


@dataclass(frozen=True)
class SlotSuggestion:
    starts_at: datetime
    ends_at: datetime
    explanation: str


@dataclass
class ScheduleReport:
    checked: bool
    conflicts: list[Conflict] = field(default_factory=list)
    unchecked_others: list[str] = field(default_factory=list)
    message: str = ""


def normalize_venue(venue: str | None) -> str | None:
    if not venue:
        return None
    cleaned = " ".join(venue.split()).lower()
    return cleaned or None


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def fmt_ist(value: datetime) -> str:
    return value.astimezone(IST).strftime("%d %b %Y, %H:%M")


def _comparable(target: ScheduledEvent, others: list[ScheduledEvent]) -> tuple[list[ScheduledEvent], list[str]]:
    usable, unchecked = [], []
    for other in others:
        if other.id == target.id or other.status == "cancelled":
            continue
        if other.is_schedulable:
            usable.append(other)
        else:
            unchecked.append(other.name)
    return usable, unchecked


def find_conflicts(target: ScheduledEvent, others: list[ScheduledEvent]) -> ScheduleReport:
    """Compare one event against the rest of the club's events."""
    if target.status == "cancelled":
        return ScheduleReport(checked=False, message="Cancelled events are not checked for conflicts.")
    if not target.is_schedulable:
        return ScheduleReport(
            checked=False, message="Add a start and end time to check this event for schedule conflicts."
        )

    usable, unchecked = _comparable(target, others)
    venue = normalize_venue(target.venue)
    conflicts = []
    for other in usable:
        if not overlaps(target.starts_at, target.ends_at, other.starts_at, other.ends_at):
            continue
        start, end = max(target.starts_at, other.starts_at), min(target.ends_at, other.ends_at)
        window = f"{fmt_ist(start)} – {end.astimezone(IST):%H:%M} IST"
        reasons = ["time"]
        parts = [f"Overlaps with “{other.name}” from {window}."]
        if venue and venue == normalize_venue(other.venue):
            reasons.append("venue")
            parts.append(f"Both are booked at {target.venue.strip()}.")
        if target.lead_id and target.lead_id == other.lead_id:
            reasons.append("lead")
            parts.append("The same person leads both events.")
        kind = max(reasons, key=SEVERITY.__getitem__)
        conflicts.append(Conflict(kind, other.id, other.name, start, end, reasons, " ".join(parts)))

    conflicts.sort(key=lambda c: (-SEVERITY[c.kind], c.overlap_start))
    message = "No overlapping events found in this club." if not conflicts else ""
    if unchecked:
        message = (message + " " if message else "") + (
            f"{len(unchecked)} other event(s) have no start or end time and could not be compared."
        )
    return ScheduleReport(checked=True, conflicts=conflicts, unchecked_others=unchecked, message=message)


def suggest_slots(
    target: ScheduledEvent, others: list[ScheduledEvent], now: datetime, *, limit: int = MAX_SUGGESTIONS
) -> tuple[list[SlotSuggestion], str]:
    """Alternative start times with no known clash, closest to the original start first.

    Candidates: same duration, starting on the half hour between 09:00 and 21:00 IST,
    from the original day through the next 7 days, in the future, and at least 30
    minutes clear of every other non-cancelled, scheduled event of the club.
    """
    if not target.is_schedulable:
        return [], "Add a start and end time first."
    duration = target.ends_at - target.starts_at
    window = datetime.combine(date.min, DAY_END) - datetime.combine(date.min, DAY_START)
    if duration > window:
        return [], "This event is longer than the 09:00–21:00 window, so no alternative slots are suggested."

    usable, unchecked = _comparable(target, others)
    busy = [(o.starts_at - BUFFER, o.ends_at + BUFFER) for o in usable]
    first_day = max(target.starts_at, now).astimezone(IST).date()
    candidates = []
    for offset in range(SEARCH_DAYS + 1):
        day = first_day + timedelta(days=offset)
        start = datetime.combine(day, DAY_START, IST)
        last_start = datetime.combine(day, DAY_END, IST) - duration
        while start <= last_start:
            end = start + duration
            if start > now and start != target.starts_at and not any(overlaps(start, end, b, e) for b, e in busy):
                candidates.append((start, end))
            start += SLOT_STEP

    candidates.sort(key=lambda slot: (abs(slot[0] - target.starts_at), slot[0]))
    suggestions = [
        SlotSuggestion(
            start, end,
            f"{fmt_ist(start)} – {end.astimezone(IST):%H:%M} IST keeps the same length and is at least "
            f"30 minutes clear of every other scheduled event in this club. This is not a venue booking.",
        )
        for start, end in candidates[:limit]
    ]
    note = "These are times with no known clash in this club's calendar."
    if unchecked:
        note += f" {len(unchecked)} event(s) without times were not considered."
    if not suggestions:
        note = "No clash-free slot found between 09:00 and 21:00 IST in the next 7 days."
    return suggestions, note
