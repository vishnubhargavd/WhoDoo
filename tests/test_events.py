"""Event Management API: validation, lifecycle, permissions and club isolation."""

import uuid
from datetime import datetime, timedelta, timezone

from app.models.shared import Club, Event, User

API = "/api/v1/events"


def at(days: int, hour: int = 18) -> str:
    """An ISO time `days` from now at `hour`:00 IST, as a browser form would send it (no offset)."""
    day = (datetime.now(timezone.utc) + timedelta(days=days)).date()
    return f"{day.isoformat()}T{hour:02d}:00"


def gala(**changes) -> dict:
    body = {"name": "Spring Gala", "description": "Our biggest night of the year.", "starts_at": at(20),
            "ends_at": at(20, 22), "venue": "Main Hall"}
    body.update(changes)
    return body


async def create(client, auth, user, **changes):
    return await client.post(API, headers=auth(user), json=gala(**changes))


async def test_lead_creates_draft_led_by_themselves(client, world, auth):
    response = await create(client, auth, world.lead)
    assert response.status_code == 201, response.text
    event = response.json()
    assert event["status"] == "draft" and event["lead_id"] == str(world.lead.id)
    assert event["lead_name"] == "Leo Lead" and event["can_manage"]
    assert event["starts_at"].endswith("+00:00") or event["starts_at"].endswith("Z")
    # 18:00 IST is 12:30 UTC; event_date is the IST calendar date.
    assert "T12:30:00" in event["starts_at"] and event["event_date"] == at(20)[:10]
    assert event["progress"]["total"] == 0


async def test_validation_errors(client, world, auth):
    cases = [
        gala(name="  "),
        gala(ends_at=at(20, 17)),  # ends before it starts
        gala(ends_at=at(24)),  # longer than 72 hours
        gala(starts_at="not a date"),
        gala(club_id=str(uuid.uuid4())),  # never trust a club id from the browser
        gala(status="published"),
    ]
    for body in cases:
        response = await client.post(API, headers=auth(world.president), json=body)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "validation_error"

    backwards = await client.post(API, headers=auth(world.president), json=gala(ends_at=at(20, 17)))
    assert backwards.json()["error"]["message"] == "The event must end after it starts"  # readable, no prefix

    past = await create(client, auth, world.president, starts_at=at(-1), ends_at=at(-1, 20))
    assert past.status_code == 400 and past.json()["error"]["code"] == "start_in_past"


async def test_only_president_and_leads_create(client, world, auth):
    for person in (world.volunteer, world.member, world.treasurer):
        assert (await create(client, auth, person)).status_code == 403
    assert (await client.post(API, json=gala())).status_code == 401


async def test_lead_cannot_assign_someone_else_but_president_can(client, world, auth):
    response = await create(client, auth, world.lead, lead_id=str(world.lead2.id))
    assert response.status_code == 403

    response = await create(client, auth, world.president, lead_id=str(world.lead2.id))
    assert response.status_code == 201 and response.json()["lead_id"] == str(world.lead2.id)

    bad = await create(client, auth, world.president, name="Other", lead_id=str(world.volunteer.id))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_lead"


async def test_duplicate_create_is_rejected(client, world, auth):
    assert (await create(client, auth, world.president)).status_code == 201
    again = await create(client, auth, world.president, name="  spring gala ")
    assert again.status_code == 409 and again.json()["error"]["code"] == "duplicate_event"


async def test_edit_permissions(client, world, auth):
    event_id = (await create(client, auth, world.lead)).json()["id"]
    url = f"{API}/{event_id}"

    assert (await client.patch(url, headers=auth(world.lead2), json={"venue": "Lab"})).status_code == 404  # draft hidden
    await client.post(f"{url}/status", headers=auth(world.lead), json={"status": "published"})
    assert (await client.patch(url, headers=auth(world.lead2), json={"venue": "Lab"})).status_code == 403
    assert (await client.patch(url, headers=auth(world.volunteer), json={"venue": "Lab"})).status_code == 403

    ok = await client.patch(url, headers=auth(world.lead), json={"venue": "Open Air Theatre", "starts_at": at(21)})
    assert ok.status_code == 400 and ok.json()["error"]["code"] == "invalid_schedule"  # end would precede start
    ok = await client.patch(url, headers=auth(world.lead), json={"venue": "Open Air Theatre"})
    assert ok.status_code == 200 and ok.json()["venue"] == "Open Air Theatre"

    takeover = await client.patch(url, headers=auth(world.lead), json={"lead_id": str(world.lead2.id)})
    assert takeover.status_code == 403
    handover = await client.patch(url, headers=auth(world.president), json={"lead_id": str(world.lead2.id)})
    assert handover.status_code == 200
    # The old lead no longer manages it.
    assert (await client.patch(url, headers=auth(world.lead), json={"venue": "X Hall"})).status_code == 403


async def test_lifecycle(client, world, auth):
    event_id = (await create(client, auth, world.lead, venue=None)).json()["id"]
    status = f"{API}/{event_id}/status"

    blocked = await client.post(status, headers=auth(world.lead), json={"status": "published"})
    assert blocked.status_code == 400 and blocked.json()["error"]["details"] == ["venue"]

    await client.patch(f"{API}/{event_id}", headers=auth(world.lead), json={"venue": "Main Hall"})
    published = await client.post(status, headers=auth(world.lead), json={"status": "published"})
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert "completed" not in published.json()["allowed_statuses"]  # not started yet

    again = await client.post(status, headers=auth(world.lead), json={"status": "published"})
    assert again.status_code == 200  # repeated click is harmless

    early = await client.post(status, headers=auth(world.lead), json={"status": "completed"})
    assert early.status_code == 400 and early.json()["error"]["code"] == "not_started"

    cancelled = await client.post(status, headers=auth(world.lead), json={"status": "cancelled"})
    assert cancelled.status_code == 200
    reopen = await client.post(status, headers=auth(world.lead), json={"status": "draft"})
    assert reopen.status_code == 409 and reopen.json()["error"]["code"] == "invalid_transition"
    frozen = await client.patch(f"{API}/{event_id}", headers=auth(world.lead), json={"venue": "Elsewhere"})
    assert frozen.status_code == 409


async def test_completed_after_start(client, db, world, auth):
    event_id = (await create(client, auth, world.lead)).json()["id"]
    await client.post(f"{API}/{event_id}/status", headers=auth(world.lead), json={"status": "published"})
    event = await db.get(Event, uuid.UUID(event_id))
    event.starts_at = datetime.now(timezone.utc) - timedelta(hours=2)
    event.ends_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.commit()
    done = await client.post(f"{API}/{event_id}/status", headers=auth(world.lead), json={"status": "completed"})
    assert done.status_code == 200 and done.json()["status"] == "completed"

    past = await client.get(API, headers=auth(world.member), params={"scope": "past"})
    assert [e["name"] for e in past.json()] and "Spring Gala" in [e["name"] for e in past.json()]


async def test_listing_and_draft_visibility(client, world, auth):
    draft_id = (await create(client, auth, world.lead, name="Secret Draft")).json()["id"]
    pub_id = (await create(client, auth, world.lead, name="Open Night", starts_at=at(25), ends_at=at(25, 21))).json()["id"]
    await client.post(f"{API}/{pub_id}/status", headers=auth(world.lead), json={"status": "published"})

    names = lambda r: [e["name"] for e in r.json()]  # noqa: E731
    assert "Secret Draft" not in names(await client.get(API, headers=auth(world.member)))
    assert "Open Night" in names(await client.get(API, headers=auth(world.member)))
    assert "Secret Draft" in names(await client.get(API, headers=auth(world.president)))
    assert (await client.get(f"{API}/{draft_id}", headers=auth(world.volunteer))).status_code == 404

    upcoming = names(await client.get(API, headers=auth(world.lead)))
    assert upcoming.index("Secret Draft") < upcoming.index("Open Night")  # soonest first


async def test_other_clubs_cannot_see_or_touch_events(client, db, world, auth):
    event_id = (await create(client, auth, world.president)).json()["id"]
    await client.post(f"{API}/{event_id}/status", headers=auth(world.president), json={"status": "published"})

    other = Club(id=uuid.uuid4(), name="Other Club")
    db.add(other)
    await db.flush()
    rival = User(id=uuid.uuid4(), club_id=other.id, full_name="Rita Rival", email="rita@other.example",
                 role="president", is_active_member=True)
    db.add(rival)
    await db.commit()

    assert (await client.get(f"{API}/{event_id}", headers=auth(rival))).status_code == 404
    assert (await client.patch(f"{API}/{event_id}", headers=auth(rival), json={"venue": "X"})).status_code == 404
    assert (await client.get(f"{API}/{event_id}/tasks", headers=auth(rival))).status_code == 404
    assert (await client.get(f"{API}/{event_id}/readiness", headers=auth(rival))).status_code == 404
    assert "Spring Gala" not in [e["name"] for e in (await client.get(API, headers=auth(rival), params={"scope": "all"})).json()]
    # And a president of another club cannot make one of our people lead their event.
    bad = await client.post(API, headers=auth(rival), json=gala(lead_id=str(world.lead.id)))
    assert bad.status_code == 400


async def test_existing_events_still_work_for_finance(client, world, auth):
    """Events created before Event Management (no times) still list and open."""
    response = await client.get(f"{API}/{world.bake_sale.id}", headers=auth(world.president))
    assert response.status_code == 200 and response.json()["starts_at"] is None
    readiness = await client.get(f"{API}/{world.bake_sale.id}/readiness", headers=auth(world.president))
    assert readiness.status_code == 200 and "missing_schedule" in [f["code"] for f in readiness.json()["factors"]]
    check = await client.get(f"{API}/{world.bake_sale.id}/schedule-check", headers=auth(world.president))
    assert check.status_code == 200 and check.json()["checked"] is False


async def test_schedule_check_and_readiness(client, world, auth):
    a = (await create(client, auth, world.president, lead_id=str(world.lead.id))).json()["id"]
    await create(client, auth, world.president, name="Dance Night", starts_at=at(20, 20), ends_at=at(20, 23),
                 venue="main hall")
    check = await client.get(f"{API}/{a}/schedule-check", headers=auth(world.lead))
    body = check.json()
    assert check.status_code == 200 and body["checked"]
    assert [c["kind"] for c in body["conflicts"]] == ["venue"]
    assert body["suggestions"] and "no known clash" in body["suggestion_note"]
    # "Dance Night" is the president's draft: it still clashes, but the lead does not learn its name.
    hidden = body["conflicts"][0]
    assert hidden["other_event_name"] == "an unpublished draft event" and not hidden["other_visible"]
    assert "Dance Night" not in check.text
    seen = (await client.get(f"{API}/{a}/schedule-check", headers=auth(world.president))).json()["conflicts"][0]
    assert seen["other_event_name"] == "Dance Night" and seen["other_visible"]

    readiness = await client.get(f"{API}/{a}/readiness", headers=auth(world.lead))
    report = readiness.json()
    assert readiness.status_code == 200 and report["applicable"]
    assert {"venue_clash"} <= {f["code"] for f in report["factors"]}
    assert report["score"] == 100 - sum(f["points"] for f in report["factors"])

    assert (await client.get(f"{API}/{a}/readiness", headers=auth(world.volunteer))).status_code in (403, 404)
