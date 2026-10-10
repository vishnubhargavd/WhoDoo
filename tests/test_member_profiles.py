"""Tests for Task 2: Editable Member Profiles and Automatic Renewal Reminders."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.models.renewal_reminder import MemberRenewalReminder
from app.models.shared import User
from app.services.profile_service import (
    dismiss_renewal_reminder,
    generate_renewal_reminders_for_club,
    get_member_profile,
    update_member_profile,
)


@pytest.fixture
def enable_membercore(monkeypatch):
    monkeypatch.setattr(get_settings(), "membercore_enabled", True)


async def test_member_profile_view_and_update(client, db, world, auth, enable_membercore):
    # 1. Fetch initial profile
    res = await client.get("/api/v1/membercore/profile", headers=auth(world.member))
    assert res.status_code == 200
    data = res.json()
    assert data["email"] == world.member.email
    assert data["role"] == "member"
    assert data["phone"] is None

    # 2. Update permitted fields
    update_payload = {
        "full_name": "Mia Updated",
        "phone": "+91 9876543210",
        "department": "Computer Science & Engineering",
        "academic_year": "3rd Year",
        "profile_image_url": "https://img.example.com/avatar.png",
    }
    put_res = await client.put(
        "/api/v1/membercore/profile",
        headers=auth(world.member),
        json=update_payload,
    )
    assert put_res.status_code == 200
    updated_data = put_res.json()
    assert updated_data["full_name"] == "Mia Updated"
    assert updated_data["phone"] == "+91 9876543210"
    assert updated_data["department"] == "Computer Science & Engineering"
    assert updated_data["academic_year"] == "3rd Year"
    assert updated_data["profile_image_url"] == "https://img.example.com/avatar.png"

    # 3. Verify in PostgreSQL database
    res_db = await db.execute(
        text("SELECT phone, department FROM public.users WHERE id = :id"),
        {"id": world.member.id},
    )
    row = res_db.fetchone()
    assert row.phone == "+91 9876543210"
    assert row.department == "Computer Science & Engineering"


async def test_disallowed_self_editing_fields_rejected(client, db, world, auth, enable_membercore):
    # Attempting to tamper with club role or administrative privileges
    res = await client.put(
        "/api/v1/membercore/profile",
        headers=auth(world.member),
        json={"role": "president"},
    )
    # Extra fields forbidden by schema
    assert res.status_code == 422

    # Attempting to tamper with is_active_member
    res2 = await client.put(
        "/api/v1/membercore/profile",
        headers=auth(world.member),
        json={"is_active_member": True},
    )
    assert res2.status_code == 422


async def test_automatic_renewal_reminders_flow(client, db, world, auth, enable_membercore):
    now = datetime.now(timezone.utc)
    plan_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    renewal_id = uuid.uuid4()
    dues_id = uuid.uuid4()
    starts = now - timedelta(days=360)
    expires = now + timedelta(days=5)

    # 1. Seed plan
    await db.execute(
        text("""
        INSERT INTO public.membership_plans (id, club_id, name, price_paise, validity_days, ticket_discount_bps)
        VALUES (:id, :club_id, 'Annual Pass', 100000, 365, 1000)
        """),
        {"id": plan_id, "club_id": world.club.id},
    )
    # 2. Seed pending membership
    await db.execute(
        text("""
        INSERT INTO public.memberships (id, user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
        VALUES (:id, :user_id, :club_id, :plan_id, 'pending', :starts, :expires, 1000)
        """),
        {
            "id": membership_id,
            "user_id": world.member.id,
            "club_id": world.club.id,
            "plan_id": plan_id,
            "starts": starts,
            "expires": expires,
        },
    )
    # 3. Seed renewal & dues & grant
    await db.execute(
        text("""
        INSERT INTO public.membership_renewals (id, membership_id, club_id, renewal_number, request_id, plan_id, new_starts_at, new_expires_at, discount_bps_snapshot)
        VALUES (:id, :membership_id, :club_id, 1, :id, :plan_id, :starts, :expires, 1000)
        """),
        {"id": renewal_id, "membership_id": membership_id, "club_id": world.club.id, "plan_id": plan_id, "starts": starts, "expires": expires},
    )
    await db.execute(
        text("""
        INSERT INTO public.membership_dues (id, membership_id, club_id, renewal_id, amount_paise, idempotency_key)
        VALUES (:id, :membership_id, :club_id, :renewal_id, 100000, :id)
        """),
        {"id": dues_id, "membership_id": membership_id, "club_id": world.club.id, "renewal_id": renewal_id},
    )
    await db.execute(
        text("""
        INSERT INTO public.membership_activation_grants (membership_id, club_id, renewal_id, source, waived_by_user_id, waiver_reason, request_id)
        VALUES (:membership_id, :club_id, :renewal_id, 'complimentary_waiver', :actor, 'Welcome waiver', :id)
        """),
        {"membership_id": membership_id, "club_id": world.club.id, "renewal_id": renewal_id, "actor": world.president.id, "id": uuid.uuid4()},
    )
    # 4. Activate membership
    await db.execute(
        text("UPDATE public.memberships SET status = 'active' WHERE id = :id"),
        {"id": membership_id},
    )
    await db.commit()

    # Generate reminders (scheduled or API triggered)
    gen_res = await client.post(
        "/api/v1/membercore/reminders/generate",
        headers=auth(world.president),
    )
    assert gen_res.status_code == 200
    assert gen_res.json()["created_count"] >= 1

    # Idempotent re-run does not duplicate
    gen_again = await client.post(
        "/api/v1/membercore/reminders/generate",
        headers=auth(world.president),
    )
    assert gen_again.status_code == 200
    assert gen_again.json()["created_count"] == 0

    # Profile shows active reminder
    prof_res = await client.get("/api/v1/membercore/profile", headers=auth(world.member))
    assert prof_res.status_code == 200
    reminders = prof_res.json()["active_reminders"]
    assert len(reminders) >= 1
    rem = reminders[0]
    assert rem["reminder_stage"] == "7_days"
    assert "Annual Pass" in rem["plan_name"]
    assert rem["is_dismissed"] is False

    # Dismiss reminder
    dismiss_res = await client.post(
        f"/api/v1/membercore/reminders/{rem['id']}/dismiss",
        headers=auth(world.member),
    )
    assert dismiss_res.status_code == 200
    assert dismiss_res.json()["status"] == "dismissed"

    # Profile now has no active reminders
    prof_after = await client.get("/api/v1/membercore/profile", headers=auth(world.member))
    assert len(prof_after.json()["active_reminders"]) == 0


async def test_member_profile_page_renders(client, world, auth):
    res = await client.get("/profile", headers=auth(world.member))
    assert res.status_code == 200
    assert "Member Profile" in res.text
    assert "Display Name" in res.text
    assert world.member.email in res.text
