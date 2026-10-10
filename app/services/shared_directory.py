"""The only place the finance module reads tables owned by other modules.

Users, clubs and events are placeholders today (migrations/000_shared_placeholders.sql).
When the member/auth and event modules land, re-point these functions at the real
tables or services; nothing else in the finance code needs to change.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shared import Club, Event, User


async def get_club(session: AsyncSession, club_id: uuid.UUID) -> Club | None:
    return await session.get(Club, club_id)


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


class AuthIdentityUnavailable(Exception):
    """users.auth_user_id cannot be used: migration 030 is missing, or the data breaks its rules.

    The message is for operators' logs, not for API clients.
    """


# Set once the column has been seen; until then every Supabase sign-in re-checks, so applying
# migration 030 takes effect without a restart.
_auth_identity_ready = False

_AUTH_IDENTITY_COLUMN = text(
    """
    SELECT format_type(a.atttypid, a.atttypmod) AS data_type,
           EXISTS (
               SELECT 1 FROM pg_index i
               WHERE i.indrelid = a.attrelid AND i.indisunique AND i.indnkeyatts = 1
                 AND i.indkey[0] = a.attnum
                 -- Migration 030 indexes only the rows that have a value. NULLs never collide
                 -- anyway, so that is the same guarantee as an index over every row.
                 AND (i.indpred IS NULL
                      OR pg_get_expr(i.indpred, i.indrelid) = '(auth_user_id IS NOT NULL)')
           ) AS is_unique
    FROM pg_attribute a
    WHERE a.attrelid = to_regclass('users') AND a.attname = 'auth_user_id' AND NOT a.attisdropped
    """
)


async def check_auth_identity_column(session: AsyncSession) -> None:
    """Raise AuthIdentityUnavailable unless users.auth_user_id exists as a unique uuid column."""
    global _auth_identity_ready
    if _auth_identity_ready:
        return
    row = (await session.execute(_AUTH_IDENTITY_COLUMN)).first()
    if row is None:
        raise AuthIdentityUnavailable(
            "users.auth_user_id does not exist: apply MemberCore migration 030 before AUTH_MODE=supabase"
        )
    if row.data_type != "uuid" or not row.is_unique:
        raise AuthIdentityUnavailable("users.auth_user_id must be a uuid column with a UNIQUE constraint")
    _auth_identity_ready = True


async def get_user_by_auth_user_id(session: AsyncSession, auth_user_id: uuid.UUID) -> User | None:
    """The application user linked to a Supabase Auth user, or None if nobody is linked.

    Matches users.auth_user_id only: never the email address, never users.id.
    """
    global _auth_identity_ready
    await check_auth_identity_column(session)
    try:
        ids = list(
            await session.scalars(
                text("SELECT id FROM users WHERE auth_user_id = :auth_user_id LIMIT 2"),
                {"auth_user_id": auth_user_id},
            )
        )
    except ProgrammingError:
        _auth_identity_ready = False  # the column went away after it was checked
        raise AuthIdentityUnavailable("users.auth_user_id could not be queried") from None
    if len(ids) > 1:
        raise AuthIdentityUnavailable("more than one user is linked to the same auth user")
    return await session.get(User, ids[0]) if ids else None


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
