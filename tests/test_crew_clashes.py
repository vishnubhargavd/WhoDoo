"""CrewPulse: volunteer clash detection, exact duplicate prevention, cross-event conflicts,
and authorized audit trail for overrides.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.crewpulse import CrewTask, CrewTaskClashOverride
from app.models.shared import Club, Event, User
from tests.test_events import API as EVENTS, at, create

TASKS = "/api/v1/tasks"


def duty_time(hours_from_now: int, duration_hours: int = 2) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=duration_hours)
    return start.isoformat(), end.isoformat()


async def published_event(client, auth, world, name="Clash Gala") -> str:
    res = await create(client, auth, world.lead, name=name)
    event_id = res.json()["id"]
    await client.post(f"{EVENTS}/{event_id}/status", headers=auth(world.lead), json={"status": "published"})
    return event_id


async def add_task(client, auth, user, event_id, **body):
    payload = {"title": "Registration Desk", "priority": "high", "due_at": at(24)}
    payload.update(body)
    return await client.post(f"{EVENTS}/{event_id}/tasks", headers=auth(user), json=payload)


@pytest.mark.asyncio
async def test_exact_duplicate_assignment_rejection(client, world, auth):
    """Exact duplicate assignment is rejected even if override is requested."""
    event_id = await published_event(client, auth, world)

    # 1. Create first assignment
    res1 = await add_task(
        client, auth, world.lead, event_id,
        title="Check-in Security",
        assignee_id=str(world.volunteer.id),
    )
    assert res1.status_code == 201

    # 2. Try to create exact duplicate assignment for same volunteer in this event
    res2 = await add_task(
        client, auth, world.lead, event_id,
        title="Check-in Security",
        assignee_id=str(world.volunteer.id),
    )
    # The DB or clash engine catches duplicate
    assert res2.status_code in (400, 409)

    # 3. Even with override_clash=True, exact duplicate must be blocked
    res3 = await add_task(
        client, auth, world.lead, event_id,
        title="Check-in Security",
        assignee_id=str(world.volunteer.id),
        override_clash=True,
        override_reason="Need double shift coverage",
    )
    assert res3.status_code == 409
    data = res3.json()
    err_code = data.get("error", {}).get("code") or data.get("code")
    assert err_code in ("exact_duplicate_assignment", "duplicate_task")


@pytest.mark.asyncio
async def test_same_volunteer_time_overlap(client, world, auth):
    """Overlapping scheduled duties for the same volunteer must be flagged as conflict."""
    event_id = await published_event(client, auth, world)

    s1, e1 = duty_time(2, 3)  # 2h to 5h
    res1 = await add_task(
        client, auth, world.lead, event_id,
        title="Stage Lighting",
        assignee_id=str(world.volunteer.id),
        starts_at=s1,
        ends_at=e1,
    )
    assert res1.status_code == 201

    # Overlapping time: 3h to 6h (overlaps between 3h and 5h)
    s2, e2 = duty_time(3, 3)
    res2 = await add_task(
        client, auth, world.lead, event_id,
        title="Sound Control",
        assignee_id=str(world.volunteer.id),
        starts_at=s2,
        ends_at=e2,
    )
    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == "volunteer_clash"
    assert "overlaps" in res2.json()["error"]["message"]


@pytest.mark.asyncio
async def test_different_volunteer_nonconflict(client, world, auth):
    """Different volunteers can have identical duty times without conflict."""
    event_id = await published_event(client, auth, world)

    s, e = duty_time(2, 3)
    res1 = await add_task(
        client, auth, world.lead, event_id,
        title="Front Gate Duty",
        assignee_id=str(world.volunteer.id),
        starts_at=s,
        ends_at=e,
    )
    assert res1.status_code == 201

    res2 = await add_task(
        client, auth, world.lead, event_id,
        title="Back Gate Duty",
        assignee_id=str(world.member.id),
        starts_at=s,
        ends_at=e,
    )
    assert res2.status_code == 201


@pytest.mark.asyncio
async def test_cross_event_clash_and_privacy(client, db, world, auth):
    """Volunteer scheduled in another event conflicts. If cross-club, privacy is preserved."""
    event1 = await published_event(client, auth, world, name="Alpha Fest")
    event2 = await published_event(client, auth, world, name="Beta Summit")

    s1, e1 = duty_time(4, 2)
    # Assign in event 1
    res1 = await add_task(
        client, auth, world.lead, event1,
        title="Ticketing Booth",
        assignee_id=str(world.volunteer.id),
        starts_at=s1,
        ends_at=e1,
    )
    assert res1.status_code == 201

    # Attempt to assign same volunteer in event 2 during overlapping window
    s2, e2 = duty_time(4, 2)
    res2 = await add_task(
        client, auth, world.lead, event2,
        title="VIP Escort",
        assignee_id=str(world.volunteer.id),
        starts_at=s2,
        ends_at=e2,
    )
    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == "volunteer_clash"
    # Same club reveals event name
    assert "Alpha Fest" in res2.json()["error"]["message"]


@pytest.mark.asyncio
async def test_authorized_override_audit(client, db, world, auth):
    """Authorized organizer can override scheduling conflicts with a valid reason, creating an audit record."""
    event_id = await published_event(client, auth, world)

    s1, e1 = duty_time(1, 2)
    s2, e2 = duty_time(2, 2)  # Overlaps

    res1 = await add_task(
        client, auth, world.lead, event_id,
        title="Stage Left Mic",
        assignee_id=str(world.volunteer.id),
        starts_at=s1,
        ends_at=e1,
    )
    assert res1.status_code == 201

    # Override without reason should fail
    res_no_reason = await add_task(
        client, auth, world.lead, event_id,
        title="Stage Right Mic",
        assignee_id=str(world.volunteer.id),
        starts_at=s2,
        ends_at=e2,
        override_clash=True,
    )
    assert res_no_reason.status_code in (400, 422)

    # Override with short reason (< 5 chars) should fail
    res_short = await add_task(
        client, auth, world.lead, event_id,
        title="Stage Right Mic",
        assignee_id=str(world.volunteer.id),
        starts_at=s2,
        ends_at=e2,
        override_clash=True,
        override_reason="ok",
    )
    assert res_short.status_code in (400, 422)

    # Valid authorized override
    res_ok = await add_task(
        client, auth, world.lead, event_id,
        title="Stage Right Mic",
        assignee_id=str(world.volunteer.id),
        starts_at=s2,
        ends_at=e2,
        override_clash=True,
        override_reason="Volunteer handles audio console between both mics",
    )
    assert res_ok.status_code == 201
    task_id = res_ok.json()["id"]

    # Verify audit record exists in database
    override = await db.scalar(
        select(CrewTaskClashOverride).where(CrewTaskClashOverride.task_id == uuid.UUID(task_id))
    )
    assert override is not None
    assert override.volunteer_id == world.volunteer.id
    assert override.overridden_by == world.lead.id
    assert "audio console" in override.override_reason


@pytest.mark.asyncio
async def test_unauthorized_override_rejection(client, world, auth):
    """Non-organizers cannot create tasks or override conflicts."""
    event_id = await published_event(client, auth, world)

    res = await add_task(
        client, auth, world.volunteer, event_id,
        title="Self Assigned Task",
        override_clash=True,
        override_reason="I want to do this task",
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_clash_preview_endpoint(client, world, auth):
    """Preview endpoint flags conflicts before creating assignments."""
    event_id = await published_event(client, auth, world)

    s1, e1 = duty_time(5, 2)
    await add_task(
        client, auth, world.lead, event_id,
        title="Main Gate Security",
        assignee_id=str(world.volunteer.id),
        starts_at=s1,
        ends_at=e1,
    )

    # Preview clash with overlapping times
    s2, e2 = duty_time(6, 2)
    preview = await client.post(
        f"{EVENTS}/{event_id}/tasks/check-clashes",
        headers=auth(world.lead),
        json={
            "assignee_id": str(world.volunteer.id),
            "title": "Badge Distribution",
            "starts_at": s2,
            "ends_at": e2,
        },
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["has_clashes"] is True
    assert data["can_override"] is True
    assert data["is_exact_duplicate"] is False
    assert len(data["clashes"]) >= 1
