"""SyncSphere: overlap detection and alternative slots. Pure functions, no database."""

import uuid
from datetime import datetime, timedelta, timezone

from app.services.syncsphere import IST, ScheduledEvent, find_conflicts, normalize_venue, overlaps, suggest_slots

NOW = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def event(name, start, end, venue=None, lead=None, status="published") -> ScheduledEvent:
    return ScheduledEvent(uuid.uuid4(), name, start, end, venue, lead, status)


def test_overlap_is_half_open():
    a, b = ist(20, 10), ist(20, 12)
    assert overlaps(a, b, ist(20, 11), ist(20, 13))
    assert not overlaps(a, b, ist(20, 12), ist(20, 14))  # back to back
    assert not overlaps(a, b, ist(20, 8), ist(20, 10))
    assert overlaps(a, b, ist(20, 10, 30), ist(20, 11))  # contained


def test_overlap_across_utc_and_ist_offsets():
    # 23:00-01:00 IST on the 20th/21st is 17:30-19:30 UTC on the 20th.
    late = event("Night", ist(20, 23), ist(21, 1))
    utc_view = event("Same time in UTC", datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc),
                     datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc))
    report = find_conflicts(late, [utc_view])
    assert report.checked and len(report.conflicts) == 1


def test_no_conflict_and_back_to_back():
    target = event("Gala", ist(20, 18), ist(20, 21))
    report = find_conflicts(target, [event("Setup", ist(20, 15), ist(20, 18)), event("Other day", ist(21, 18), ist(21, 21))])
    assert report.checked and report.conflicts == []
    assert "No overlapping" in report.message


def test_venue_and_lead_clash_ranked_and_explained():
    lead = uuid.uuid4()
    target = event("Gala", ist(20, 18), ist(20, 21), venue="Main Hall", lead=lead)
    others = [
        event("Quiz", ist(20, 19), ist(20, 20)),
        event("Dance", ist(20, 20), ist(20, 22), venue="  main   HALL "),
        event("Talk", ist(20, 17), ist(20, 19), lead=lead),
    ]
    conflicts = find_conflicts(target, others).conflicts
    assert [c.kind for c in conflicts] == ["venue", "lead", "time"]
    assert "Main Hall" in conflicts[0].explanation or "main" in conflicts[0].explanation.lower()
    assert "same person leads" in conflicts[1].explanation
    assert conflicts[0].overlap_start == ist(20, 20) and conflicts[0].overlap_end == ist(20, 21)


def test_cancelled_and_self_are_ignored():
    target = event("Gala", ist(20, 18), ist(20, 21))
    report = find_conflicts(target, [target, event("Old", ist(20, 18), ist(20, 21), status="cancelled")])
    assert report.conflicts == []


def test_missing_times_are_reported_not_assumed_free():
    target = event("Gala", ist(20, 18), None)
    report = find_conflicts(target, [])
    assert not report.checked and "start and end" in report.message

    ok = event("Gala", ist(20, 18), ist(20, 21))
    report = find_conflicts(ok, [event("TBD", None, None)])
    assert report.checked and report.unchecked_others == ["TBD"]
    assert "could not be compared" in report.message


def test_cancelled_target_is_not_checked():
    assert not find_conflicts(event("X", ist(20, 18), ist(20, 21), status="cancelled"), []).checked


def test_normalize_venue():
    assert normalize_venue("  Main   Hall ") == "main hall"
    assert normalize_venue("   ") is None and normalize_venue(None) is None


def test_suggestions_avoid_busy_times_with_buffer():
    target = event("Gala", ist(20, 18), ist(20, 20))
    busy = event("Dance", ist(20, 15), ist(20, 21))
    slots, note = suggest_slots(target, [target, busy], NOW)
    assert len(slots) == 3
    for slot in slots:
        assert slot.ends_at - slot.starts_at == timedelta(hours=2)
        assert not overlaps(slot.starts_at, slot.ends_at, ist(20, 14, 30), ist(20, 21, 30))
        local_start, local_end = slot.starts_at.astimezone(IST), slot.ends_at.astimezone(IST)
        assert local_start.hour >= 9 and (local_end.hour, local_end.minute) <= (21, 0)
        assert "not a venue booking" in slot.explanation
    # Closest clash-free option on the same day: 12:30-14:30 ends exactly at the buffer edge.
    assert slots[0].starts_at == ist(20, 12, 30)
    assert "no known clash" in note


def test_suggestions_never_in_the_past():
    target = event("Soon", ist(10, 12), ist(10, 13))
    slots, _ = suggest_slots(target, [], NOW)
    assert slots and all(slot.starts_at > NOW for slot in slots)


def test_suggestions_need_times_and_a_fitting_duration():
    assert suggest_slots(event("X", None, None), [], NOW)[0] == []
    slots, note = suggest_slots(event("Marathon", ist(20, 6), ist(20, 22)), [], NOW)
    assert slots == [] and "longer than" in note
