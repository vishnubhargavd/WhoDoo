"""Server-rendered review pages. They read through the same services as the API;
every action on a page is a fetch() call to /api/v1, so there is one code path for writes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import (
    CurrentUser,
    get_current_user,
    get_db,
    require_finance_admin,
)
from app.models.ledger import ENTRY_TYPES, EXPENSE_CATEGORIES, PAYMENT_STATUSES, SOURCE_MODULES
from app.schemas.ledger import LedgerFilters
from app.services import ledger_service, shared_directory
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
