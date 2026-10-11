"""The event budget (Member 2, Batch A).

The budget is stored here (event_budgets); every amount spent against it is read from
TrustLedger by event_id. Pending claims are shown separately and never counted as spent.
Going over budget is a warning only: no financial action is blocked by it.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import FINANCE_ADMINS, CurrentUser
from app.core.errors import AppError, forbidden, not_found
from app.models.expense import ExpenseClaim
from app.models.ledger import LedgerEntry
from app.models.member2 import EventBudget
from app.models.shared import Event
from app.schemas.member2 import BudgetIn, BudgetOut
from app.services import audit_service, event_service, modules


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _can_see_money(user: CurrentUser, event: Event) -> bool:
    return event_service.can_manage(user, event) or (user.club_id == event.club_id and user.role in FINANCE_ADMINS)


async def _event_for_budget(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> Event:
    event = await db.get(Event, event_id)
    if event is None or event.club_id != user.club_id:
        raise not_found("Event")
    if not _can_see_money(user, event):
        if not event_service.can_view(user, event):
            raise not_found("Event")  # a draft stays invisible
        raise forbidden("Only the president, this event's lead or the treasurer can see its budget")
    return event


def _paise(amount: Decimal | None) -> int:
    return int((Decimal(amount or 0) * 100).to_integral_value())


async def budget(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> BudgetOut:
    event = await _event_for_budget(db, user, event_id)
    E = LedgerEntry
    spending = E.entry_type.in_(("expense", "reimbursement"))
    row = (await db.execute(
        select(
            func.coalesce(func.sum(E.amount).filter(spending, E.payment_status == "paid"), 0).label("spent"),
            func.coalesce(func.sum(E.amount).filter(spending, E.payment_status == "approved"), 0).label("committed"),
        ).where(E.club_id == event.club_id, E.event_id == event.id)
    )).one()
    pending = await db.scalar(
        select(func.coalesce(func.sum(ExpenseClaim.amount), 0)).where(
            ExpenseClaim.club_id == event.club_id, ExpenseClaim.event_id == event.id,
            ExpenseClaim.status.in_(("pending_approval", "change_requested")),
        )
    )
    out = BudgetOut(event_id=event.id, spent_paise=_paise(row.spent), committed_paise=_paise(row.committed),
                    pending_paise=_paise(pending))
    if not modules.migrated("budgets"):
        out.available = False
        return out
    stored = await db.get(EventBudget, event.id)
    if stored is None:
        return out
    out.budget_paise = stored.budget_paise
    out.remaining_paise = stored.budget_paise - out.spent_paise - out.committed_paise
    out.over_budget = out.remaining_paise < 0
    if out.over_budget:
        out.warning = "Spending is over this event's budget."
    elif out.remaining_paise - out.pending_paise < 0:
        out.warning = "Claims waiting for approval would take this event over budget."
    elif stored.budget_paise and out.remaining_paise <= stored.budget_paise // 10:
        out.warning = "Less than 10% of the budget is left."
    return out


async def set_budget(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID, data: BudgetIn) -> BudgetOut:
    if not modules.migrated("budgets"):
        raise AppError(404, "module_unavailable", "Event budgets are not switched on in this deployment")
    event = await _event_for_budget(db, user, event_id)
    if event.status == "cancelled":
        raise AppError(409, "event_cancelled", "A cancelled event's budget cannot be changed")
    stored = await db.get(EventBudget, event.id, with_for_update=True)
    before = stored.budget_paise if stored else None
    if stored is None:
        stored = EventBudget(event_id=event.id, club_id=event.club_id, budget_paise=data.budget_paise, updated_by=user.id)
        db.add(stored)
    else:
        stored.budget_paise, stored.updated_by, stored.updated_at = data.budget_paise, user.id, utcnow()
    await db.flush()
    audit_service.record(
        db, club_id=event.club_id, actor_id=user.id, action="event.budget_set", entity_type="event",
        entity_id=event.id, details={"from_paise": before, "to_paise": data.budget_paise},
    )
    return await budget(db, user, event_id)
