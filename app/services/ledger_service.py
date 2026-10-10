"""TrustLedger: the one place money movements are written and totalled.

Integration contract for other modules. Call these inside your own transaction
(they flush but never commit), passing your module's row as the reference:

    await ledger_service.record_income(
        db, club_id=..., source_module="event_ticket", amount=Decimal("250"),
        created_by=buyer_id, reference_id=ticket.id, reference_type="ticket",
        payer_id=buyer_id, event_id=event.id, idempotency_key=f"{payment_id}:income")

`record_income`  - dues, ticket sales, merchandise, fundraisers (credit)
`record_expense` - approved reimbursements and direct expenses (debit)
`record_refund`  - cancelled or refunded orders and tickets (debit)

Pass an `idempotency_key` for anything driven by a payment or webhook: a second
call with the same key returns the first entry instead of posting a duplicate.
Entries are immutable; corrections go through `record_adjustment`.
"""

import uuid
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, bad_request, conflict  # noqa: F401  (AppError re-exported for callers)
from app.models.ledger import EXPENSE_CATEGORIES, LedgerEntry
from app.schemas.ledger import EventProfitLoss, LedgerFilters, LedgerSummary, MonthlyTrendPoint
from app.services import audit_service

INCOME_SOURCES = ("membership", "event_ticket", "merchandise", "fundraiser")
ZERO = Decimal("0.00")
TWO_PLACES = Decimal("0.01")

E = LedgerEntry


def to_money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


async def _post(session: AsyncSession, **values) -> LedgerEntry:
    """Insert one entry. With an idempotency key, a repeat returns the entry already posted."""
    values["amount"] = to_money(values["amount"])
    if values["amount"] <= 0:
        raise bad_request("invalid_amount", "Ledger amounts must be greater than zero")
    values.setdefault("currency", get_settings().currency)
    values = {key: value for key, value in values.items() if value is not None}
    values["id"] = uuid.uuid4()

    stmt = (
        pg_insert(LedgerEntry)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(LedgerEntry)
    )
    entry = (await session.scalars(stmt)).first()
    if entry is None:
        entry = await session.scalar(select(LedgerEntry).where(E.idempotency_key == values["idempotency_key"]))
    return entry


async def record_income(
    session: AsyncSession,
    *,
    club_id: uuid.UUID,
    source_module: str,
    amount: Decimal,
    created_by: uuid.UUID,
    reference_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    payer_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    category: str | None = None,
    description: str | None = None,
    payment_status: str = "paid",
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    """Record money coming in (credit): dues, ticket sales, merchandise, fundraisers."""
    return await _post(
        session,
        club_id=club_id,
        entry_type="income",
        direction="credit",
        source_module=source_module,
        amount=amount,
        created_by=created_by,
        reference_id=reference_id,
        reference_type=reference_type,
        payer_id=payer_id,
        event_id=event_id,
        category=category,
        description=description,
        payment_status=payment_status,
        idempotency_key=idempotency_key,
        created_at=created_at,
    )


async def record_expense(
    session: AsyncSession,
    *,
    club_id: uuid.UUID,
    amount: Decimal,
    created_by: uuid.UUID,
    entry_type: str = "reimbursement",
    source_module: str = "expense_claim",
    reference_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    payee_id: uuid.UUID | None = None,
    approved_by: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    category: str | None = None,
    description: str | None = None,
    receipt_url: str | None = None,
    payment_status: str = "approved",
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    """Record money going out (debit). `entry_type` is "reimbursement" or "expense".

    A reimbursement is posted as `approved` and only reduces the balance once
    `set_payment_status(..., "paid")` is called.
    """
    if entry_type not in ("reimbursement", "expense"):
        raise bad_request("invalid_entry_type", "record_expense takes entry_type 'reimbursement' or 'expense'")
    return await _post(
        session,
        club_id=club_id,
        entry_type=entry_type,
        direction="debit",
        source_module=source_module,
        amount=amount,
        created_by=created_by,
        reference_id=reference_id,
        reference_type=reference_type,
        payee_id=payee_id,
        approved_by=approved_by,
        event_id=event_id,
        category=category,
        description=description,
        receipt_url=receipt_url,
        payment_status=payment_status,
        idempotency_key=idempotency_key,
        created_at=created_at,
    )


async def record_refund(
    session: AsyncSession,
    *,
    club_id: uuid.UUID,
    source_module: str,
    amount: Decimal,
    created_by: uuid.UUID,
    reference_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    payee_id: uuid.UUID | None = None,
    approved_by: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    category: str | None = None,
    description: str | None = None,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    """Record money returned to a buyer (debit). The original income entry is left untouched."""
    return await _post(
        session,
        club_id=club_id,
        entry_type="refund",
        direction="debit",
        source_module=source_module,
        amount=amount,
        created_by=created_by,
        reference_id=reference_id,
        reference_type=reference_type,
        payee_id=payee_id,
        approved_by=approved_by,
        event_id=event_id,
        category=category,
        description=description,
        payment_status="paid",
        idempotency_key=idempotency_key,
        created_at=created_at,
    )


async def record_adjustment(
    session: AsyncSession,
    *,
    club_id: uuid.UUID,
    direction: str,
    amount: Decimal,
    created_by: uuid.UUID,
    description: str,
    source_module: str = "manual",
    reference_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    event_id: uuid.UUID | None = None,
    category: str | None = None,
    payment_status: str = "paid",
    idempotency_key: str | None = None,
) -> LedgerEntry:
    """Correct the books with a new entry. This is the only way to fix a posted entry."""
    return await _post(
        session,
        club_id=club_id,
        entry_type="adjustment",
        direction=direction,
        source_module=source_module,
        amount=amount,
        created_by=created_by,
        approved_by=created_by,
        reference_id=reference_id,
        reference_type=reference_type,
        event_id=event_id,
        category=category,
        description=description,
        payment_status=payment_status,
        idempotency_key=idempotency_key,
    )


async def get_entry_for_reference(
    session: AsyncSession, *, source_module: str, reference_id: uuid.UUID, entry_type: str
) -> LedgerEntry | None:
    stmt = (
        select(LedgerEntry)
        .where(E.source_module == source_module, E.reference_id == reference_id, E.entry_type == entry_type)
        .order_by(E.created_at)
        .limit(1)
    )
    return await session.scalar(stmt)


ALLOWED_STATUS_MOVES = {
    "pending": {"approved", "paid", "rejected"},
    "approved": {"paid", "rejected"},
}


async def set_payment_status(
    session: AsyncSession, entry: LedgerEntry, new_status: str, *, actor_id: uuid.UUID | None
) -> LedgerEntry:
    """Move an entry's payment status forward. This is the only field of an entry that can change."""
    if entry.payment_status == new_status:
        return entry
    if new_status not in ALLOWED_STATUS_MOVES.get(entry.payment_status, set()):
        raise conflict(
            "invalid_status_change",
            f"A ledger entry cannot move from {entry.payment_status} to {new_status}",
        )
    old_status = entry.payment_status
    entry.payment_status = new_status
    await session.flush()
    audit_service.record(
        session,
        club_id=entry.club_id,
        actor_id=actor_id,
        action="ledger.payment_status_changed",
        entity_type="ledger_entry",
        entity_id=entry.id,
        details={"from": old_status, "to": new_status},
    )
    return entry


async def create_manual_entry(session: AsyncSession, *, club_id: uuid.UUID, actor_id: uuid.UUID, data) -> LedgerEntry:
    """Treasurer-entered income, expense or adjustment. `data` is a ManualEntryCreate."""
    common = dict(
        club_id=club_id,
        amount=data.amount,
        created_by=actor_id,
        description=data.description,
        category=data.category,
        event_id=data.event_id,
        payment_status=data.payment_status,
    )
    if data.entry_type == "income":
        entry = await record_income(session, source_module=data.source_module, **common)
    elif data.entry_type == "expense":
        entry = await record_expense(
            session, entry_type="expense", source_module=data.source_module, approved_by=actor_id, **common
        )
    else:
        entry = await record_adjustment(
            session, direction=data.direction, source_module=data.source_module, **common
        )
    audit_service.record(
        session,
        club_id=club_id,
        actor_id=actor_id,
        action="ledger.manual_entry_created",
        entity_type="ledger_entry",
        entity_id=entry.id,
        details={"entry_type": entry.entry_type, "direction": entry.direction, "amount": str(entry.amount)},
    )
    return entry


# --- Reading -----------------------------------------------------------------


def _total(*conditions):
    return func.coalesce(func.sum(E.amount).filter(and_(*conditions)), 0)


def _start_of(day) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _end_of(day) -> datetime:
    return datetime.combine(day, time.max, tzinfo=timezone.utc)


def _filter_conditions(club_id: uuid.UUID, filters: LedgerFilters) -> list:
    conditions = [E.club_id == club_id]
    if filters.date_from:
        conditions.append(E.created_at >= _start_of(filters.date_from))
    if filters.date_to:
        conditions.append(E.created_at <= _end_of(filters.date_to))
    for column in ("entry_type", "source_module", "event_id", "payment_status", "category"):
        value = getattr(filters, column)
        if value is not None:
            conditions.append(getattr(E, column) == value)
    if filters.q:
        pattern = f"%{filters.q.strip()}%"
        conditions.append(
            or_(E.description.ilike(pattern), E.category.ilike(pattern), E.reference_type.ilike(pattern))
        )
    return conditions


async def list_entries(
    session: AsyncSession, club_id: uuid.UUID, filters: LedgerFilters
) -> tuple[list[LedgerEntry], int]:
    conditions = _filter_conditions(club_id, filters)
    total = await session.scalar(select(func.count()).select_from(LedgerEntry).where(*conditions))
    stmt = (
        select(LedgerEntry)
        .where(*conditions)
        .order_by(E.created_at.desc(), E.id)
        .limit(filters.limit)
        .offset(filters.offset)
    )
    return list(await session.scalars(stmt)), total or 0


async def get_summary(session: AsyncSession, club_id: uuid.UUID) -> LedgerSummary:
    """Club-wide totals. Only `paid` entries move the balance; `approved` ones are still owed."""
    paid = E.payment_status == "paid"
    spending = E.entry_type.in_(("expense", "reimbursement"))

    totals = (
        await session.execute(
            select(
                _total(paid, E.direction == "credit").label("credits"),
                _total(paid, E.direction == "debit").label("debits"),
                _total(paid, E.entry_type == "income").label("income"),
                _total(paid, spending).label("expenses"),
                _total(paid, E.entry_type == "reimbursement").label("reimbursements_paid"),
                _total(E.payment_status == "approved", E.entry_type == "reimbursement").label("reimbursements_owed"),
                _total(paid, E.entry_type == "refund").label("refunds"),
            ).where(E.club_id == club_id)
        )
    ).one()

    by_source = dict.fromkeys(INCOME_SOURCES, ZERO)
    rows = await session.execute(
        select(E.source_module, func.sum(E.amount))
        .where(E.club_id == club_id, paid, E.entry_type == "income")
        .group_by(E.source_module)
    )
    for source, amount in rows:
        by_source[source] = amount

    by_category = dict.fromkeys(EXPENSE_CATEGORIES, ZERO)
    # Literal SQL constants (not bind parameters) so SELECT and GROUP BY are the same expression.
    category = func.coalesce(E.category, literal_column("'other'"))
    rows = await session.execute(
        select(category, func.sum(E.amount)).where(E.club_id == club_id, paid, spending).group_by(category)
    )
    for category, amount in rows:
        key = category if category in by_category else "other"
        by_category[key] += amount

    month = func.to_char(func.date_trunc(literal_column("'month'"), E.created_at), literal_column("'YYYY-MM'"))
    rows = await session.execute(
        select(
            month.label("month"),
            _total(E.direction == "credit").label("income"),
            _total(E.direction == "debit").label("expenses"),
        )
        .where(E.club_id == club_id, paid)
        .group_by(month)
        .order_by(month.desc())
        .limit(12)
    )
    trend = [
        MonthlyTrendPoint(month=row.month, income=row.income, expenses=row.expenses, net=row.income - row.expenses)
        for row in reversed(rows.all())
    ]

    return LedgerSummary(
        current_balance=totals.credits - totals.debits,
        total_income=totals.income,
        total_expenses=totals.expenses,
        total_reimbursements_paid=totals.reimbursements_paid,
        pending_reimbursements=totals.reimbursements_owed,
        total_refunds=totals.refunds,
        income_by_source=by_source,
        expenses_by_category=by_category,
        monthly_trend=trend,
    )


async def get_event_profit_loss(
    session: AsyncSession, club_id: uuid.UUID, event_id: uuid.UUID, event_name: str | None = None
) -> EventProfitLoss:
    """Profit and loss for one event. Costs count once approved, even if not yet paid out."""
    paid = E.payment_status == "paid"
    income = and_(paid, E.entry_type == "income")
    spending = E.entry_type.in_(("expense", "reimbursement"))
    committed = E.payment_status.in_(("approved", "paid"))
    ticket = E.source_module == "event_ticket"

    row = (
        await session.execute(
            select(
                _total(income, ticket).label("tickets"),
                _total(income, E.source_module == "merchandise").label("merch"),
                _total(income, E.source_module.notin_(("event_ticket", "merchandise"))).label("other"),
                _total(paid, E.entry_type == "refund").label("refunds"),
                _total(committed, spending).label("costs"),
                _total(paid, E.entry_type == "reimbursement").label("reimbursements_paid"),
                _total(E.payment_status == "approved", E.entry_type == "reimbursement").label("reimbursements_owed"),
                _total(paid, E.entry_type == "adjustment", E.direction == "credit").label("adjust_up"),
                _total(paid, E.entry_type == "adjustment", E.direction == "debit").label("adjust_down"),
                func.count().filter(and_(income, ticket)).label("tickets_sold"),
            ).where(E.club_id == club_id, E.event_id == event_id)
        )
    ).one()

    net = row.tickets + row.merch + row.other + row.adjust_up - row.refunds - row.costs - row.adjust_down
    return EventProfitLoss(
        event_id=event_id,
        event_name=event_name,
        ticket_income=row.tickets,
        merchandise_income=row.merch,
        other_income=row.other,
        refunds=row.refunds,
        approved_expenses=row.costs,
        paid_reimbursements=row.reimbursements_paid,
        outstanding_reimbursements=row.reimbursements_owed,
        net_profit_loss=net,
        result="profit" if net > 0 else "loss" if net < 0 else "break_even",
        tickets_sold=row.tickets_sold,
        attendees=None,
    )

