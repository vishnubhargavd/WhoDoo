"""Server-rendered review pages. They read through the same services as the API;
every action on a page is a fetch() call to /api/v1, so there is one code path for writes."""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import (
    CurrentUser,
    get_current_user,
    get_db,
    require_approver,
    require_finance_admin,
)
from app.models.ledger import ENTRY_TYPES, EXPENSE_CATEGORIES, PAYMENT_STATUSES, SOURCE_MODULES
from app.schemas.ledger import LedgerFilters
from app.services import expense_service, ledger_service, shared_directory
from app.web.templating import render

router = APIRouter(include_in_schema=False)


@router.get("/")
async def home(user: CurrentUser = Depends(get_current_user)):
    return RedirectResponse("/treasurer" if user.is_finance_admin else "/expenses", status_code=303)


@router.get("/treasurer")
async def treasurer_dashboard(
    request: Request, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
):
    summary = await ledger_service.get_summary(db, user.club_id)
    events = await shared_directory.list_events(db, user.club_id)
    event_results = [
        await ledger_service.get_event_profit_loss(db, user.club_id, event.id, event.name) for event in events
    ]
    recent, _ = await ledger_service.list_entries(db, user.club_id, LedgerFilters(limit=8))
    peak = max([max(point.income, point.expenses) for point in summary.monthly_trend] or [0]) or 1
    return render(
        request,
        "treasurer_dashboard.html",
        user,
        summary=summary,
        event_results=event_results,
        recent=recent,
        trend_peak=peak,
    )


@router.get("/treasurer/ledger")
async def ledger_page(
    request: Request, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
):
    try:
        filters = LedgerFilters.model_validate(dict(request.query_params))
        filter_error = None
    except ValidationError as exc:
        filters = LedgerFilters()
        filter_error = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in exc.errors())
    entries, total = await ledger_service.list_entries(db, user.club_id, filters)
    events = await shared_directory.list_events(db, user.club_id)
    return render(
        request,
        "ledger.html",
        user,
        entries=entries,
        total=total,
        filters=filters,
        filter_error=filter_error,
        events=events,
        event_names={event.id: event.name for event in events},
        entry_types=ENTRY_TYPES,
        source_modules=SOURCE_MODULES,
        payment_statuses=PAYMENT_STATUSES,
        categories=EXPENSE_CATEGORIES,
    )


# --- Snap-to-Reimburse ---------------------------------------------------------


@router.get("/expenses")
async def my_expenses(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    claims = await expense_service.list_my_claims(db, user)
    return render(request, "expenses.html", user, claims=claims)


@router.get("/expenses/new")
async def new_expense(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    events = await shared_directory.list_events(db, user.club_id)
    return render(
        request,
        "expense_new.html",
        user,
        events=events,
        categories=EXPENSE_CATEGORIES,
        approval_limit=get_settings().expense_auto_approval_limit,
    )


@router.get("/expenses/approvals")
async def expense_approvals(
    request: Request, user: CurrentUser = Depends(require_approver), db: AsyncSession = Depends(get_db)
):
    queue = await expense_service.get_queue(db, user)
    return render(request, "expense_approvals.html", user, queue=queue)


@router.get("/expenses/{expense_id}")
async def expense_detail(
    expense_id: uuid.UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    claim = await expense_service.get_claim_detail(db, user, expense_id)
    events = await shared_directory.list_events(db, user.club_id)
    return render(
        request,
        "expense_detail.html",
        user,
        claim=claim,
        events=events,
        categories=EXPENSE_CATEGORIES,
        approval_limit=get_settings().expense_auto_approval_limit,
    )
