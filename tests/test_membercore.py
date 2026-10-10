"""Real disposable PostgreSQL tests for the pending MemberCore proposal and API."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.database import SessionLocal, asyncpg_dsn
from app.services import fairseat_pricing, membercore

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


@pytest.fixture
def membercore_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "membercore_enabled", True)


async def seed_membership(db, world, *, status="pending", starts_at=None, expires_at=None):
    ids = SimpleNamespace(plan=uuid.uuid4(), membership=uuid.uuid4(), renewal=uuid.uuid4(), dues=uuid.uuid4())
    common = dict(club=world.club.id, user=world.member.id, plan=ids.plan, membership=ids.membership,
                  renewal=ids.renewal, starts=starts_at or NOW - timedelta(days=1),
                  expires=expires_at or NOW + timedelta(days=30))
    await db.execute(text("""INSERT INTO membership_plans
      (id, club_id, name, price_paise, validity_days, ticket_discount_bps)
      VALUES (:plan,:club,'Student plan',12500,31,1250)"""), common)
    await db.execute(text("""INSERT INTO memberships
      (id,user_id,club_id,plan_id,status,starts_at,expires_at,discount_bps_snapshot)
      VALUES (:membership,:user,:club,:plan,'pending',:starts,:expires,1250)"""), common)
    await db.execute(text("""INSERT INTO membership_renewals
      (id,membership_id,club_id,renewal_number,request_id,plan_id,new_starts_at,new_expires_at,discount_bps_snapshot)
      VALUES (:renewal,:membership,:club,1,:renewal,:plan,:starts,:expires,1250)"""), common)
    await db.execute(text("""INSERT INTO membership_dues
      (id,membership_id,club_id,renewal_id,amount_paise,idempotency_key)
      VALUES (:dues,:membership,:club,:renewal,12500,:dues)"""), dict(common, dues=ids.dues))
    if status == "active":
        await db.execute(text("""INSERT INTO membership_activation_grants
          (membership_id,club_id,renewal_id,source,waived_by_user_id,waiver_reason,request_id)
          VALUES (:membership,:club,:renewal,'complimentary_waiver',:actor,'Test waiver',:request)"""),
                         dict(common, actor=world.president.id, request=uuid.uuid4()))
    if status != "pending":
        await db.execute(text("UPDATE memberships SET status=:status WHERE id=:membership"),
                         dict(common, status=status))
    await db.commit()
    return ids


async def reconcile(db, world, ids, outcome):
    event = uuid.uuid4()
    await db.execute(text("""INSERT INTO membership_dues_reconciliations
      (id,dues_id,club_id,source,provider,provider_event_id,request_id,outcome,verified_at)
      VALUES (:id,:dues,:club,'provider_webhook','test-provider',:provider_event,:id,:outcome,:now)"""),
                     dict(id=event, dues=ids.dues, club=world.club.id,
                          provider_event=str(event), outcome=outcome, now=NOW))
    return event


async def paid_grant(db, world, ids, paid):
    await db.execute(text("""INSERT INTO membership_activation_grants
      (membership_id,club_id,renewal_id,source,dues_id,reconciliation_id,request_id)
      VALUES (:membership,:club,:renewal,'paid_settlement',:dues,:paid,:request)"""),
                     dict(membership=ids.membership, club=world.club.id, renewal=ids.renewal,
                          dues=ids.dues, paid=paid, request=uuid.uuid4()))


async def test_source_missing_pending_and_wrong_club_are_ineligible(db, world):
    source = membercore.PostgresMembershipSource()
    kwargs = dict(user_id=world.member.id, club_id=world.club.id, now=NOW)
    assert await source.membership_for(db, **kwargs) is None  # even users.is_active_member=true
    await seed_membership(db, world)
    assert await source.membership_for(db, **kwargs) is None
    assert await source.membership_for(db, **(kwargs | {"club_id": uuid.uuid4()})) is None
    assert await source.membership_for(db, **(kwargs | {"user_id": world.president.id})) is None


@pytest.mark.parametrize("status,delta,expected_reason", [
    ("active", 0, "discount_applied"), ("active", -1, "not_started"),
    ("active", 1000, "expired"), ("cancelled", 0, "inactive_membership"),
    ("expired", 0, "inactive_membership"),
])
async def test_source_uses_existing_calculator_boundaries(db, world, status, delta, expected_reason):
    await seed_membership(db, world, status=status, starts_at=NOW, expires_at=NOW + timedelta(seconds=1))
    now = NOW + timedelta(milliseconds=delta)
    membership = await membercore.PostgresMembershipSource().membership_for(
        db, user_id=world.member.id, club_id=world.club.id, now=now)
    price = fairseat_pricing.calculate_ticket_discount(
        base_price_paise=101, event_club_id=str(world.club.id), membership=membership,
        now_ms=fairseat_pricing.to_ms(now))
    assert price.reason == expected_reason
    assert price.price_paise == (88 if price.eligible else 101)


async def test_plan_edit_does_not_change_snapshot_discount(db, world):
    ids = await seed_membership(db, world, status="active")
    await db.execute(text("UPDATE membership_plans SET ticket_discount_bps=9000 WHERE id=:id"), {"id": ids.plan})
    membership = await membercore.PostgresMembershipSource().membership_for(
        db, user_id=world.member.id, club_id=world.club.id, now=NOW)
    assert membership.discount_bps == 1250


async def test_naive_current_timestamp_rejected_before_query(db, world):
    with pytest.raises(ValueError, match="timezone-aware"):
        await membercore.PostgresMembershipSource().membership_for(
            db, user_id=world.member.id, club_id=world.club.id, now=NOW.replace(tzinfo=None))


async def test_membercore_source_is_explicitly_opt_in(monkeypatch):
    monkeypatch.setattr(get_settings(), "membercore_enabled", False)
    assert not isinstance(fairseat_pricing.get_membership_source(), membercore.PostgresMembershipSource)
    monkeypatch.setattr(get_settings(), "membercore_enabled", True)
    assert isinstance(fairseat_pricing.get_membership_source(), membercore.PostgresMembershipSource)


async def test_me_requires_auth_and_uses_only_callers_application_identity(client, db, world, auth, membercore_enabled):
    assert (await client.get("/api/v1/membercore/me")).status_code == 401
    await seed_membership(db, world, status="active")
    response = await client.get("/api/v1/membercore/me", headers=auth(world.member))
    assert response.status_code == 200
    payload = response.json()
    assert payload["identity"]["application_user_id"] == str(world.member.id)
    assert payload["identity"]["club_id"] == str(world.club.id)
    assert payload["identity"]["role"] == "member"
    assert payload["membership"]["discount_bps_snapshot"] == 1250
    assert "no-store" in response.headers["Cache-Control"]
    # Client-supplied IDs/roles never select a different person's record.
    other = await client.get("/api/v1/membercore/me", headers=auth(world.president),
                             params={"user_id": str(world.member.id), "role": "president"})
    assert other.json()["membership"] is None
    assert other.json()["identity"]["application_user_id"] == str(world.president.id)


async def test_me_pending_is_honest_and_disabled_flag_is_closed(client, db, world, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "membercore_enabled", False)
    assert (await client.get("/api/v1/membercore/me", headers=auth(world.member))).status_code == 404
    monkeypatch.setattr(get_settings(), "membercore_enabled", True)
    await seed_membership(db, world)
    response = await client.get("/api/v1/membercore/me", headers=auth(world.member))
    membership = response.json()["membership"]
    assert membership["status"] == "pending"
    assert membership["eligible"] is False and membership["applied_discount_bps"] == 0
    assert membership["eligibility_reason"] == "pending_membership"


async def test_missing_membership_schema_returns_clear_safe_503(client, db, world, auth, membercore_enabled):
    await db.execute(text("ALTER TABLE memberships RENAME TO unavailable_memberships"))
    await db.commit()
    try:
        response = await client.get("/api/v1/membercore/me", headers=auth(world.member))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "membercore_schema_unavailable"
        assert "SELECT" not in response.text and "unavailable_memberships" not in response.text
    finally:
        await db.execute(text("ALTER TABLE unavailable_memberships RENAME TO memberships"))
        await db.commit()


async def test_refund_blocks_new_grant_and_reactivation(db, world):
    ids = await seed_membership(db, world)
    paid = await reconcile(db, world, ids, "paid")
    await reconcile(db, world, ids, "refunded")
    async with db.begin_nested():
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await paid_grant(db, world, ids, paid)
    assert await db.scalar(text("SELECT status FROM memberships WHERE id=:id"), {"id": ids.membership}) == "pending"


async def test_refunded_existing_grant_cannot_reactivate(db, world):
    ids = await seed_membership(db, world)
    paid = await reconcile(db, world, ids, "paid")
    await paid_grant(db, world, ids, paid)
    await db.execute(text("UPDATE memberships SET status='active' WHERE id=:id"), {"id": ids.membership})
    await reconcile(db, world, ids, "refunded")
    assert await db.scalar(text("SELECT status FROM memberships WHERE id=:id"), {"id": ids.membership}) == "cancelled"
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(text("UPDATE memberships SET status='active' WHERE id=:id"), {"id": ids.membership})


async def test_refund_serializes_with_competing_activation(db, world):
    ids = await seed_membership(db, world)
    paid = await reconcile(db, world, ids, "paid")
    await paid_grant(db, world, ids, paid)
    await db.commit()
    started = asyncio.Event()

    async def activate():
        async with SessionLocal() as contender:
            started.set()
            await contender.execute(text("UPDATE memberships SET status='active' WHERE id=:id"), {"id": ids.membership})
            await contender.commit()

    # Refund owns the aggregate lock before a competing activation can inspect proof.
    await reconcile(db, world, ids, "refunded")
    task = asyncio.create_task(activate())
    await started.wait()
    await db.commit()
    with pytest.raises(IntegrityError):
        await asyncio.wait_for(task, timeout=5)
    assert await db.scalar(text("SELECT status FROM memberships WHERE id=:id"), {"id": ids.membership}) == "pending"


async def test_sql_rollback_assertions_and_pending_copies_match(db):
    root = Path(__file__).resolve().parents[1]
    package = root / "docs" / "membercore" / "phase3c"
    for name in ("030_membercore_identity.sql", "031_membercore_memberships.sql"):
        assert (root / "migrations" / "pending" / name).read_bytes() == (package / "migrations" / "pending" / name).read_bytes()
    sql = "\n".join(line for line in (package / "membercore_migration_assertions.test.sql").read_text(encoding="utf-8").splitlines()
                    if not line.startswith("\\"))
    conn = await asyncpg.connect(asyncpg_dsn(get_settings().database_url))
    try:
        await conn.execute(sql)
        assert await conn.fetchval("SELECT count(*) FROM public.memberships") == 0
    finally:
        await conn.close()
