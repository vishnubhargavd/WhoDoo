"""Actual PostgreSQL QR issuance, authorization, atomic redemption and replay tests."""
import asyncio
import base64
import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.services import gatesync_service
from tests.test_fairseat import NOW, book, category, gala  # noqa: F401

API = "/api/v1/gatesync"


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "gatesync_enabled", True)
    monkeypatch.setattr(get_settings(), "gatesync_credential_key", base64.urlsafe_b64encode(b"g" * 32).decode())


async def free_ticket(client, auth, world, category, book):
    cat = await category(price=0)
    reserved = await book(world.member, cat)
    assert reserved.status_code == 201, reserved.text
    data = reserved.json()
    ticket_id = data["tickets"][0]["id"]
    response = await client.post(f"{API}/tickets/{ticket_id}/credential", headers=auth(world.member))
    assert response.status_code == 200, response.text
    return cat, ticket_id, response.json()["credential"]


async def test_free_booking_issues_stable_opaque_encrypted_credential(client, auth, world, category, book, db):
    cat, ticket_id, credential = await free_ticket(client, auth, world, category, book)
    assert gatesync_service.digest_credential(credential) is not None
    assert str(ticket_id) not in credential
    again = await client.post(f"{API}/tickets/{ticket_id}/credential", headers=auth(world.member))
    assert again.json()["credential"] == credential
    row = (await db.execute(text("SELECT credential_digest,credential_encrypted FROM event_tickets WHERE id=:id"),
                            {"id": uuid.UUID(ticket_id)})).first()
    assert bytes(row.credential_digest) == gatesync_service.digest_credential(credential)
    assert credential.encode() not in bytes(row.credential_encrypted)
    listing = await client.get(f"{API}/tickets", headers=auth(world.member))
    assert listing.status_code == 200
    assert listing.json()[0]["ticket_id"] == ticket_id
    assert "credential" not in listing.json()[0]
    assert (await client.get(f"{API}/tickets", headers=auth(world.outsider))).json() == []


async def test_credential_ownership_and_staff_assignment_are_server_enforced(client, auth, world, category, book, gala):
    _, ticket_id, _ = await free_ticket(client, auth, world, category, book)
    assert (await client.post(f"{API}/tickets/{ticket_id}/credential", headers=auth(world.outsider))).status_code == 404
    for actor in (world.member, world.volunteer, world.lead2, world.treasurer):
        denied = await client.post(f"{API}/events/{gala.id}/staff", headers=auth(actor),
                                   json={"user_id": str(world.volunteer.id)})
        assert denied.status_code == 403
    assert (await client.post(f"{API}/events/{gala.id}/staff", headers=auth(world.lead),
                             json={"user_id": str(world.volunteer.id)})).status_code == 201
    assert len((await client.get(f"{API}/access", headers=auth(world.volunteer))).json()["events"]) == 1
    assert (await client.get(f"{API}/access", headers=auth(world.member))).json()["events"] == []


async def test_two_scanners_redeem_exactly_once(client, auth, world, category, book, gala, db):
    _, ticket_id, credential = await free_ticket(client, auth, world, category, book)
    await client.post(f"{API}/events/{gala.id}/staff", headers=auth(world.president),
                      json={"user_id": str(world.volunteer.id)})
    scans = await asyncio.gather(*(
        client.post(f"{API}/redeem", headers=auth(world.volunteer),
                    json={"event_id": str(gala.id), "credential": credential}) for _ in range(2)))
    assert [r.status_code for r in scans] == [200, 200]
    assert sorted(r.json()["outcome"] for r in scans) == ["already_checked_in", "checked_in"]
    assert await db.scalar(text("SELECT count(*) FROM gatesync_attendance WHERE ticket_id=:id"),
                           {"id": uuid.UUID(ticket_id)}) == 1
    assert await db.scalar(text("SELECT status FROM event_tickets WHERE id=:id"), {"id": uuid.UUID(ticket_id)}) == "checked_in"
    assert (await client.post(f"{API}/tickets/{ticket_id}/credential", headers=auth(world.member))).status_code == 409
    # No token is retained in audit details, including rejected replay logs.
    assert credential not in str((await db.execute(text("SELECT details FROM audit_logs"))).all())


async def test_unauthorized_malformed_wrong_event_and_expired_scans(client, auth, world, category, book, gala, db):
    _, ticket_id, credential = await free_ticket(client, auth, world, category, book)
    body = {"event_id": str(gala.id), "credential": credential}
    for actor in (world.member, world.outsider, world.volunteer, world.lead2):
        assert (await client.post(f"{API}/redeem", headers=auth(actor), json=body)).status_code == 403
    malformed = await client.post(f"{API}/redeem", headers=auth(world.lead),
                                  json=body | {"credential": "ticket-uuid-is-not-a-credential"})
    assert malformed.json()["outcome"] == "not_eligible"
    wrong = await client.post(f"{API}/redeem", headers=auth(world.president),
                             json=body | {"event_id": str(world.fest.id)})
    assert wrong.json()["outcome"] == "wrong_event"
    # At the event's end the credential is expired; no attendance is written.
    from tests.test_fairseat import acting
    result = await gatesync_service.redeem(db, acting(world.lead), gala.id, credential, now=gala.ends_at)
    assert result["outcome"] == "not_eligible"
    await db.commit()
    assert await db.scalar(text("SELECT count(*) FROM gatesync_attendance")) == 0


async def test_cancelled_booking_revokes_qr(client, auth, world, category, book, gala):
    _, ticket_id, credential = await free_ticket(client, auth, world, category, book)
    bookings = (await client.get("/api/v1/ticket-bookings", headers=auth(world.member))).json()
    cancelled = await client.post(f"/api/v1/ticket-bookings/{bookings[0]['booking_id']}/cancel", headers=auth(world.member))
    assert cancelled.status_code == 200
    rejected = await client.post(f"{API}/redeem", headers=auth(world.lead),
                                 json={"event_id": str(gala.id), "credential": credential})
    assert rejected.json()["outcome"] == "not_eligible"


async def test_cross_club_and_pending_payment_cannot_issue_or_scan(client, auth, world, category, book, gala, db):
    from app.models.fairseat import EventTicket
    from app.models.shared import Club, User
    from tests.test_fairseat import acting
    other_club = Club(id=uuid.uuid4(), name="Other club")
    db.add(other_club)
    await db.flush()
    other = User(id=uuid.uuid4(), club_id=other_club.id, full_name="Other president", email="qr-other@test.example",
                 role="president", is_active_member=False)
    db.add(other)
    await db.commit()
    _, ticket_id, credential = await free_ticket(client, auth, world, category, book)
    assert (await client.post(f"{API}/tickets/{ticket_id}/credential", headers=auth(other))).status_code == 404
    assert (await client.post(f"{API}/redeem", headers=auth(other),
                             json={"event_id": str(gala.id), "credential": credential})).status_code == 404
    cat = await category(price=10000, name="Paid")
    pending = (await book(world.outsider, cat)).json()
    ticket = EventTicket(id=uuid.uuid4(), booking_id=uuid.UUID(pending["booking_id"]), event_id=gala.id,
                         holder_user_id=world.outsider.id, seat_number=1, status="pending_issue")
    db.add(ticket)
    await db.commit()
    assert (await client.post(f"{API}/tickets/{ticket.id}/credential", headers=auth(world.outsider))).status_code == 409


async def test_missing_credential_key_fails_closed(client, auth, world, category, book, monkeypatch):
    cat = await category(price=0)
    ticket_id = (await book(world.member, cat)).json()["tickets"][0]["id"]
    monkeypatch.setattr(get_settings(), "gatesync_credential_key", "")
    response = await client.post(f"{API}/tickets/{ticket_id}/credential", headers=auth(world.member))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gatesync_configuration"


def test_credential_digest_rejects_noncanonical_tokens():
    token = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    credential = "whodoo.ticket.v1." + token
    assert gatesync_service.digest_credential(credential) == hashlib.sha256(b"whodoo.ticket.v1." + b"a" * 32).digest()
    for invalid in (credential + "=", credential[:-1] + "!", str(uuid.uuid4()), credential.replace(".v1.", ".v2.")):
        assert gatesync_service.digest_credential(invalid) is None
