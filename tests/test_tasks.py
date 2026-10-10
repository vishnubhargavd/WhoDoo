"""CrewPulse API: task creation, assignment permissions, status transitions and deadlines."""

import uuid
from datetime import datetime, timedelta, timezone

from app.models.crewpulse import CrewTask
from app.models.shared import Club, User
from tests.test_events import API as EVENTS, at, create

TASKS = "/api/v1/tasks"


async def published_event(client, auth, world, **changes) -> str:
    event_id = (await create(client, auth, world.lead, **changes)).json()["id"]
    await client.post(f"{EVENTS}/{event_id}/status", headers=auth(world.lead), json={"status": "published"})
    return event_id


async def add_task(client, auth, user, event_id, **body):
    payload = {"title": "Book the DJ", "priority": "high"}
    payload.update(body)
    return await client.post(f"{EVENTS}/{event_id}/tasks", headers=auth(user), json=payload)


async def test_organizer_creates_and_assigns(client, world, auth):
    event_id = await published_event(client, auth, world)
    response = await add_task(client, auth, world.lead, event_id, assignee_id=str(world.volunteer.id),
                              is_critical=True, due_at=at(18))
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "todo" and task["assignee_name"] == "Vik Volunteer"
    assert task["is_critical"] and task["can_edit"] and task["event_name"] == "Spring Gala"

    president = await add_task(client, auth, world.president, event_id, title="Print posters")
    assert president.status_code == 201 and "unassigned" in president.json()["risks"]

    listing = await client.get(f"{EVENTS}/{event_id}/tasks", headers=auth(world.member))
    assert listing.status_code == 200 and len(listing.json()) == 2
    assert listing.json()[0]["title"] == "Book the DJ"  # critical first

    event = (await client.get(f"{EVENTS}/{event_id}", headers=auth(world.lead))).json()
    assert event["progress"]["total"] == 2 and event["progress"]["todo"] == 2
    # Organizers: who leads, who created it, and who is on the crew with how much work.
    assert event["lead_name"] == "Leo Lead" and event["created_by_name"] == "Leo Lead"
    assert [(m["full_name"], m["tasks_total"], m["tasks_done"]) for m in event["crew"]] == [("Vik Volunteer", 1, 0)]


async def test_only_organizers_create_or_edit(client, world, auth):
    event_id = await published_event(client, auth, world)
    for person in (world.volunteer, world.member, world.treasurer, world.lead2):
        assert (await add_task(client, auth, person, event_id)).status_code == 403

    task_id = (await add_task(client, auth, world.lead, event_id, assignee_id=str(world.volunteer.id))).json()["id"]
    takeover = await client.patch(f"{TASKS}/{task_id}", headers=auth(world.volunteer),
                                  json={"assignee_id": str(world.member.id)})
    assert takeover.status_code == 403
    assert (await client.patch(f"{TASKS}/{task_id}", headers=auth(world.lead2), json={"title": "Mine now"})).status_code == 403


async def test_task_validation_and_duplicates(client, world, auth):
    event_id = await published_event(client, auth, world)
    assert (await add_task(client, auth, world.lead, event_id, title="ab")).status_code == 422
    assert (await add_task(client, auth, world.lead, event_id, priority="urgent")).status_code == 422
    assert (await add_task(client, auth, world.lead, event_id, status="done")).status_code == 422

    past = await add_task(client, auth, world.lead, event_id, due_at=at(-1))
    assert past.status_code == 400 and past.json()["error"]["code"] == "due_in_past"

    assert (await add_task(client, auth, world.lead, event_id)).status_code == 201
    dupe = await add_task(client, auth, world.lead, event_id, title="  book the dj ")
    assert dupe.status_code == 409 and dupe.json()["error"]["code"] == "duplicate_task"


async def test_assignee_must_be_in_the_club(client, db, world, auth):
    other = Club(id=uuid.uuid4(), name="Other Club")
    db.add(other)
    await db.flush()
    stranger = User(id=uuid.uuid4(), club_id=other.id, full_name="Sam Stranger", email="sam@other.example",
                    role="volunteer", is_active_member=True)
    db.add(stranger)
    await db.commit()

    event_id = await published_event(client, auth, world)
    response = await add_task(client, auth, world.lead, event_id, assignee_id=str(stranger.id))
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_assignee"
    # The stranger cannot see the task list either.
    assert (await client.get(f"{EVENTS}/{event_id}/tasks", headers=auth(stranger))).status_code == 404
    assert (await client.get(f"{TASKS}/mine", headers=auth(stranger))).json() == []


async def test_status_flow_and_blockers(client, world, auth):
    event_id = await published_event(client, auth, world)
    task_id = (await add_task(client, auth, world.lead, event_id, assignee_id=str(world.volunteer.id))).json()["id"]
    status = f"{TASKS}/{task_id}/status"

    other = await client.post(status, headers=auth(world.member), json={"status": "done"})
    assert other.status_code == 403

    started = await client.post(status, headers=auth(world.volunteer), json={"status": "in_progress"})
    assert started.status_code == 200 and started.json()["status"] == "in_progress"

    no_note = await client.post(status, headers=auth(world.volunteer), json={"status": "blocked"})
    assert no_note.status_code == 422
    blocked = await client.post(status, headers=auth(world.volunteer),
                                json={"status": "blocked", "blocker_note": "DJ wants a ₹5,000 advance"})
    assert blocked.json()["status"] == "blocked" and "blocked" in blocked.json()["risks"]

    # The leader sees the blocker straight away, in the event's progress and readiness.
    event = (await client.get(f"{EVENTS}/{event_id}", headers=auth(world.lead))).json()
    assert event["progress"]["blocked"] == 1 and event["progress"]["at_risk"] == 1
    readiness = (await client.get(f"{EVENTS}/{event_id}/readiness", headers=auth(world.lead))).json()
    blocker = next(f for f in readiness["factors"] if f["code"] == "blocked_tasks")
    assert blocker["evidence"] == ["Book the DJ"]

    done = await client.post(status, headers=auth(world.volunteer), json={"status": "done"})
    assert done.json()["completed_at"] and done.json()["blocker_note"] is None
    again = await client.post(status, headers=auth(world.volunteer), json={"status": "done"})
    assert again.status_code == 200 and again.json()["completed_at"] == done.json()["completed_at"]

    reopened = await client.post(status, headers=auth(world.lead), json={"status": "todo"})
    assert reopened.json()["completed_at"] is None

    trail = (await client.get(f"{TASKS}/{task_id}/activity", headers=auth(world.lead))).json()
    assert [(a["action"], a["to_value"]) for a in trail] == [
        ("created", "Book the DJ"), ("assigned", str(world.volunteer.id)), ("status_changed", "in_progress"),
        ("status_changed", "blocked"), ("status_changed", "done"), ("status_changed", "todo"),
    ]
    assert trail[3]["note"] == "DJ wants a ₹5,000 advance" and trail[3]["actor_name"] == "Vik Volunteer"


async def test_reassign_and_unassign(client, world, auth):
    event_id = await published_event(client, auth, world)
    task_id = (await add_task(client, auth, world.lead, event_id, assignee_id=str(world.volunteer.id))).json()["id"]
    url = f"{TASKS}/{task_id}"

    moved = await client.patch(url, headers=auth(world.lead), json={"assignee_id": str(world.member.id), "priority": "low"})
    assert moved.status_code == 200 and moved.json()["assignee_name"] == "Mia Member"
    assert moved.json()["priority"] == "low"
    # The previous assignee loses the right to update it.
    assert (await client.post(f"{url}/status", headers=auth(world.volunteer), json={"status": "done"})).status_code == 403

    cleared = await client.patch(url, headers=auth(world.lead), json={"unassign": True})
    assert cleared.json()["assignee_id"] is None
    both = await client.patch(url, headers=auth(world.lead), json={"unassign": True, "assignee_id": str(world.member.id)})
    assert both.status_code == 422


async def test_my_tasks_and_overdue(client, db, world, auth):
    event_id = await published_event(client, auth, world)
    task_id = (await add_task(client, auth, world.lead, event_id, assignee_id=str(world.volunteer.id),
                              due_at=at(5))).json()["id"]
    await add_task(client, auth, world.lead, event_id, title="Someone else's", assignee_id=str(world.member.id))

    task = await db.get(CrewTask, uuid.UUID(task_id))
    task.due_at = datetime.now(timezone.utc) - timedelta(hours=3)
    await db.commit()

    mine = (await client.get(f"{TASKS}/mine", headers=auth(world.volunteer))).json()
    assert [t["title"] for t in mine] == ["Book the DJ"]
    assert "overdue" in mine[0]["risks"] and mine[0]["can_update_status"] and not mine[0]["can_edit"]


async def test_tasks_frozen_when_event_cancelled(client, world, auth):
    event_id = await published_event(client, auth, world)
    task_id = (await add_task(client, auth, world.lead, event_id, assignee_id=str(world.volunteer.id))).json()["id"]
    await client.post(f"{EVENTS}/{event_id}/status", headers=auth(world.lead), json={"status": "cancelled"})

    assert (await add_task(client, auth, world.lead, event_id, title="Late idea")).status_code == 409
    assert (await client.post(f"{TASKS}/{task_id}/status", headers=auth(world.volunteer),
                              json={"status": "done"})).status_code == 409
    assert (await client.get(f"{TASKS}/mine", headers=auth(world.volunteer))).json() == []


async def test_crewpulse_volunteer_coordination_and_audit(client, world, auth):
    event_id = await published_event(client, auth, world)

    # 1. Lead creates a task assigned to volunteer with deadline
    res = await add_task(
        client, auth, world.lead, event_id,
        title="Stage Cable Setup",
        description="Run XLR cables to stage",
        priority="high",
        is_critical=True,
        assignee_id=str(world.volunteer.id),
        due_at=at(12),
    )
    assert res.status_code == 201
    task = res.json()
    task_id = task["id"]
    assert task["title"] == "Stage Cable Setup"
    assert task["status"] == "todo"
    assert task["assignee_name"] == "Vik Volunteer"
    assert "clashes" in task

    # 2. Duplicate task on same event is rejected
    dupe = await add_task(client, auth, world.lead, event_id, title="Stage Cable Setup")
    assert dupe.status_code == 409

    # 3. Volunteer sees their assigned task
    mine = (await client.get(f"{TASKS}/mine", headers=auth(world.volunteer))).json()
    my_task = next(t for t in mine if t["id"] == task_id)
    assert my_task["can_update_status"] is True
    assert my_task["can_edit"] is False

    # 4. Unrelated member cannot update status or edit
    unauth_status = await client.post(
        f"{TASKS}/{task_id}/status",
        headers=auth(world.member),
        json={"status": "in_progress"},
    )
    assert unauth_status.status_code == 403

    unauth_edit = await client.patch(
        f"{TASKS}/{task_id}",
        headers=auth(world.member),
        json={"title": "Hacked Title"},
    )
    assert unauth_edit.status_code == 403

    # 5. Volunteer starts task, then reports a blocker
    await client.post(f"{TASKS}/{task_id}/status", headers=auth(world.volunteer), json={"status": "in_progress"})
    blocked = await client.post(
        f"{TASKS}/{task_id}/status",
        headers=auth(world.volunteer),
        json={"status": "blocked", "blocker_note": "Need 50m extension reel"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["blocker_note"] == "Need 50m extension reel"

    # 6. Event lead unblocks and marks completed
    done = await client.post(
        f"{TASKS}/{task_id}/status",
        headers=auth(world.lead),
        json={"status": "done"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["completed_at"] is not None

    # 7. Verify full activity audit history
    activity = (await client.get(f"{TASKS}/{task_id}/activity", headers=auth(world.lead))).json()
    actions = [a["action"] for a in activity]
    assert actions == ["created", "assigned", "status_changed", "status_changed", "status_changed"]
    assert activity[3]["note"] == "Need 50m extension reel"
    assert activity[3]["actor_name"] == "Vik Volunteer"
    assert activity[4]["to_value"] == "done"
    assert activity[4]["actor_name"] == "Leo Lead"

