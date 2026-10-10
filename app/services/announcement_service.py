"""WhoDoo Connect: announcements and notification dispatch for student organizations."""

import uuid
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser
from app.core.errors import bad_request, forbidden, not_found
from app.models.announcement import ClubAnnouncement
from app.schemas.announcement import AnnouncementCreate, AnnouncementOut


async def ensure_announcements_table(db: AsyncSession) -> None:
    """Idempotently ensure table exists."""
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS public.club_announcements (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            club_id         uuid NOT NULL,
            author_id       uuid NOT NULL,
            author_name     text NOT NULL,
            title           text NOT NULL,
            content         text NOT NULL,
            target_audience text NOT NULL DEFAULT 'all',
            is_pinned       boolean NOT NULL DEFAULT false,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        );
    """))


async def list_announcements(
    db: AsyncSession, club_id: uuid.UUID, audience: str | None = None
) -> list[ClubAnnouncement]:
    await ensure_announcements_table(db)
    query = select(ClubAnnouncement).where(ClubAnnouncement.club_id == club_id)
    if audience and audience != "all":
        query = query.where(ClubAnnouncement.target_audience.in_(("all", audience)))
    query = query.order_by(ClubAnnouncement.is_pinned.desc(), ClubAnnouncement.created_at.desc())
    return list((await db.scalars(query)).all())


async def get_announcement(
    db: AsyncSession, club_id: uuid.UUID, announcement_id: uuid.UUID
) -> ClubAnnouncement:
    await ensure_announcements_table(db)
    item = await db.get(ClubAnnouncement, announcement_id)
    if not item or item.club_id != club_id:
        raise not_found("Announcement")
    return item


async def create_announcement(
    db: AsyncSession, user: CurrentUser, data: AnnouncementCreate
) -> ClubAnnouncement:
    # Any officer, lead, president, treasurer, or admin can publish announcements
    if user.role not in ("president", "treasurer", "event_lead", "admin"):
        raise forbidden("Only club leaders and organizers can publish announcements")

    await ensure_announcements_table(db)
    announcement = ClubAnnouncement(
        club_id=user.club_id,
        author_id=user.id,
        author_name=user.email.split("@")[0].replace(".", " ").title(),
        title=data.title.strip(),
        content=data.content.strip(),
        target_audience=data.target_audience,
        is_pinned=data.is_pinned,
    )
    db.add(announcement)
    await db.flush()
    return announcement


async def delete_announcement(
    db: AsyncSession, user: CurrentUser, announcement_id: uuid.UUID
) -> None:
    item = await get_announcement(db, user.club_id, announcement_id)
    if user.role not in ("president", "admin") and item.author_id != user.id:
        raise forbidden("You cannot delete this announcement")
    await db.delete(item)
    await db.flush()
