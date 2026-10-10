"""Event Autopilot readiness rules. Pure functions, no database."""

import uuid
from datetime import datetime, timedelta, timezone

from app.services.autopilot import EventSnapshot, TaskSnapshot, assess, task_risks
from app.services.syncsphere import ScheduledEvent, find_conflicts

NOW = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
LEAD = uuid.uuid4()


def make_event(days_away=10, hours=3, **changes) -> EventSnapshot:
    start = NOW + timedelta(days=days_away)
    values = dict(id=uuid.uuid4(), name="Spring Gala", status="published", starts_at=start,
                  ends_at=start + timedelta(hours=hours), venue="Main Hall", lead_id=LEAD,
                  description="Our biggest event of the year, open to all students.")
    values.update(changes)
    return EventSnapshot(**values)


def task(title="Book DJ", status="todo", critical=False, assignee=LEAD, due=None) -> TaskSnapshot:
    return TaskSnapshot(uuid.uuid4(), title, status, critical, assignee, due)


def codes(report):
    return {f.code for f in report.factors}


def test_ready_event_scores_100():
    report = assess(make_event(), [task(status="done"), task("Posters")], None, NOW)
    assert report.applicable and report.score == 100 and report.band == "on_track"
    assert report.factors == [] and report.stats["completion"] == 0.5


def test_missing_information_is_explained():
    report = assess(make_event(lead_id=None, venue=None, description="Gala"), [task()], None, NOW)
    assert codes(report) == {"missing_lead", "missing_venue", "missing_description"}
    assert report.score == 70 and report.band == "needs_attention"
    assert all(f.explanation and f.action for f in report.factors)
    assert report.factors[0].code == "missing_lead"  # biggest first


def test_missing_schedule():
    report = assess(make_event(starts_at=None, ends_at=None), [], None, NOW)
    assert "missing_schedule" in codes(report) and "no_tasks" not in codes(report)


def test_no_tasks_only_counts_close_to_the_event():
    assert "no_tasks" not in codes(assess(make_event(days_away=30), [], None, NOW))
    assert "no_tasks" in codes(assess(make_event(days_away=5), [], None, NOW))


def test_draft_close_to_event():
    assert "not_published" in codes(assess(make_event(days_away=3, status="draft"), [task()], None, NOW))
    assert "not_published" not in codes(assess(make_event(days_away=20, status="draft"), [task()], None, NOW))


def test_overdue_blocked_unassigned_are_capped():
    late = NOW - timedelta(hours=1)
    tasks = [task(f"T{i}", due=late) for i in range(5)] + [task("Stuck", status="blocked", due=late)]
    tasks += [task(f"U{i}", assignee=None) for i in range(5)]
    report = assess(make_event(), tasks, None, NOW)
    by_code = {f.code: f for f in report.factors}
    assert by_code["overdue_tasks"].points == 24 and len(by_code["overdue_tasks"].evidence) == 6
    assert by_code["blocked_tasks"].points == 10 and by_code["blocked_tasks"].evidence == ["Stuck"]
    assert by_code["unassigned_tasks"].points == 15
    assert report.score == 51


def test_done_tasks_never_count_as_overdue():
    report = assess(make_event(), [task(status="done", due=NOW - timedelta(days=1))], None, NOW)
    assert report.score == 100


def test_critical_and_progress_only_inside_their_windows():
    tasks = [task("Sound check", critical=True), task("Chairs"), task("Posters", status="done")]
    far = assess(make_event(days_away=10), tasks, None, NOW)
    assert not {"critical_incomplete", "low_progress"} & codes(far)
    near = assess(make_event(days_away=1), tasks, None, NOW)
    assert {"critical_incomplete", "low_progress"} <= codes(near)


def test_schedule_conflicts_feed_the_score():
    ev = make_event()
    target = ScheduledEvent(ev.id, ev.name, ev.starts_at, ev.ends_at, ev.venue, ev.lead_id)
    other = ScheduledEvent(uuid.uuid4(), "Dance", ev.starts_at, ev.ends_at, "main hall", None)
    report = assess(ev, [task()], find_conflicts(target, [other]), NOW)
    assert "venue_clash" in codes(report) and report.score == 80


def test_not_applicable_for_past_cancelled_completed():
    assert not assess(make_event(status="cancelled"), [], None, NOW).applicable
    assert not assess(make_event(status="completed"), [], None, NOW).applicable
    past = make_event(days_away=-2)
    report = assess(past, [], None, NOW)
    assert not report.applicable and report.score is None and "ended" in report.summary


def test_score_never_below_zero():
    late = NOW - timedelta(hours=1)
    tasks = [task(f"T{i}", status="blocked", critical=True, assignee=None, due=late) for i in range(6)]
    report = assess(make_event(days_away=1, status="draft", lead_id=None, venue=None, description=""), tasks, None, NOW)
    assert report.score == 0 and report.band == "at_risk"


def test_task_risks():
    start = NOW + timedelta(hours=10)
    assert task_risks(task(status="done", assignee=None), start, NOW) == []
    risks = task_risks(task(status="blocked", critical=True, assignee=None, due=NOW - timedelta(minutes=1)), start, NOW)
    assert risks == ["blocked", "overdue", "unassigned", "critical_due_soon"]


def test_high_severity_issue_is_never_on_track():
    report = assess(make_event(), [task(status="blocked")], None, NOW)
    assert report.score == 90 and report.band == "needs_attention"
    minor = assess(make_event(), [task(assignee=None)], None, NOW)
    assert minor.score == 95 and minor.band == "on_track"
