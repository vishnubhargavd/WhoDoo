"""LegacyVault verification tests:
1. Authorized officers create handover records across categories.
2. Incoming/newly authorized leadership can access historical records.
3. Club isolation is strictly enforced.
4. Ordinary members cannot access or create leadership records (HTTP 403).
5. Audit trail preserves author, timestamps, and references.
6. Continuity snapshot reuses existing event and ledger data without duplication.
7. Web interface renders for authorized officers.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.shared import Club, Event, User


async def test_legacyvault_complete_workflow(client, db, world, auth):
    # 1. Create a past event in the club
    event = Event(
        id=uuid.uuid4(),
        club_id=world.president.club_id,
        name="Tech Symposium 2025",
        starts_at=datetime(2025, 9, 15, 10, 0, tzinfo=timezone.utc),
        status="completed",
    )
    db.add(event)
    await db.commit()

    # 2. Ordinary member attempts to create a handover record -> 403 Forbidden
    member_post = await client.post(
        "/api/v1/legacyvault/records",
        headers=auth(world.member),
        json={
            "term_year": "2025-2026",
            "title": "Unauthorized Note",
            "category": "handover_note",
            "content": "A member should not be able to write leadership records.",
        },
    )
    assert member_post.status_code == 403

    # Ordinary member attempts to view handover records -> 403 Forbidden
    member_get = await client.get("/api/v1/legacyvault/records", headers=auth(world.member))
    assert member_get.status_code == 403

    # Ordinary member attempts to view web page -> 403 Forbidden
    member_web = await client.get("/legacyvault", headers=auth(world.member))
    assert member_web.status_code == 403

    # 3. Outgoing President creates handover note
    pres_rec_res = await client.post(
        "/api/v1/legacyvault/records",
        headers=auth(world.president),
        json={
            "term_year": "2025-2026",
            "title": "Presidential Handover & Major Sponsors Rolodex",
            "category": "handover_note",
            "content": "Key sponsor contacts: TechCorp (contact: priya@techcorp.example). Remember to book the auditorium 3 months early.",
            "event_id": str(event.id),
        },
    )
    assert pres_rec_res.status_code == 201
    pres_rec = pres_rec_res.json()
    assert pres_rec["title"] == "Presidential Handover & Major Sponsors Rolodex"
    assert pres_rec["author_id"] == str(world.president.id)
    assert pres_rec["author_name"] == world.president.full_name
    assert pres_rec["event_id"] == str(event.id)
    assert pres_rec["event_name"] == "Tech Symposium 2025"
    pres_rec_id = pres_rec["id"]

    # 4. Outgoing Treasurer creates finance summary
    treas_rec_res = await client.post(
        "/api/v1/legacyvault/records",
        headers=auth(world.treasurer),
        json={
            "term_year": "2025-2026",
            "title": "Annual Financial Audit & Vendor Accounts Guide",
            "category": "finance_summary",
            "content": "All expense claims require 2 signatures for >= 1000 INR. Keep receipts backed in Supabase storage.",
        },
    )
    assert treas_rec_res.status_code == 201
    treas_rec = treas_rec_res.json()
    assert treas_rec["category"] == "finance_summary"

    # 5. Newly elected incoming Event Lead accesses historical archives
    list_res = await client.get(
        "/api/v1/legacyvault/records",
        headers=auth(world.lead),
    )
    assert list_res.status_code == 200
    records = list_res.json()
    assert len(records) >= 2
    record_titles = [r["title"] for r in records]
    assert "Presidential Handover & Major Sponsors Rolodex" in record_titles
    assert "Annual Financial Audit & Vendor Accounts Guide" in record_titles

    # Filter by term year
    term_filter_res = await client.get(
        "/api/v1/legacyvault/records?term_year=2025-2026",
        headers=auth(world.lead),
    )
    assert term_filter_res.status_code == 200
    assert len(term_filter_res.json()) >= 2

    # Filter by category
    cat_filter_res = await client.get(
        "/api/v1/legacyvault/records?category=finance_summary",
        headers=auth(world.lead),
    )
    assert cat_filter_res.status_code == 200
    assert len(cat_filter_res.json()) == 1
    assert cat_filter_res.json()[0]["category"] == "finance_summary"

    # 6. Retrieve single record with audit trail
    single_res = await client.get(
        f"/api/v1/legacyvault/records/{pres_rec_id}",
        headers=auth(world.lead),
    )
    assert single_res.status_code == 200
    single_data = single_res.json()
    assert single_data["id"] == pres_rec_id
    assert single_data["author_name"] == world.president.full_name
    assert "created_at" in single_data
    assert "updated_at" in single_data

    # 7. Continuity snapshot aggregates existing data without duplicating tables
    snapshot_res = await client.get(
        "/api/v1/legacyvault/snapshot",
        headers=auth(world.president),
    )
    assert snapshot_res.status_code == 200
    snap = snapshot_res.json()
    assert snap["total_records"] >= 2
    assert snap["past_events_count"] >= 1
    assert snap["active_officers_count"] >= 3  # president, treasurer, leads

    # 8. Club Isolation: Second club cannot view or access Club 1's records
    club2 = Club(id=uuid.uuid4(), name="Rival Robotics Club")
    db.add(club2)
    club2_pres = User(
        id=uuid.uuid4(),
        club_id=club2.id,
        full_name="Rita Rival",
        email="rita@rival.example",
        role="president",
        is_active_member=True,
    )
    db.add(club2_pres)
    await db.commit()

    club2_list = await client.get(
        "/api/v1/legacyvault/records",
        headers=auth(club2_pres),
    )
    assert club2_list.status_code == 200
    assert len(club2_list.json()) == 0  # Club 2 has no records; cannot see Club 1's records

    club2_get_single = await client.get(
        f"/api/v1/legacyvault/records/{pres_rec_id}",
        headers=auth(club2_pres),
    )
    assert club2_get_single.status_code == 404  # Not found for Club 2

    # 9. HTML pages render cleanly for officers
    web_res = await client.get("/legacyvault", headers=auth(world.president), follow_redirects=True)
    assert web_res.status_code == 200
    assert "LegacyVault" in web_res.text
    assert "Presidential Handover" in web_res.text
