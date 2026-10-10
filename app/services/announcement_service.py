"""CrewDoo Connect: announcements that reach the people they are for.

Who sees what is decided here from the database, per reader:

    everyone     the whole club
    members      people with member standing
    volunteers   volunteers, event leads, the treasurer and the president
    officers     event leads, the treasurer and the president
    event        whoever leads the event, has a task on it or holds a ticket for it

Officers can read everything in their club, so nothing they post is invisible to each other.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import APPROVERS, ROLE_PRESIDENT, ROLE_VOLUNTEER, CurrentUser
from app.core.errors import forbidden, not_found
from app.models.community import Announcement, AnnouncementRead
from app.models.crewpulse import CrewTask
from app.models.fairseat import TicketBooking
from app.models.shared import Event, User
from app.schemas.community import AnnouncementIn, AnnouncementOut
from app.services import audit_service, modules

OFFICERS = APPROVERS


def can_post(user: CurrentUser) -> bool:
    return user.role in OFFICERS


async def _my_events(session: AsyncSession, user: CurrentUser) -> set[uuid.UUID]:
    """Events this person is part of: leading, working on, or attending."""
    events = set(await session.scalars(select(Event.id).where(Event.club_id == user.club_id, Event.lead_id == user.id)))
    events |= set(await session.scalars(select(CrewTask.event_id).where(CrewTask.assignee_id == user.id)))
    if modules.migrated("tickets"):
        events |= set(await session.scalars(
            select(TicketBooking.event_id)
            .where(TicketBooking.purchaser_user_id == user.id, TicketBooking.status == "confirmed")
        ))
    return events


async def _visible(session: AsyncSession, user: CurrentUser):
    """The condition picking out announcements this user may read."""
    A = Announcement
    if user.role in OFFICERS:
        return A.club_id == user.club_id
    audiences = ["everyone"]
    if user.is_active_member:
        audiences.append("members")
    if user.role == ROLE_VOLUNTEER:
        audiences.append("volunteers")
    mine = await _my_events(session, user)
    reach = [A.audience.in_(audiences), A.author_id == user.id]
    if mine:
        reach.append(A.event_id.in_(mine))
    return (A.club_id == user.club_id) & or_(*reach)


async def feed(session: AsyncSession, user: CurrentUser) -> list[AnnouncementOut]:
    """Pinned first, then newest."""
    A = Announcement
    rows = (await session.execute(
        select(A, User.full_name, Event.name, AnnouncementRead.read_at)
        .join(User, User.id == A.author_id)
        .outerjoin(Event, Event.id == A.event_id)
        .outerjoin(AnnouncementRead, (AnnouncementRead.announcement_id == A.id) & (AnnouncementRead.user_id == user.id))
        .where(await _visible(session, user), A.removed_at.is_(None))
        .order_by(A.pinned.desc(), A.created_at.desc())
        .limit(200)
    )).all()
    counts = {}
    if rows and user.role in OFFICERS:
        counts = dict((await session.execute(
            select(AnnouncementRead.announcement_id, func.count())
            .where(AnnouncementRead.announcement_id.in_([row[0].id for row in rows]))
            .group_by(AnnouncementRead.announcement_id)
        )).all())
    return [
        AnnouncementOut(
            id=a.id, title=a.title, body=a.body, audience=a.audience, event_id=a.event_id, event_name=event_name,
            pinned=a.pinned, author_name=author, created_at=a.created_at,
            read=read_at is not None or a.author_id == user.id,
            can_remove=a.author_id == user.id or user.role == ROLE_PRESIDENT,
            read_count=counts.get(a.id, 0) if user.role in OFFICERS else None,
        )
        for a, author, event_name, read_at in rows
    ]


async def unread_count(session: AsyncSession, user: CurrentUser) -> int:
    return sum(not item.read for item in await feed(session, user))


async def post(session: AsyncSession, user: CurrentUser, data: AnnouncementIn) -> uuid.UUID:
    if not can_post(user):
        raise forbidden("Only event leads, the treasurer and the president can post announcements")
    if data.event_id is not None:
        event = await session.get(Event, data.event_id)
        if event is None or event.club_id != user.club_id:
            raise not_found("Event")
    announcement = Announcement(id=uuid.uuid4(), club_id=user.club_id, author_id=user.id, **data.model_dump())
    session.add(announcement)
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="connect.announcement_posted",
        entity_type="announcement", entity_id=announcement.id, details={"audience": data.audience, "title": data.title},
    )
    return announcement.id


async def mark_read(session: AsyncSession, user: CurrentUser, announcement_id: uuid.UUID | None = None) -> int:
    """Mark one announcement, or everything the user can see, as read. Returns how many were new."""
    visible = select(Announcement.id).where(await _visible(session, user), Announcement.removed_at.is_(None))
    if announcement_id is not None:
        visible = visible.where(Announcement.id == announcement_id)
    ids = list(await session.scalars(visible))
    if announcement_id is not None and not ids:
        raise not_found("Announcement")
    if not ids:
        return 0
    result = await session.execute(
        insert(AnnouncementRead)
        .values([{"announcement_id": found, "user_id": user.id} for found in ids])
        .on_conflict_do_nothing()
    )
    return result.rowcount or 0


async def remove(session: AsyncSession, user: CurrentUser, announcement_id: uuid.UUID) -> None:
    """Take an announcement down. It stays on record with who removed it."""
    announcement = await session.get(Announcement, announcement_id)
    if announcement is None or announcement.club_id != user.club_id or announcement.removed_at is not None:
        raise not_found("Announcement")
    if announcement.author_id != user.id and user.role != ROLE_PRESIDENT:
        raise forbidden("Only the author or the president can remove an announcement")
    announcement.removed_at, announcement.removed_by = datetime.now(timezone.utc), user.id
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="connect.announcement_removed",
        entity_type="announcement", entity_id=announcement.id, details=None,
    )
