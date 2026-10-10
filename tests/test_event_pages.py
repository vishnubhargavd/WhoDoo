"""Smoke tests: Member 2 pages render for the right people with real data."""

from app.core.config import get_settings
from app.core.security import create_access_token
from tests.test_events import API as EVENTS, at, create


def _cookie(user) -> dict[str, str]:
    return {get_settings().session_cookie: create_access_token(user.id)}


async def _gala_with_tasks(client, auth, world) -> str:
    event_id = (await create(client, auth, world.lead)).json()["id"]
    await client.post(f"{EVENTS}/{event_id}/status", headers=auth(world.lead), json={"status": "published"})
    task = await client.post(f"{EVENTS}/{event_id}/tasks", headers=auth(world.lead), json={
        "title": "Book the DJ", "assignee_id": str(world.volunteer.id), "is_critical": True, "due_at": at(15)})
    await client.post(f"/api/v1/tasks/{task.json()['id']}/status", headers=auth(world.volunteer),
                      json={"status": "blocked", "blocker_note": "Needs an advance payment"})
    await client.post(f"{EVENTS}/{event_id}/tasks", headers=auth(world.lead), json={"title": "Print posters"})
    return event_id


async def test_pages_need_sign_in(client):
    response = await client.get("/events")
    assert response.status_code == 303 and response.headers["location"] == "/dev/login?next=/events"


async def test_organizer_pages(client, world, auth):
    event_id = await _gala_with_tasks(client, auth, world)
    client.cookies.update(_cookie(world.lead))

    listing = await client.get("/events")
    assert listing.status_code == 200 and "Spring Gala" in listing.text and "2 at risk" in listing.text  # blocked DJ + unowned posters

    detail = await client.get(f"/events/{event_id}")
    assert detail.status_code == 200
    for text in ("Event Autopilot", "Blocked tasks", "Needs an advance payment", "SyncSphere", "Print posters", "No owner",
                 "Organizers", "Crew (1)", "Vik Volunteer", "Created by", "Updated "):
        assert text in detail.text, text

    assert (await client.get(f"/events/{event_id}/edit")).status_code == 200
    assert (await client.get("/events/new")).status_code == 200
    assert (await client.get("/events?scope=past")).status_code == 200


async def test_volunteer_pages(client, world, auth):
    event_id = await _gala_with_tasks(client, auth, world)
    client.cookies.update(_cookie(world.volunteer))

    mine = await client.get("/my-tasks")
    assert mine.status_code == 200 and "Book the DJ" in mine.text and "Report blocker" in mine.text
    assert "Pending" in mine.text and "Completed" in mine.text
    detail_for_volunteer = await client.get(f"/events/{event_id}")
    assert "Organizers" in detail_for_volunteer.text and "Leo Lead" in detail_for_volunteer.text

    detail = await client.get(f"/events/{event_id}")
    assert detail.status_code == 200 and "Event Autopilot" not in detail.text and "Add a task" not in detail.text
    assert (await client.get("/events/new")).status_code == 403
    assert (await client.get(f"/events/{event_id}/edit")).status_code == 403


async def test_empty_states(client, world):
    client.cookies.update(_cookie(world.volunteer))
    assert "No tasks assigned to you" in (await client.get("/my-tasks")).text
