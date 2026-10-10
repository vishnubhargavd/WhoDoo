"""The only place the finance module reads tables owned by other modules.

Users, clubs and events are placeholders today (migrations/000_shared_placeholders.sql).
When the member/auth and event modules land, re-point these functions at the real
tables or services; nothing else in the finance code needs to change.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shared import Club, Event, User


async def get_club(session: AsyncSession, club_id: uuid.UUID) -> Club | None:
    return await session.get(Club, club_id)


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_users(session: AsyncSession, user_ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(ids)))
    return {user.id: user for user in rows}


async def list_users(session: AsyncSession, club_id: uuid.UUID | None = None) -> list[User]:
    stmt = select(User).order_by(User.role, User.full_name)
    if club_id:
        stmt = stmt.where(User.club_id == club_id)
    return list(await session.scalars(stmt))


async def first_user_with_role(
    session: AsyncSession, club_id: uuid.UUID, role: str, *, exclude: uuid.UUID | None = None
) -> User | None:
    stmt = select(User).where(User.club_id == club_id, User.role == role).order_by(User.created_at, User.id)
    if exclude:
        stmt = stmt.where(User.id != exclude)
    return await session.scalar(stmt.limit(1))


async def get_event(session: AsyncSession, club_id: uuid.UUID, event_id: uuid.UUID) -> Event | None:
    event = await session.get(Event, event_id)
    return event if event and event.club_id == club_id else None


async def list_events(session: AsyncSession, club_id: uuid.UUID) -> list[Event]:
    stmt = select(Event).where(Event.club_id == club_id).order_by(Event.event_date.desc().nulls_last(), Event.name)
    return list(await session.scalars(stmt))
