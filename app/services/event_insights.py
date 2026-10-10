"""Event registrations, low-ticket warnings and the event budget (Member 2, Batch A).

Registrations. FairSeat bookings are the one source: a "registration" is a booking, and seats
are counted exactly as FairSeat counts them for capacity (ticket_inventory.in_use: confirmed
seats plus holds that have not run out). Nothing here counts tickets a second way. Booking
itself, and refusing over-capacity requests under concurrency, stay in fairseat_service.reserve,
which re-counts under the event and category locks.

Low tickets. A category or event is "low" once what is left is at or under the larger of
LOW_TICKET_PERCENT of its capacity and LOW_TICKET_SEATS (defaults 10% and 5): whichever of the
two thresholds is reached first. "sold_out" at zero.

Budget. The budget is stored here (event_budgets); every amount spent against it is read from
TrustLedger by event_id. Pending claims are shown separately and never counted as spent.
Going over budget is a warning only: no financial action is blocked by it.
"""

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import FINANCE_ADMINS, CurrentUser
from app.core.errors import AppError, forbidden, not_found
from app.models.expense import ExpenseClaim
from app.models.fairseat import TicketBooking
from app.models.ledger import LedgerEntry
from app.models.member2 import EventBudget
from app.models.shared import Event
from app.schemas.member2 import BudgetIn, BudgetOut, CategoryAvailability, RegistrationSummary
from app.services import audit_service, event_service, modules, ticket_inventory, ticket_rules, ticket_service

B = TicketBooking


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def low_threshold(capacity: int) -> int:
    settings = get_settings()
    return max(math.ceil(capacity * settings.low_ticket_percent / 100), settings.low_ticket_seats)


def level(remaining: int, capacity: int) -> tuple[str, str | None]:
    if remaining <= 0:
        return "sold_out", "Sold out"
    if remaining <= low_threshold(capacity):
        return "low", f"Only {remaining} {'ticket' if remaining == 1 else 'tickets'} left"
    return "available", None


def status_label(sale_state: str, lvl: str) -> str:
    if sale_state == "open":
        return "SOLD OUT" if lvl == "sold_out" else "Registration open"
    return {
        "not_open": "Registration not open yet", "closed": "REGISTRATION CLOSED", "off": "Registration paused",
        "unpublished": "Not published", "not_configured": "No registration set up",
    }.get(sale_state, sale_state)


# --- Registrations -------------------------------------------------------------------------


async def registrations(db: AsyncSession, event: Event, now: datetime | None = None) -> RegistrationSummary:
    now = now or utcnow()
    if not ticket_service.enabled() or not modules.migrated("tickets"):
        return RegistrationSummary(event_id=event.id, configured=False, status_label="Ticketing is not switched on")
    settings = await ticket_service.get_settings_row(db, event.id)
    if settings is None:
        return RegistrationSummary(event_id=event.id, configured=False, status_label=status_label("not_configured", ""))
    categories = await ticket_service._categories(db, [event.id])

    live_hold = (B.status == "pending_payment") & (B.hold_expires_at > now)
    rows = (await db.execute(
        select(
            B.ticket_category_id,
            func.count().filter(ticket_inventory.in_use(now)).label("registrations"),
            func.count().filter(B.status == "confirmed").label("confirmed"),
            func.coalesce(func.sum(B.quantity).filter(B.status == "confirmed"), 0).label("confirmed_seats"),
            func.coalesce(func.sum(B.quantity).filter(live_hold), 0).label("held_seats"),
        ).where(B.event_id == event.id).group_by(B.ticket_category_id)
    )).all()
    by_category = {r.ticket_category_id: r for r in rows}

    out_categories = []
    for c in categories:
        r = by_category.get(c.id)
        confirmed, holds = (int(r.confirmed_seats), int(r.held_seats)) if r else (0, 0)
        remaining = max(c.capacity - confirmed - holds, 0)
        lvl, message = level(remaining, c.capacity)
        out_categories.append(CategoryAvailability(
            id=c.id, name=c.name, capacity=c.capacity, confirmed=confirmed, held=holds, remaining=remaining,
            level=lvl, message=message,
        ))
    confirmed_seats = sum(c.confirmed for c in out_categories)
    held_seats = sum(c.held for c in out_categories)
    # The event total bounds every category (strict partition, so normally they agree).
    remaining = max(settings.capacity - confirmed_seats - held_seats, 0)
    lvl, message = level(remaining, settings.capacity)
    state = ticket_rules.sale_state(settings.sales_enabled, settings.opens_at, settings.closes_at,
                                    ticket_service._times(event), now)
    return RegistrationSummary(
        event_id=event.id, configured=True, sale_state=state, status_label=status_label(state, lvl),
        capacity=settings.capacity,
        total_registrations=sum(int(r.registrations) for r in rows),
        confirmed_registrations=sum(int(r.confirmed) for r in rows),
        confirmed_seats=confirmed_seats, held_seats=held_seats, remaining=remaining, level=lvl, message=message,
        categories=out_categories,
    )


# --- Budget --------------------------------------------------------------------------------


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
