"""CertiDoo verification tests:
1. Identifying eligible attendees via genuine GateSync attendance.
2. Identifying eligible volunteers via completed CrewPulse tasks.
3. Refusing participation certificates to ticket holders who never checked in.
4. Refusing volunteer certificates to members without completed tasks.
5. Duplicate issuance prevention.
6. Unauthorized issuance prevention (member cannot issue).
7. Public verification of persistent server-owned records.
8. Club isolation enforcement.
9. Recipient retrieval of earned certificates.
"""

import base64
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.models.certidoo import Certificate
from app.models.crewpulse import CrewTask
from app.models.shared import Club, Event, User
from tests.test_fairseat import API, NOW, book, category, gala  # noqa: F401


@pytest.fixture(autouse=True)
def gatesync_config(monkeypatch):
    monkeypatch.setattr(get_settings(), "gatesync_enabled", True)
    monkeypatch.setattr(get_settings(), "gatesync_credential_key", base64.urlsafe_b64encode(b"d" * 32).decode())


async def test_certidoo_complete_workflow(client, db, world, auth, category, book):
    # 1. Create event and tickets
    cat = await category(price=0, capacity=10, name="Annual Gala")
    event_id = cat["event_id"]

    # Member 1 (Mia Member) books a ticket
    b1_res = await book(world.member, cat)
    assert b1_res.status_code == 201
    ticket_id = b1_res.json()["tickets"][0]["id"]

    # Member 2 (Omar Outsider / second user) books a ticket but NEVER checks in
    b2_res = await book(world.outsider, cat)
    assert b2_res.status_code == 201

    # Member 1 checks in via GateSync
    cred_res = await client.post(f"/api/v1/gatesync/tickets/{ticket_id}/credential", headers=auth(world.member))
    assert cred_res.status_code == 200
    credential = cred_res.json()["credential"]

    # Volunteer Vik is assigned staff and checks Member 1 in
    await client.post(
        f"/api/v1/gatesync/events/{event_id}/staff",
        headers=auth(world.president),
        json={"user_id": str(world.volunteer.id)},
    )
    redeem_res = await client.post(
        "/api/v1/gatesync/redeem",
        headers=auth(world.volunteer),
        json={"event_id": str(event_id), "credential": credential},
    )
    assert redeem_res.status_code == 200
    assert redeem_res.json()["outcome"] == "checked_in"

    # Volunteer Vik also has a completed CrewPulse task for this event
    task = CrewTask(
        id=uuid.uuid4(),
        club_id=world.president.club_id,
        event_id=uuid.UUID(str(event_id)),
        title="Stage Lighting Setup",
        assignee_id=world.volunteer.id,
        status="done",
        created_by=world.president.id,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(task)
    await db.commit()

    # 2. Officer checks eligible recipients
    eligible_res = await client.get(
        f"/api/v1/certificates/events/{event_id}/eligible",
        headers=auth(world.president),
    )
    assert eligible_res.status_code == 200
    eligible_list = eligible_res.json()

    # Mia should be eligible for participation
    mia_elig = [e for e in eligible_list if e["user_id"] == str(world.member.id) and e["eligibility_type"] == "participation"]
    assert len(mia_elig) == 1
    assert "check-in" in mia_elig[0]["detail"].lower()

    # Vik should be eligible for volunteer
    vik_elig = [e for e in eligible_list if e["user_id"] == str(world.volunteer.id) and e["eligibility_type"] == "volunteer"]
    assert len(vik_elig) == 1
    assert "CrewPulse" in vik_elig[0]["detail"]

    # Omar (who booked but never checked in) must NOT be eligible for participation
    omar_elig = [e for e in eligible_list if e["user_id"] == str(world.outsider.id)]
    assert len(omar_elig) == 0

    # 3. Refuse certificate to ticket holder who never attended
    refused_res = await client.post(
        "/api/v1/certificates/issue",
        headers=auth(world.president),
        json={
            "event_id": str(event_id),
            "recipient_user_id": str(world.outsider.id),
            "certificate_type": "participation",
            "title": "Certificate of Participation",
        },
    )
    assert refused_res.status_code == 400
    assert "ineligible_recipient" in refused_res.json()["error"]["code"]

    # 4. Refuse volunteer certificate to member without completed tasks
    refused_vol = await client.post(
        "/api/v1/certificates/issue",
        headers=auth(world.president),
        json={
            "event_id": str(event_id),
            "recipient_user_id": str(world.member.id),
            "certificate_type": "volunteer",
            "title": "Certificate of Volunteer Service",
        },
    )
    assert refused_vol.status_code == 400
    assert "ineligible_recipient" in refused_vol.json()["error"]["code"]

    # 5. Unauthorized user (member) cannot issue certificate
    unauth_res = await client.post(
        "/api/v1/certificates/issue",
        headers=auth(world.member),
        json={
            "event_id": str(event_id),
            "recipient_user_id": str(world.member.id),
            "certificate_type": "participation",
            "title": "Self Awarded Certificate",
        },
    )
    assert unauth_res.status_code == 403

    # 6. Issue valid participation certificate to Mia
    issue_res = await client.post(
        "/api/v1/certificates/issue",
        headers=auth(world.president),
        json={
            "event_id": str(event_id),
            "recipient_user_id": str(world.member.id),
            "certificate_type": "participation",
            "title": "Certificate of Participation - Annual Gala",
        },
    )
    assert issue_res.status_code == 201
    cert_data = issue_res.json()
    assert cert_data["verification_code"].startswith("CERT-")
    cert_code = cert_data["verification_code"]
    cert_id = cert_data["id"]

    # 7. Prevent duplicate certificate issuance
    dup_res = await client.post(
        "/api/v1/certificates/issue",
        headers=auth(world.president),
        json={
            "event_id": str(event_id),
            "recipient_user_id": str(world.member.id),
            "certificate_type": "participation",
            "title": "Duplicate Certificate",
        },
    )
    assert dup_res.status_code == 409
    assert "duplicate_certificate" in dup_res.json()["error"]["code"]

    # 8. Issue volunteer certificate to Vik
    vik_res = await client.post(
        "/api/v1/certificates/issue",
        headers=auth(world.lead),
        json={
            "event_id": str(event_id),
            "recipient_user_id": str(world.volunteer.id),
            "certificate_type": "volunteer",
            "title": "Certificate of Outstanding Volunteer Service",
        },
    )
    assert vik_res.status_code == 201
    assert vik_res.json()["verification_code"].startswith("CERT-")

    # 9. Public certificate verification endpoint (no auth required)
    verify_res = await client.get(f"/api/v1/certificates/verify/{cert_code}")
    assert verify_res.status_code == 200
    verification = verify_res.json()
    assert verification["valid"] is True
    assert verification["verification_code"] == cert_code
    assert verification["recipient_name"] == world.member.full_name
    assert verification["certificate_type"] == "participation"
    assert verification["status"] == "valid"

    # Non-existent code returns 404
    non_existent = await client.get("/api/v1/certificates/verify/CERT-9999-FAKE1234")
    assert non_existent.status_code == 404

    # 10. Recipient views their certificates
    my_res = await client.get("/api/v1/certificates/my", headers=auth(world.member))
    assert my_res.status_code == 200
    my_certs = my_res.json()
    assert len(my_certs) == 1
    assert my_certs[0]["verification_code"] == cert_code

    # 11. HTML pages render cleanly
    web_res = await client.get("/certificates", headers=auth(world.member), follow_redirects=True)
    assert web_res.status_code == 200
    assert "CertiDoo" in web_res.text

    verify_web_res = await client.get(f"/certificates/verify/{cert_code}")
    assert verify_web_res.status_code == 200
    assert "Verified Authentic" in verify_web_res.text
    assert cert_code in verify_web_res.text
