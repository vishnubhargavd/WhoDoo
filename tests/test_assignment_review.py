"""Batch B (Member 2): CrewPulse duplicate and clash review before a volunteer is assigned."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.models.crewpulse import CrewTask, CrewTaskActivity
from app.models.shared import Event
from app.services import assignment_review, modules

API = "/api/v1"
DAY = (datetime.now(timezone.utc) + timedelta(days=10)).replace(hour=4, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _fresh():
    modules.forget()


def code(response) -> str:
    return response.json()["error"]["code"]


def at(hours: float) -> str:
    return (DAY + timedelta(hours=hours)).isoformat()


@pytest.fixture
async def events(db, world):
    """Two published events led by Leo Lead: a gala (04:00-10:00 UTC) and a workshop that overlaps it."""
    def make(name, start, end):
        return Event(id=uuid.uuid4(), club_id=world.club.id, name=name, lead_id=world.lead.id, status="published",
                     starts_at=DAY + timedelta(hours=start), ends_at=DAY + timedelta(hours=end), created_by=world.lead.id)
    gala, workshop, later = make("Gala", 0, 6), make("Workshop", 5, 8), make("Later Meetup", 30, 32)
    db.add_all([gala, workshop, later])
    await db.commit()
    return SimpleNamespace(gala=gala, workshop=workshop, later=later)


async def add(client, auth, user, event, title, assignee=None, **extra):
    body = {"title": title, **({"assignee_id": str(assignee.id)} if assignee else {}), **extra}
    return await client.post(f"{API}/events/{event.id}/tasks", headers=auth(user), json=body)


async def review(client, auth, user, event, title, assignee, **extra):
    body = {"title": title, "assignee_id": str(assignee.id), **extra}
    return await client.post(f"{API}/events/{event.id}/tasks/review", headers=auth(user), json=body)


def test_titles_that_are_nearly_the_same():
    assert assignment_review.similar("Book DJ", "Book the DJ")
    assert assignment_review.similar("Set up stage", "Setup stage")
    assert assignment_review.similar("Print posters!", "print  posters")
    assert not assignment_review.similar("Book the DJ", "Buy snacks")


async def test_exact_duplicate_is_blocked_even_with_an_override(client, auth, world, events):
    first = await add(client, auth, world.lead, events.gala, "Book the DJ", world.volunteer)
    assert first.status_code == 201, first.text
    found = (await review(client, auth, world.lead, events.gala, "book the dj", world.volunteer)).json()
    assert (found["ok"], found["can_override"], found["issues"][0]["code"]) == (False, False, "exact_duplicate")
    again = await add(client, auth, world.lead, events.gala, "Book the DJ", world.volunteer,
                      override_reason="I really mean it")
    assert (again.status_code, code(again)) == (409, "duplicate_assignment")


async def test_near_duplicate_needs_review_then_a_recorded_override(client, db, auth, world, events):
    await add(client, auth, world.lead, events.gala, "Book the DJ", world.volunteer)
    found = (await review(client, auth, world.lead, events.gala, "Book DJ", world.volunteer)).json()
    assert (found["ok"], found["can_override"]) == (False, True)
    assert [i["code"] for i in found["issues"]] == ["near_duplicate"]
    assert world.volunteer.id not in {uuid.UUID(s["id"]) for s in found["suggestions"]}
    assert found["suggestions"], "someone free should be suggested"

    refused = await add(client, auth, world.lead, events.gala, "Book DJ", world.volunteer)
    assert (refused.status_code, code(refused)) == (409, "assignment_conflicts")
    assert refused.json()["error"]["details"]["issues"][0]["code"] == "near_duplicate"
    short = await add(client, auth, world.lead, events.gala, "Book DJ", world.volunteer, override_reason="ok")
    assert short.status_code == 422                                   # a reason is required, not a token

    saved = await add(client, auth, world.lead, events.gala, "Book DJ", world.volunteer,
                      override_reason="Backup DJ booking, a different vendor")
    assert saved.status_code == 201, saved.text
    task_id = uuid.UUID(saved.json()["id"])
    audit = await db.scalar(text("SELECT details::text FROM audit_logs WHERE action = 'crewpulse.assignment_override'"))
    assert "Backup DJ booking" in audit and "near_duplicate" in audit
    notes = list(await db.scalars(select(CrewTaskActivity.note).where(CrewTaskActivity.task_id == task_id)))
    assert any(n and "Assigned despite review" in n for n in notes)
    # Nothing was deleted or moved: both tasks are still there, with their owner.
    owners = list(await db.scalars(select(CrewTask.assignee_id).where(CrewTask.event_id == events.gala.id)))
    assert owners == [world.volunteer.id, world.volunteer.id]


async def test_overlapping_shifts_in_one_event_are_flagged_and_separate_ones_are_not(client, auth, world, events):
    ok = await add(client, auth, world.lead, events.gala, "Registration desk", world.volunteer,
                   shift_starts_at=at(0), shift_ends_at=at(2))
    assert ok.status_code == 201, ok.text
    assert ok.json()["shift_starts_at"] is not None
    clash = await add(client, auth, world.lead, events.gala, "Stage crew", world.volunteer,
                      shift_starts_at=at(1), shift_ends_at=at(3))
    assert (clash.status_code, code(clash)) == (409, "assignment_conflicts")
    assert "overlaps" in clash.json()["error"]["message"]
    later = await add(client, auth, world.lead, events.gala, "Stage crew", world.volunteer,
                      shift_starts_at=at(2), shift_ends_at=at(4))         # back to back is fine
    assert later.status_code == 201, later.text
    bad = await add(client, auth, world.lead, events.gala, "Clean up", world.volunteer,
                    shift_starts_at=at(5), shift_ends_at=at(4))
    assert bad.status_code == 422


async def test_duties_at_the_same_time_in_two_events_clash(client, auth, world, events):
    await add(client, auth, world.lead, events.gala, "Ushering", world.volunteer)
    clash = (await review(client, auth, world.lead, events.workshop, "Projector setup", world.volunteer)).json()
    assert [i["code"] for i in clash["issues"]] == ["event_clash"]
    assert clash["issues"][0]["event_name"] == "Gala"
    free = (await review(client, auth, world.lead, events.later, "Projector setup", world.volunteer)).json()
    assert free["ok"] is True
    # A shift outside the other event's time clears the clash.
    shifted = (await review(client, auth, world.lead, events.workshop, "Projector setup", world.volunteer,
                            shift_starts_at=at(6.5), shift_ends_at=at(7.5))).json()
    assert shifted["ok"] is True


async def test_reassigning_is_reviewed_too(client, db, auth, world, events):
    await add(client, auth, world.lead, events.gala, "Ushering", world.volunteer)
    task = (await add(client, auth, world.lead, events.workshop, "Projector setup", world.member)).json()
    url = f"{API}/tasks/{task['id']}"
    moved = await client.patch(url, headers=auth(world.lead), json={"assignee_id": str(world.volunteer.id)})
    assert (moved.status_code, code(moved)) == (409, "assignment_conflicts")
    assert await db.scalar(select(CrewTask.assignee_id).where(CrewTask.id == uuid.UUID(task["id"]))) == world.member.id
    forced = await client.patch(url, headers=auth(world.lead), json={
        "assignee_id": str(world.volunteer.id), "override_reason": "Only one who knows the projector"})
    assert forced.status_code == 200, forced.text
    # Editing an unrelated field of an already-reviewed task does not ask again.
    edited = await client.patch(url, headers=auth(world.lead), json={"priority": "high"})
    assert edited.status_code == 200
    unassigned = await client.patch(url, headers=auth(world.lead), json={"unassign": True})
    assert unassigned.status_code == 200


async def test_only_organizers_review_and_finished_tasks_do_not_count(client, auth, world, events):
    assert (await review(client, auth, world.volunteer, events.gala, "Anything", world.volunteer)).status_code == 403
    assert (await review(client, auth, world.lead2, events.gala, "Anything", world.volunteer)).status_code == 403
    done = (await add(client, auth, world.lead, events.gala, "Ushering", world.volunteer)).json()
    await client.post(f"{API}/tasks/{done['id']}/status", headers=auth(world.volunteer), json={"status": "done"})
    assert (await review(client, auth, world.lead, events.workshop, "Projector", world.volunteer)).json()["ok"] is True


async def test_configured_open_task_limit(client, auth, world, events, monkeypatch):
    monkeypatch.setattr(get_settings(), "crew_max_open_tasks", 1)
    await add(client, auth, world.lead, events.later, "Chairs", world.volunteer)
    over = (await review(client, auth, world.lead, events.later, "Water bottles", world.volunteer)).json()
    assert [i["code"] for i in over["issues"]] == ["over_limit"]


async def test_two_organizers_assigning_at_once_cannot_both_slip_through(client, auth, world, events):
    """Each save re-checks under a per-person lock, so only one of two clashing assignments lands."""
    results = await asyncio.gather(
        add(client, auth, world.lead, events.gala, "Ticket scanning", world.volunteer,
            shift_starts_at=at(1), shift_ends_at=at(3)),
        add(client, auth, world.president, events.gala, "Photo booth", world.volunteer,
            shift_starts_at=at(2), shift_ends_at=at(4)),
    )
    assert sorted(r.status_code for r in results) == [201, 409]
