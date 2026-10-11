"""Cash flow for the treasurer: one event's money in and out, and the club's money overall.

Nothing is stored here. Every figure is read from TrustLedger (ledger_entries), which is the one
record of money that has moved or been promised, plus expense_claims for claims still waiting
for approval (those are not in the ledger yet). So these views can never disagree with the books.

    received    income entries that are paid
    refunded    refund entries that are paid
    paid out    expense and reimbursement entries that are paid
    committed   expense and reimbursement entries approved but not yet paid
    pending     expense claims waiting for approval
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser
from app.core.errors import forbidden, not_found
from app.models.expense import ExpenseClaim
from app.models.ledger import EXPENSE_CATEGORIES, LedgerEntry
from app.models.merch import MerchOrder, OrderItem
from app.models.shared import Event
from app.services import event_insights

E = LedgerEntry
ZERO = Decimal("0.00")
SPENDING = ("expense", "reimbursement")
# How income is grouped for people, from the ledger's source_module.
SOURCES = (
    ("memberships", "Memberships", ("membership",)),
    ("events", "Event registrations and tickets", ("event_ticket",)),
    ("merchandise", "Merchandise", ("merchandise",)),
    ("other", "Other income", ("fundraiser", "manual", "expense_claim")),
)


def _sum(*conditions):
    return func.coalesce(func.sum(E.amount).filter(*conditions), 0)


def _paise(amount) -> int:
    return int((Decimal(amount or 0) * 100).to_integral_value())


def _rupees(paise: int | None) -> Decimal:
    return (Decimal(paise or 0) / 100).quantize(ZERO)


async def event_cash_flow(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> dict:
    """Everything that has come in and gone out for one event, against its budget."""
    if not user.is_finance_admin:
        raise forbidden("Only the treasurer and the president can see an event's cash flow")
    event = await db.get(Event, event_id)
    if event is None or event.club_id != user.club_id:
        raise not_found("Event")
    here = (E.club_id == event.club_id, E.event_id == event.id)
    paid = E.payment_status == "paid"
    income = (paid, E.entry_type == "income")
    tickets = E.source_module == "event_ticket"
    registration = E.reference_type == "event_registration"

    row = (await db.execute(select(
        _sum(*income, tickets, registration).label("registrations"),
        func.count().filter(*income, tickets, registration).label("registration_count"),
        _sum(*income, tickets, E.reference_type.is_distinct_from("event_registration")).label("tickets"),
        func.count().filter(*income, tickets, E.reference_type.is_distinct_from("event_registration")).label("ticket_count"),
        _sum(*income, E.source_module == "merchandise").label("merchandise"),
        _sum(*income, E.source_module.notin_(("event_ticket", "merchandise"))).label("other"),
        _sum(paid, E.entry_type == "refund").label("refunds"),
        _sum(paid, E.entry_type.in_(SPENDING)).label("paid_out"),
        _sum(E.payment_status == "approved", E.entry_type.in_(SPENDING)).label("committed"),
    ).where(*here))).one()

    # Money out by kind of expense: paid and committed from the ledger, pending from the claims.
    lines = {name: {"category": name, "paid": ZERO, "committed": ZERO, "pending": ZERO} for name in EXPENSE_CATEGORIES}
    for category, status, amount in (await db.execute(
        select(E.category, E.payment_status, func.sum(E.amount))
        .where(*here, E.entry_type.in_(SPENDING), E.payment_status.in_(("paid", "approved")))
        .group_by(E.category, E.payment_status)
    )).all():
        category = category or "other"
        line = lines.setdefault(category, {"category": category, "paid": ZERO, "committed": ZERO, "pending": ZERO})
        line["paid" if status == "paid" else "committed"] += amount
    pending_total = ZERO
    for category, amount in (await db.execute(
        select(ExpenseClaim.category, func.sum(ExpenseClaim.amount))
        .where(ExpenseClaim.club_id == event.club_id, ExpenseClaim.event_id == event.id,
               ExpenseClaim.status.in_(("pending_approval", "change_requested")))
        .group_by(ExpenseClaim.category)
    )).all():
        lines.setdefault(category, {"category": category, "paid": ZERO, "committed": ZERO, "pending": ZERO})["pending"] += amount
        pending_total += amount

    money_in = row.registrations + row.tickets + row.merchandise + row.other
    net = money_in - row.refunds - row.paid_out
    entries = list(await db.scalars(select(E).where(*here).order_by(E.created_at.desc()).limit(200)))
    return {
        "event": event,
        "in": {
            "registrations": row.registrations, "registration_count": row.registration_count,
            "tickets": row.tickets, "ticket_count": row.ticket_count,
            "merchandise": row.merchandise, "other": row.other, "total": money_in,
        },
        "refunds": row.refunds,
        "out": {"paid": row.paid_out, "committed": row.committed, "pending": pending_total,
                "lines": [line for line in lines.values() if any((line["paid"], line["committed"], line["pending"]))]},
        # What is in hand now, and what it becomes once everything approved and waiting is paid.
        "net": net,
        "net_after_commitments": net - row.committed - pending_total,
        "budget": await event_insights.budget(db, user, event.id),
        "entries": entries,
    }


async def overview(db: AsyncSession, club_id: uuid.UUID) -> dict:
    """Where the club's money came from and went, across everything."""
    paid = E.payment_status == "paid"
    by_source = dict((await db.execute(
        select(E.source_module, func.sum(E.amount))
        .where(E.club_id == club_id, paid, E.entry_type == "income").group_by(E.source_module)
    )).all())
    sources = [
        {"key": key, "label": label, "amount": sum((by_source.get(module, ZERO) for module in modules), ZERO)}
        for key, label, modules in SOURCES
    ]
    known = {module for _, _, modules in SOURCES for module in modules}
    sources[-1]["amount"] += sum((amount for module, amount in by_source.items() if module not in known), ZERO)

    totals = (await db.execute(select(
        _sum(paid, E.entry_type == "refund").label("refunds"),
        _sum(paid, E.entry_type.in_(SPENDING)).label("paid_out"),
        _sum(E.payment_status == "approved", E.entry_type.in_(SPENDING)).label("committed"),
        _sum(paid, E.entry_type == "refund", E.source_module == "merchandise").label("merch_refunds"),
    ).where(E.club_id == club_id))).one()

    orders = (await db.execute(
        select(func.count(func.distinct(MerchOrder.id)), func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(OrderItem, OrderItem.order_id == MerchOrder.id)
        .where(MerchOrder.club_id == club_id, MerchOrder.payment_status == "paid")
    )).one()
    generated = sum((source["amount"] for source in sources), ZERO)
    return {
        "sources": sources,
        "generated": generated,
        "refunds": totals.refunds,
        "paid_out": totals.paid_out,
        "committed": totals.committed,
        "net": generated - totals.refunds - totals.paid_out,
        "merchandise": {
            "orders": orders[0], "items": orders[1],
            "revenue": next(source["amount"] for source in sources if source["key"] == "merchandise"),
            "refunds": totals.merch_refunds,
        },
    }


async def event_rows(db: AsyncSession, user: CurrentUser, events: list[Event]) -> list[dict]:
    """One line per event for the dashboard: in, out, net, and how the budget stands."""
    rows = []
    for event in events:
        flow = await event_cash_flow(db, user, event.id)
        budget = flow["budget"]
        rows.append({
            "event": event, "money_in": flow["in"]["total"] - flow["refunds"],
            "money_out": flow["out"]["paid"], "committed": flow["out"]["committed"] + flow["out"]["pending"],
            "net": flow["net"], "registrations": flow["in"]["registration_count"] + flow["in"]["ticket_count"],
            "budget": _rupees(budget.budget_paise) if budget.budget_paise is not None else None,
            "over_budget": budget.over_budget,
        })
    return rows
