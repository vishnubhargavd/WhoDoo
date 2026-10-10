import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_db, require_finance_admin
from app.core.errors import not_found
from app.schemas.ledger import (
    EventProfitLoss,
    LedgerEntryOut,
    LedgerEntryPage,
    LedgerFilters,
    LedgerSummary,
    ManualEntryCreate,
)
from app.services import ledger_service, shared_directory

router = APIRouter(prefix="/ledger", tags=["TrustLedger"])


@router.get("/summary", response_model=LedgerSummary)
async def ledger_summary(
    user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
) -> LedgerSummary:
    return await ledger_service.get_summary(db, user.club_id)


@router.get("/entries", response_model=LedgerEntryPage)
async def ledger_entries(
    filters: Annotated[LedgerFilters, Query()],
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> LedgerEntryPage:
    items, total = await ledger_service.list_entries(db, user.club_id, filters)
    return LedgerEntryPage(items=items, total=total, limit=filters.limit, offset=filters.offset)


@router.get("/events/{event_id}/profit-loss", response_model=EventProfitLoss)
async def event_profit_loss(
    event_id: uuid.UUID,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> EventProfitLoss:
    event = await shared_directory.get_event(db, user.club_id, event_id)
    if event is None:
        raise not_found("Event")
    return await ledger_service.get_event_profit_loss(db, user.club_id, event.id, event.name)


@router.post("/manual-entry", response_model=LedgerEntryOut, status_code=201)
async def create_manual_entry(
    body: ManualEntryCreate,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> LedgerEntryOut:
    if body.event_id and await shared_directory.get_event(db, user.club_id, body.event_id) is None:
        raise not_found("Event")
    entry = await ledger_service.create_manual_entry(db, club_id=user.club_id, actor_id=user.id, data=body)
    await db.commit()
    return entry
