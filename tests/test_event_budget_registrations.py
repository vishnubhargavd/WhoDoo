"""Tests for Task 3: Event Budgets, Registrations, and Low-Seat Warnings."""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.models.expense import ExpenseClaim
from app.models.fairseat import TicketBooking, TicketCategory
from app.models.shared import Event


async def test_event_create_and_update_with_budget_and_registration_fields(client, db, world, auth):
    now = datetime.now(timezone.utc)
    reg_open = now + timedelta(hours=1)
    reg_close = now + timedelta(days=2)
    start_time = now + timedelta(days=3)
    end_time = now + timedelta(days=3, hours=4)

    # 1. Organizer creates event with budget and registration fields
    create_payload = {
        "name": "Hackathon 2026",
        "description": "Annual 24-hour hackathon",
        "starts_at": start_time.isoformat(),
        "ends_at": end_time.isoformat(),
        "venue": "Main Hall",
        "budget_inr": 50000.00,
        "registration_capacity": 100,
        "registration_opens_at": reg_open.isoformat(),
        "registration_closes_at": reg_close.isoformat(),
    }
    create_res = await client.post(
        "/api/v1/events",
        headers=auth(world.president),
        json=create_payload,
    )
    assert create_res.status_code == 201
    created_data = create_res.json()
    event_id = created_data["id"]
    assert float(created_data["budget_inr"]) == 50000.00
    assert created_data["registration_capacity"] == 100

    # 2. Update event budget & capacity
    patch_res = await client.patch(
        f"/api/v1/events/{event_id}",
        headers=auth(world.president),
        json={"budget_inr": 75000.00, "registration_capacity": 150},
    )
    assert patch_res.status_code == 200
    updated = patch_res.json()
    assert float(updated["budget_inr"]) == 75000.00
    assert updated["registration_capacity"] == 150

    # 3. Verify in database
    db_res = await db.execute(text("SELECT budget_inr, registration_capacity FROM public.events WHERE id = :id"), {"id": event_id})
    row = db_res.fetchone()
    assert row.budget_inr == Decimal("75000.00")
    assert row.registration_capacity == 150


async def test_event_budget_dashboard_and_over_budget_warning(client, db, world, auth):
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4()

    # 1. Seed event with budget of ₹2,000.00
    await db.execute(
        text("""
        INSERT INTO public.events (id, club_id, name, budget_inr, starts_at, ends_at, venue, status, created_by, lead_id)
        VALUES (:id, :club_id, 'Stage Play', 2000.00, :starts, :ends, 'Auditorium', 'published', :user_id, :user_id)
        """),
        {
            "id": event_id,
            "club_id": world.club.id,
            "starts": now + timedelta(days=2),
            "ends": now + timedelta(days=2, hours=3),
            "user_id": world.president.id,
        },
    )
    # Add approved claim of ₹1,500.00
    await db.execute(
        text("""
        INSERT INTO public.expense_claims (id, club_id, submitted_by, event_id, title, amount, expense_date, receipt_url, category, status)
        VALUES (:id, :club_id, :user_id, :event_id, 'Costumes', 1500.00, :exp_date, 'https://example.com/rcpt1.jpg', 'supplies', 'approved')
        """),
        {
            "id": uuid.uuid4(),
            "club_id": world.club.id,
            "user_id": world.member.id,
            "event_id": event_id,
            "exp_date": date(2026, 10, 5),
        },
    )
    await db.commit()

    # Budget dashboard within budget
    res = await client.get(f"/api/v1/events/{event_id}/budget", headers=auth(world.president))
    assert res.status_code == 200
    data = res.json()
    assert float(data["budget_allocated_inr"]) == 2000.00
    assert float(data["expenses_approved_inr"]) == 1500.00
    assert float(data["remaining_budget_inr"]) == 500.00
    assert data["is_over_budget"] is False

    # Add second claim of ₹800.00, exceeding budget
    await db.execute(
        text("""
        INSERT INTO public.expense_claims (id, club_id, submitted_by, event_id, title, amount, expense_date, receipt_url, category, status)
        VALUES (:id, :club_id, :user_id, :event_id, 'Stage Props', 800.00, :exp_date, 'https://example.com/rcpt2.jpg', 'supplies', 'approved')
        """),
        {
            "id": uuid.uuid4(),
            "club_id": world.club.id,
            "user_id": world.member.id,
            "event_id": event_id,
            "exp_date": date(2026, 10, 6),
        },
    )
    await db.commit()

    # Budget dashboard now reports over-budget warning
    res2 = await client.get(f"/api/v1/events/{event_id}/budget", headers=auth(world.president))
    assert res2.status_code == 200
    data2 = res2.json()
    assert float(data2["expenses_approved_inr"]) == 2300.00
    assert float(data2["remaining_budget_inr"]) == -300.00
    assert data2["is_over_budget"] is True
    assert float(data2["over_budget_amount_inr"]) == 300.00


async def test_event_registration_stats_and_low_seat_warnings(client, db, world, auth):
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4()
    cat_id = uuid.uuid4()

    # 1. Seed event and ticket category (capacity 20)
    await db.execute(
        text("""
        INSERT INTO public.events (id, club_id, name, registration_capacity, starts_at, ends_at, venue, status, created_by, lead_id)
        VALUES (:id, :club_id, 'Robotics Workshop', 20, :starts, :ends, 'Lab', 'published', :user_id, :user_id)
        """),
        {
            "id": event_id,
            "club_id": world.club.id,
            "starts": now + timedelta(days=2),
            "ends": now + timedelta(days=2, hours=3),
            "user_id": world.president.id,
        },
    )
    await db.execute(
        text("""
        INSERT INTO public.event_ticket_settings (event_id, club_id, sales_enabled, capacity, opens_at, closes_at, max_per_booking, created_by, updated_by)
        VALUES (:event_id, :club_id, true, 20, :opens, :closes, 6, :user_id, :user_id)
        """),
        {
            "event_id": event_id,
            "club_id": world.club.id,
            "opens": now - timedelta(days=1),
            "closes": now + timedelta(days=2),
            "user_id": world.president.id,
        },
    )
    await db.execute(
        text("""
        INSERT INTO public.ticket_categories (id, event_id, name, base_price_paise, pricing_version, capacity, active, member_discount_eligible, created_by)
        VALUES (:id, :event_id, 'General Admission', 25000, 1, 20, true, true, :user_id)
        """),
        {"id": cat_id, "event_id": event_id, "user_id": world.president.id},
    )
    await db.commit()

    # Initial stats: fully available
    res1 = await client.get(f"/api/v1/events/{event_id}/registration-stats", headers=auth(world.member))
    assert res1.status_code == 200
    s1 = res1.json()
    assert s1["confirmed_registrations"] == 0
    assert s1["remaining_seats"] == 20
    assert s1["is_low_seats"] is False
    assert s1["is_sold_out"] is False
    assert s1["status"] == "AVAILABLE"

    # Seed confirmed booking of 18 seats (remaining = 2, threshold <= 2)
    quote_id = uuid.uuid4()
    await db.execute(
        text("""
        INSERT INTO public.ticket_quotes (id, purchaser_user_id, event_id, ticket_category_id, quantity, base_unit_price_paise, unit_price_paise, total_price_paise, member_eligible, applied_discount_bps, discount_applied, discount_reason, discount_explanation, calculator_version, category_pricing_version, issued_at, expires_at)
        VALUES (:id, :user_id, :event_id, :cat_id, 18, 25000, 25000, 450000, false, 0, false, '', '', 'test', 1, :now, :exp)
        """),
        {"id": quote_id, "user_id": world.member.id, "event_id": event_id, "cat_id": cat_id, "now": now, "exp": now + timedelta(minutes=15)},
    )
    await db.execute(
        text("""
        INSERT INTO public.ticket_bookings (id, event_id, ticket_category_id, purchaser_user_id, quote_id, quantity, unit_price_paise, total_price_paise, currency, member_eligible, applied_discount_bps, discount_applied, discount_reason, discount_explanation, status, payment_status, stripe_payment_intent_id, paid_at, created_at, updated_at)
        VALUES (:id, :event_id, :cat_id, :user_id, :quote_id, 18, 25000, 450000, 'INR', false, 0, false, '', '', 'confirmed', 'paid', :pi, :now, :now, :now)
        """),
        {"id": uuid.uuid4(), "event_id": event_id, "cat_id": cat_id, "user_id": world.member.id, "quote_id": quote_id, "pi": f"pi_{uuid.uuid4().hex[:12]}", "now": now},
    )
    await db.commit()

    # Low-seats alert triggered
    res2 = await client.get(f"/api/v1/events/{event_id}/registration-stats", headers=auth(world.member))
    assert res2.status_code == 200
    s2 = res2.json()
    assert s2["confirmed_registrations"] == 18
    assert s2["remaining_seats"] == 2
    assert s2["is_low_seats"] is True
    assert s2["is_sold_out"] is False
    assert s2["status"] == "LOW_SEATS"

    # Book remaining 2 seats (capacity full)
    quote2_id = uuid.uuid4()
    await db.execute(
        text("""
        INSERT INTO public.ticket_quotes (id, purchaser_user_id, event_id, ticket_category_id, quantity, base_unit_price_paise, unit_price_paise, total_price_paise, member_eligible, applied_discount_bps, discount_applied, discount_reason, discount_explanation, calculator_version, category_pricing_version, issued_at, expires_at)
        VALUES (:id, :user_id, :event_id, :cat_id, 2, 25000, 25000, 50000, false, 0, false, '', '', 'test', 1, :now, :exp)
        """),
        {"id": quote2_id, "user_id": world.member.id, "event_id": event_id, "cat_id": cat_id, "now": now, "exp": now + timedelta(minutes=15)},
    )
    await db.execute(
        text("""
        INSERT INTO public.ticket_bookings (id, event_id, ticket_category_id, purchaser_user_id, quote_id, quantity, unit_price_paise, total_price_paise, currency, member_eligible, applied_discount_bps, discount_applied, discount_reason, discount_explanation, status, payment_status, stripe_payment_intent_id, paid_at, created_at, updated_at)
        VALUES (:id, :event_id, :cat_id, :user_id, :quote_id, 2, 25000, 50000, 'INR', false, 0, false, '', '', 'confirmed', 'paid', :pi, :now, :now, :now)
        """),
        {"id": uuid.uuid4(), "event_id": event_id, "cat_id": cat_id, "user_id": world.member.id, "quote_id": quote2_id, "pi": f"pi_{uuid.uuid4().hex[:12]}", "now": now},
    )
    await db.commit()

    # Sold-out status
    res3 = await client.get(f"/api/v1/events/{event_id}/registration-stats", headers=auth(world.member))
    assert res3.status_code == 200
    s3 = res3.json()
    assert s3["confirmed_registrations"] == 20
    assert s3["remaining_seats"] == 0
    assert s3["is_sold_out"] is True
    assert s3["status"] == "SOLD_OUT"


async def test_registration_deadline_closed_warning(client, db, world, auth):
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4()

    # Seed event with registration deadline in the past
    await db.execute(
        text("""
        INSERT INTO public.events (id, club_id, name, registration_capacity, registration_closes_at, starts_at, ends_at, venue, status, created_by, lead_id)
        VALUES (:id, :club_id, 'Past Deadline Event', 50, :closes, :starts, :ends, 'Room 1', 'published', :user_id, :user_id)
        """),
        {
            "id": event_id,
            "club_id": world.club.id,
            "closes": now - timedelta(hours=2),
            "starts": now + timedelta(days=2),
            "ends": now + timedelta(days=2, hours=3),
            "user_id": world.president.id,
        },
    )
    await db.commit()

    res = await client.get(f"/api/v1/events/{event_id}/registration-stats", headers=auth(world.member))
    assert res.status_code == 200
    stats = res.json()
    assert stats["is_registration_closed"] is True
    assert stats["status"] == "REGISTRATION_CLOSED"
    assert "Closed" in stats["status_label"]


async def test_event_detail_page_renders_budget_and_registration_cards(client, db, world, auth):
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4()
    await db.execute(
        text("""
        INSERT INTO public.events (id, club_id, name, budget_inr, registration_capacity, starts_at, ends_at, venue, status, created_by, lead_id)
        VALUES (:id, :club_id, 'Science Fair', 5000.00, 150, :starts, :ends, 'Main Hall', 'published', :user_id, :user_id)
        """),
        {
            "id": event_id,
            "club_id": world.club.id,
            "starts": now + timedelta(days=2),
            "ends": now + timedelta(days=2, hours=3),
            "user_id": world.president.id,
        },
    )
    await db.commit()

    res = await client.get(f"/events/{event_id}", headers=auth(world.president))
    assert res.status_code == 200
    assert "Science Fair" in res.text
    assert "Registrations &amp; Availability" in res.text or "Registrations & Availability" in res.text
    assert "Event Budget Dashboard" in res.text
    assert "Budget allocated" in res.text

