"""LegacyVault service: leadership continuity, handover preservation, and historical records."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import not_found
from app.models.legacyvault import LegacyHandoverRecord
from app.models.shared import Event, User
from app.schemas.legacyvault import (
    HandoverRecordCreate,
    HandoverRecordOut,
    HandoverSnapshotOut,
)
from app.services import ledger_service


async def list_records(
    db: AsyncSession,
    club_id: uuid.UUID,
    term_year: str | None = None,
    category: str | None = None,
) -> list[HandoverRecordOut]:
    """Retrieve historical handover records for the authorized club."""
    query = (
        select(
            LegacyHandoverRecord,
            User.full_name.label("author_name"),
            Event.name.label("event_name"),
        )
        .join(User, User.id == LegacyHandoverRecord.author_id)
        .outerjoin(Event, Event.id == LegacyHandoverRecord.event_id)
        .where(LegacyHandoverRecord.club_id == club_id)
        .order_by(LegacyHandoverRecord.term_year.desc(), LegacyHandoverRecord.created_at.desc())
    )
    if term_year:
        query = query.where(LegacyHandoverRecord.term_year == term_year)
    if category:
        query = query.where(LegacyHandoverRecord.category == category)

    rows = (await db.execute(query)).all()
    results = []
    for rec, author_name, event_name in rows:
        results.append(
            HandoverRecordOut(
                id=rec.id,
                club_id=rec.club_id,
                term_year=rec.term_year,
                title=rec.title,
                category=rec.category,
                content=rec.content,
                author_id=rec.author_id,
                author_name=author_name,
                event_id=rec.event_id,
                event_name=event_name,
                metadata_json=rec.metadata_json,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
            )
        )
    return results


async def create_record(
    db: AsyncSession,
    club_id: uuid.UUID,
    author_id: uuid.UUID,
    req: HandoverRecordCreate,
) -> LegacyHandoverRecord:
    """Create a persistent leadership handover record."""
    if req.event_id:
        event = await db.get(Event, req.event_id)
        if not event or event.club_id != club_id:
            raise not_found("Event")

    author = await db.get(User, author_id)
    if not author or author.club_id != club_id:
        raise not_found("Author")

    meta = {
        **req.metadata,
        "author_role": author.role,
        "author_email": author.email,
    }

    rec = LegacyHandoverRecord(
        id=uuid.uuid4(),
        club_id=club_id,
        term_year=req.term_year.strip(),
        title=req.title.strip(),
        category=req.category,
        content=req.content.strip(),
        author_id=author_id,
        event_id=req.event_id,
        metadata_json=meta,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def get_record(
    db: AsyncSession, club_id: uuid.UUID, record_id: uuid.UUID
) -> HandoverRecordOut:
    """Fetch an individual handover record enforcing club isolation."""
    query = (
        select(
            LegacyHandoverRecord,
            User.full_name.label("author_name"),
            Event.name.label("event_name"),
        )
        .join(User, User.id == LegacyHandoverRecord.author_id)
        .outerjoin(Event, Event.id == LegacyHandoverRecord.event_id)
        .where(
            LegacyHandoverRecord.id == record_id,
            LegacyHandoverRecord.club_id == club_id,
        )
    )
    row = (await db.execute(query)).first()
    if not row:
        raise not_found("Handover record")

    rec, author_name, event_name = row
    return HandoverRecordOut(
        id=rec.id,
        club_id=rec.club_id,
        term_year=rec.term_year,
        title=rec.title,
        category=rec.category,
        content=rec.content,
        author_id=rec.author_id,
        author_name=author_name,
        event_id=rec.event_id,
        event_name=event_name,
        metadata_json=rec.metadata_json,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


async def get_continuity_snapshot(
    db: AsyncSession, club_id: uuid.UUID
) -> HandoverSnapshotOut:
    """Aggregate existing data for leadership transition without duplicate databases."""
    # 1. Total handover records
    records_count = await db.scalar(
        select(func.count(LegacyHandoverRecord.id)).where(LegacyHandoverRecord.club_id == club_id)
    ) or 0

    # 2. Total club events
    events_count = await db.scalar(
        select(func.count(Event.id)).where(Event.club_id == club_id)
    ) or 0

    # 3. Active officers count
    officers_count = await db.scalar(
        select(func.count(User.id)).where(
            User.club_id == club_id,
            User.role.in_(("president", "treasurer", "event_lead")),
        )
    ) or 0

    # 4. Financial summary
    net_finance = 0
    try:
        summary = await ledger_service.get_summary(db, club_id)
        net_finance = summary.net_paise
    except Exception:
        net_finance = 0

    return HandoverSnapshotOut(
        total_records=records_count,
        past_events_count=events_count,
        active_officers_count=officers_count,
        net_finance_paise=net_finance,
    )
