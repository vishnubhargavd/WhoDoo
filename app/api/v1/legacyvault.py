"""LegacyVault API: leadership handover, continuity archives, and historical club knowledge."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_db, require_roles
from app.schemas.legacyvault import (
    HandoverRecordCreate,
    HandoverRecordOut,
    HandoverSnapshotOut,
)
from app.services import legacyvault_service

router = APIRouter(
    prefix="/legacyvault",
    tags=["LegacyVault"],
    dependencies=[Depends(require_roles("president", "treasurer", "event_lead"))],
)


@router.get("/snapshot", response_model=HandoverSnapshotOut)
async def get_continuity_snapshot(
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate high-level continuity metrics from existing event and ledger data."""
    return await legacyvault_service.get_continuity_snapshot(db, user.club_id)


@router.get("/records", response_model=list[HandoverRecordOut])
async def list_handover_records(
    term_year: str | None = Query(None, description="Filter by term year, e.g. 2025-2026"),
    category: str | None = Query(None, description="Filter by category"),
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve preserved handover notes, event debriefs, and governance docs."""
    return await legacyvault_service.list_records(db, user.club_id, term_year=term_year, category=category)


@router.post("/records", response_model=HandoverRecordOut, status_code=201)
async def create_handover_record(
    req: HandoverRecordCreate,
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Preserve a new handover note or debrief for incoming leadership."""
    rec = await legacyvault_service.create_record(db, user.club_id, user.id, req)
    return await legacyvault_service.get_record(db, user.club_id, rec.id)


@router.get("/records/{record_id}", response_model=HandoverRecordOut)
async def get_handover_record(
    record_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Access an individual historical handover record within the authorized club."""
    return await legacyvault_service.get_record(db, user.club_id, record_id)
