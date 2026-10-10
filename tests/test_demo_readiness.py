"""Live Hackathon Demo Readiness verification:

Exercises:
1. Student login (local demo / token)
2. Event discovery (published events, ticket categories)
3. Free booking (FairSeat quote and reservation)
4. QR wallet (GateSync credential issuance)
5. Authorized volunteer check-in (GateSync staff assignment & door redemption)
6. All key frontend/backend web routes:
   - / (root/landing)
   - /home (member home)
   - /events (event list)
   - /events/new (event creation form)
   - /store (merch store)
   - /announcements (club announcements feed)
   - /expenses (reimbursement & expense claims)
   - /my-tasks (CrewPulse volunteer tasks)
   - /treasurer (TrustLedger financial dashboard)
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.services import gatesync_service
from tests.test_fairseat import API, NOW, book, category, gala  # noqa: F401


@pytest.fixture(autouse=True)
def live_demo_config(monkeypatch):
    monkeypatch.setattr(get_settings(), "gatesync_enabled", True)
    monkeypatch.setattr(get_settings(), "gatesync_credential_key", base64.urlsafe_b64encode(b"d" * 32).decode())


async def test_end_to_end_student_to_volunteer_flow(client, db, world, auth, category, book):
    # 1. Student / Member Discovery
    cat = await category(price=0, capacity=5, name="Student Free Admission")
    event_id = cat["event_id"]

    # Discovery endpoint
    discovery_res = await client.get("/api/v1/discovery/events", headers=auth(world.member))
    assert discovery_res.status_code == 200
    events = discovery_res.json()
    assert any(str(e["id"]) == str(event_id) for e in events)

    # 2. Free Booking
    reserve_res = await book(world.member, cat)
    assert reserve_res.status_code == 201
    booking = reserve_res.json()
    assert booking["status"] == "confirmed"
    assert booking["payment_status"] == "not_required"
    assert len(booking["tickets"]) == 1
    ticket_id = booking["tickets"][0]["id"]

    # 3. QR Wallet
    wallet_res = await client.get("/api/v1/gatesync/tickets", headers=auth(world.member))
    assert wallet_res.status_code == 200
    my_tickets = wallet_res.json()
    assert any(str(t["ticket_id"]) == str(ticket_id) for t in my_tickets)

    # QR Credential Generation
    cred_res = await client.post(f"/api/v1/gatesync/tickets/{ticket_id}/credential", headers=auth(world.member))
    assert cred_res.status_code == 200
    cred_data = cred_res.json()
    assert "credential" in cred_data
    bearer_credential = cred_data["credential"]
    assert bearer_credential.startswith("whodoo.ticket.v1.")

    # 4. Authorized Volunteer Check-In Assignment
    assign_res = await client.post(
        f"/api/v1/gatesync/events/{event_id}/staff",
        headers=auth(world.president),
        json={"user_id": str(world.volunteer.id)},
    )
    assert assign_res.status_code == 201

    # Volunteer checks access
    access_res = await client.get("/api/v1/gatesync/access", headers=auth(world.volunteer))
    assert access_res.status_code == 200
    staff_events = access_res.json()["events"]
    assert any(str(e["event_id"]) == str(event_id) for e in staff_events)

    # 5. Volunteer Door Redemption
    redeem_res = await client.post(
        "/api/v1/gatesync/redeem",
        headers=auth(world.volunteer),
        json={"event_id": str(event_id), "credential": bearer_credential},
    )
    assert redeem_res.status_code == 200
    redemption = redeem_res.json()
    assert redemption["outcome"] == "checked_in"
    assert str(redemption["ticket_id"]) == str(ticket_id)


async def test_all_web_pages_load_cleanly(client, world, auth):
    """Verify that all core web routes render without template or startup errors."""
    routes = [
        ("/", world.member, 200),
        ("/home", world.member, 200),
        ("/events", world.member, 200),
        ("/events/new", world.president, 200),
        ("/store", world.member, 200),
        ("/announcements", world.member, 200),
        ("/expenses", world.member, 200),
        ("/my-tasks", world.volunteer, 200),
        ("/treasurer", world.president, 200),
        ("/certificates", world.member, 200),
        ("/certificates/verify", world.member, 200),
        ("/legacyvault", world.president, 200),
        ("/profile", world.member, 200),
    ]

    for path, user, expected_code in routes:
        res = await client.get(path, headers=auth(user), follow_redirects=True)
        assert res.status_code == expected_code, f"Route {path} failed with {res.status_code}: {res.text[:200]}"


async def test_dev_login_strictly_forbidden_when_demo_auth_disabled(client, monkeypatch):
    """Ensure that in production / non-demo configurations, /dev/login is rejected."""
    # When demo_auth is False, /dev/login returns 404
    monkeypatch.setattr(get_settings(), "demo_auth", False)
    res_get = await client.get("/dev/login")
    assert res_get.status_code == 404
    res_post = await client.post("/dev/login", data={"user_id": str(uuid.uuid4())})
    assert res_post.status_code == 404
