"""Tests for WhoDoo Connect: Club Announcements and Notifications."""

import pytest

API = "/api/v1/announcements"


async def test_officer_creates_and_members_read_announcements(client, auth, world):
    # Member cannot publish
    denied = await client.post(
        API,
        headers=auth(world.member),
        json={"title": "Unauthorized post", "content": "Should fail", "target_audience": "all"},
    )
    assert denied.status_code == 403

    # President publishes an announcement
    created = await client.post(
        API,
        headers=auth(world.president),
        json={
            "title": "Welcome to the New Academic Semester",
            "content": "All members are invited to our first general meeting on Friday at 5 PM in Main Auditorium.",
            "target_audience": "all",
            "is_pinned": True,
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["title"] == "Welcome to the New Academic Semester"
    assert data["is_pinned"] is True
    assert data["target_audience"] == "all"
    announcement_id = data["id"]

    # Members can view the announcement
    listing = await client.get(API, headers=auth(world.member))
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) >= 1
    assert any(item["id"] == announcement_id for item in items)

    # President can delete the announcement
    deleted = await client.delete(f"{API}/{announcement_id}", headers=auth(world.president))
    assert deleted.status_code == 204

    # Verify deleted
    listing_after = await client.get(API, headers=auth(world.member))
    assert not any(item["id"] == announcement_id for item in listing_after.json())
