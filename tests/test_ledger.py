from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models.ledger import AuditLog, LedgerEntry
from app.services import ledger_service


async def _post_sample_books(db, world):
    """Dues 500 + tickets 2x250 + fundraiser 1000 in; 300 expense out; one 850 reimbursement only approved."""
    common = dict(club_id=world.club.id, created_by=world.treasurer.id)
    await ledger_service.record_income(db, source_module="membership", amount=Decimal("500"), **common)
    for _ in range(2):
        await ledger_service.record_income(
            db, source_module="event_ticket", amount=Decimal("250"), event_id=world.bake_sale.id, **common
        )
    await ledger_service.record_income(db, source_module="fundraiser", amount=Decimal("1000"), **common)
    await ledger_service.record_expense(
        db, entry_type="expense", source_module="manual", amount=Decimal("300"), category="printing",
        payment_status="paid", event_id=world.bake_sale.id, **common,
    )
    owed = await ledger_service.record_expense(
        db, amount=Decimal("850"), category="supplies", event_id=world.bake_sale.id, **common
    )
    await db.commit()
    return owed


async def test_balance_counts_only_paid_entries(db, world):
    owed = await _post_sample_books(db, world)

    summary = await ledger_service.get_summary(db, world.club.id)
    assert summary.total_income == Decimal("2000.00")
    assert summary.total_expenses == Decimal("300.00")
    assert summary.current_balance == Decimal("1700.00")
    assert summary.pending_reimbursements == Decimal("850.00")
    assert summary.total_reimbursements_paid == Decimal("0")
    assert summary.income_by_source == {
        "membership": Decimal("500.00"),
        "event_ticket": Decimal("500.00"),
        "merchandise": Decimal("0.00"),
        "fundraiser": Decimal("1000.00"),
    }
    assert summary.expenses_by_category["printing"] == Decimal("300.00")

    # Paying the reimbursement is what moves the balance.
    await ledger_service.set_payment_status(db, owed, "paid", actor_id=world.treasurer.id)
    await db.commit()

    summary = await ledger_service.get_summary(db, world.club.id)
    assert summary.current_balance == Decimal("850.00")
    assert summary.total_reimbursements_paid == Decimal("850.00")
    assert summary.pending_reimbursements == Decimal("0")
    assert summary.expenses_by_category["supplies"] == Decimal("850.00")
    assert len(summary.monthly_trend) == 1
    assert summary.monthly_trend[0].net == Decimal("850.00")


async def test_refunds_and_adjustments_move_the_balance(db, world):
    common = dict(club_id=world.club.id, created_by=world.treasurer.id)
    await ledger_service.record_income(db, source_module="merchandise", amount=Decimal("799"), **common)
    await ledger_service.record_refund(db, source_module="merchandise", amount=Decimal("799"), **common)
    await ledger_service.record_adjustment(db, direction="credit", amount=Decimal("40"), description="Cash count", **common)
    await db.commit()

    summary = await ledger_service.get_summary(db, world.club.id)
    assert summary.total_income == Decimal("799.00")
    assert summary.total_refunds == Decimal("799.00")
    assert summary.current_balance == Decimal("40.00")


async def test_event_profit_and_loss(db, world):
    await _post_sample_books(db, world)

    result = await ledger_service.get_event_profit_loss(db, world.club.id, world.bake_sale.id)
    assert result.ticket_income == Decimal("500.00")
    assert result.tickets_sold == 2
    # Costs count once approved: 300 paid + 850 approved but not yet paid out.
    assert result.approved_expenses == Decimal("1150.00")
    assert result.outstanding_reimbursements == Decimal("850.00")
    assert result.net_profit_loss == Decimal("-650.00")
    assert result.result == "loss"

    empty = await ledger_service.get_event_profit_loss(db, world.club.id, world.fest.id)
    assert empty.net_profit_loss == 0 and empty.result == "break_even"


async def test_entries_are_immutable_in_the_database(db, world):
    entry = await ledger_service.record_income(
        db, club_id=world.club.id, source_module="membership", amount=Decimal("500"), created_by=world.treasurer.id
    )
    entry_id = entry.id
    await db.commit()

    with pytest.raises(DBAPIError, match="immutable"):
        await db.execute(text("UPDATE ledger_entries SET amount = 1 WHERE id = :id"), {"id": entry_id})
    await db.rollback()

    with pytest.raises(DBAPIError, match="immutable"):
        await db.execute(text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id})
    await db.rollback()

    assert await db.scalar(select(LedgerEntry.amount).where(LedgerEntry.id == entry_id)) == Decimal("500.00")


async def test_paid_entries_cannot_change_status(db, world):
    entry = await ledger_service.record_income(
        db, club_id=world.club.id, source_module="membership", amount=Decimal("500"), created_by=world.treasurer.id
    )
    with pytest.raises(ledger_service.AppError) as error:
        await ledger_service.set_payment_status(db, entry, "pending", actor_id=world.treasurer.id)
    assert error.value.code == "invalid_status_change"


async def test_idempotency_key_prevents_duplicates_but_allows_refunds(db, world):
    kwargs = dict(
        club_id=world.club.id, source_module="merchandise", amount=Decimal("799"),
        created_by=world.member.id, reference_id=world.member.id,
    )
    first = await ledger_service.record_income(db, idempotency_key="pay_123:income", **kwargs)
    again = await ledger_service.record_income(db, idempotency_key="pay_123:income", **kwargs)
    refund = await ledger_service.record_refund(db, idempotency_key="pay_123:refund", **kwargs)
    await db.commit()

    assert again.id == first.id
    assert refund.id != first.id
    assert await db.scalar(select(func.count()).select_from(LedgerEntry)) == 2


async def test_ledger_api_is_for_finance_admins_only(client, world, auth):
    assert (await client.get("/api/v1/ledger/summary")).status_code == 401
    for person in (world.volunteer, world.member, world.lead):
        response = await client.get("/api/v1/ledger/summary", headers=auth(person))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
    for person in (world.treasurer, world.president):
        assert (await client.get("/api/v1/ledger/summary", headers=auth(person))).status_code == 200


async def test_manual_entry_and_filters(client, db, world, auth):
    headers = auth(world.treasurer)
    created = await client.post(
        "/api/v1/ledger/manual-entry",
        headers=headers,
        json={"entry_type": "income", "amount": "1500.00", "description": "Alumni donation",
              "source_module": "fundraiser", "event_id": str(world.fest.id)},
    )
    assert created.status_code == 201, created.text
    assert created.json()["direction"] == "credit"
    assert created.json()["amount"] == 1500.0

    await client.post(
        "/api/v1/ledger/manual-entry", headers=headers,
        json={"entry_type": "expense", "amount": 200, "description": "Banner printing", "category": "printing"},
    )
    await client.post(
        "/api/v1/ledger/manual-entry", headers=headers,
        json={"entry_type": "adjustment", "direction": "debit", "amount": 50, "description": "Cash box shortfall"},
    )

    summary = (await client.get("/api/v1/ledger/summary", headers=headers)).json()
    assert summary["current_balance"] == 1250.0
    assert summary["income_by_source"]["fundraiser"] == 1500.0

    async def count(**params):
        page = (await client.get("/api/v1/ledger/entries", headers=headers, params=params)).json()
        return page["total"]

    assert await count() == 3
    assert await count(entry_type="expense") == 1
    assert await count(source_module="fundraiser") == 1
    assert await count(event_id=str(world.fest.id)) == 1
    assert await count(category="printing") == 1
    assert await count(q="banner") == 1
    assert await count(payment_status="pending") == 0
    assert await count(date_from="2099-01-01") == 0

    # Every manual entry leaves an audit row.
    assert await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "ledger.manual_entry_created")
    ) == 3


async def test_manual_entry_validation(client, world, auth):
    headers = auth(world.treasurer)
    no_direction = await client.post(
        "/api/v1/ledger/manual-entry", headers=headers,
        json={"entry_type": "adjustment", "amount": 10, "description": "Missing direction"},
    )
    assert no_direction.status_code == 422
    assert no_direction.json()["error"]["code"] == "validation_error"

    negative = await client.post(
        "/api/v1/ledger/manual-entry", headers=headers,
        json={"entry_type": "income", "amount": -5, "description": "Negative"},
    )
    assert negative.status_code == 422


async def test_profit_loss_api(client, db, world, auth):
    await _post_sample_books(db, world)
    response = await client.get(
        f"/api/v1/ledger/events/{world.bake_sale.id}/profit-loss", headers=auth(world.treasurer)
    )
    assert response.status_code == 200
    assert response.json()["net_profit_loss"] == -650.0
    assert response.json()["event_name"] == "Bake Sale"

    missing = await client.get(
        "/api/v1/ledger/events/00000000-0000-0000-0000-000000000000/profit-loss", headers=auth(world.treasurer)
    )
    assert missing.status_code == 404
