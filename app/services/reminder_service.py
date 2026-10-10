"""MemberCore renewal reminders, shown in the app (home page, Memberships, Notifications).

When reminders are written. `generate` looks at every active or recently expired membership
and writes the one reminder its expiry date has reached: the closest configured stage
(RENEWAL_REMINDER_DAYS, default 30, 14, 7 and 1 days before), or `expired`. Only the current
stage is written, so someone who joins with five days left gets "7 days", not three reminders
at once. The unique key (membership, term, stage) makes it safe to run as often as wanted,
from the in-process job (app/main.py), from members opening their pages, or from cron with
`python -m scripts.send_renewal_reminders`.

When they stop. A reminder belongs to the term it was written for. Renewing moves the
membership's expiry, so the old term's reminders no longer match and are not shown; nothing is
deleted. Cancelled and never-activated (pending) memberships get no reminders, and theirs are
hidden. Whether a renewal is paid always comes from MemberCore, never from a reminder.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import not_found
from app.models.member2 import MembershipRenewalReminder as R
from app.models.membercore import Membership, MembershipPlan
from app.models.shared import Club
from app.schemas.member2 import ReminderOut
from app.services import modules

# An "expired" reminder is written only for terms that ended this recently, so turning the
# feature on does not message everyone who left years ago.
EXPIRED_LOOKBACK = timedelta(days=60)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stage_for(expires_at: datetime, now: datetime, offsets: tuple[int, ...]) -> str | None:
    """The most urgent stage this expiry has reached, or None if none has been reached yet."""
    if expires_at <= now:
        return "expired" if now - expires_at <= EXPIRED_LOOKBACK else None
    left = expires_at - now
    reached = [d for d in offsets if left <= timedelta(days=d)]
    return f"d{min(reached)}" if reached else None


async def generate(session: AsyncSession, *, club_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None,
                   now: datetime | None = None) -> int:
    """Write the reminders that are due. Returns how many were new. Idempotent."""
    now = now or utcnow()
    offsets = get_settings().renewal_reminder_offsets
    horizon = now + timedelta(days=max(offsets, default=0))
    stmt = select(Membership).where(
        Membership.status.in_(("active", "expired")),
        Membership.expires_at <= horizon,
        Membership.expires_at >= now - EXPIRED_LOOKBACK,
    )
    if club_id is not None:
        stmt = stmt.where(Membership.club_id == club_id)
    if user_id is not None:
        stmt = stmt.where(Membership.user_id == user_id)
    rows = []
    for membership in await session.scalars(stmt):
        stage = stage_for(membership.expires_at, now, offsets)
        if stage:
            rows.append(dict(id=uuid.uuid4(), club_id=membership.club_id, membership_id=membership.id,
                             user_id=membership.user_id, plan_id=membership.plan_id, stage=stage,
                             term_expires_at=membership.expires_at))
    if not rows:
        return 0
    result = await session.execute(
        insert(R).values(rows)
        .on_conflict_do_nothing(index_elements=["membership_id", "term_expires_at", "stage"])
        .returning(R.id)
    )
    return len(result.all())


def _wording(stage: str, plan: str | None, club: str | None, expires_at: datetime) -> tuple[str, str]:
    name = f"{plan} membership" if plan else "membership"
    of = f" of {club}" if club else ""
    day = expires_at.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y")
    if stage == "expired":
        return "Your membership has expired", f"Your {name}{of} expired on {day}. Renew to get member benefits back."
    days = int(stage[1:])
    when = "tomorrow" if days == 1 else f"in {days} days"
    return f"Membership expires {when}", f"Your {name}{of} expires on {day}. Renew now to keep your benefits."


async def list_for(session: AsyncSession, user: CurrentUser, now: datetime | None = None,
                   unread_only: bool = False) -> list[ReminderOut]:
    """The caller's reminders for their current term. Writes any that are due first."""
    if not (modules.migrated("reminders") and modules.migrated("memberships")):
        return []
    await generate(session, user_id=user.id, now=now)
    stmt = (
        select(R, MembershipPlan.name, Club.name)
        .join(Membership, and_(Membership.id == R.membership_id, Membership.expires_at == R.term_expires_at))
        .join(MembershipPlan, MembershipPlan.id == R.plan_id)
        .join(Club, Club.id == R.club_id)
        .where(R.user_id == user.id, R.club_id == user.club_id, Membership.status.in_(("active", "expired")))
        .order_by(R.created_at.desc())
    )
    out = []
    for reminder, plan, club in (await session.execute(stmt)).all():
        title, message = _wording(reminder.stage, plan, club, reminder.term_expires_at)
        out.append(ReminderOut(
            id=reminder.id, stage=reminder.stage, title=title, message=message, plan_name=plan, club_name=club,
            expires_at=reminder.term_expires_at, created_at=reminder.created_at, read=reminder.read_at is not None,
        ))
    # Newest term first, and within a term the most urgent stage first. Only that one is live;
    # earlier stages of the same term are shown as read.
    out.sort(key=lambda r: (-r.expires_at.timestamp(), -1 if r.stage == "expired" else int(r.stage[1:])))
    seen: set[datetime] = set()
    for item in out:
        if item.expires_at in seen:
            item.read = True
        seen.add(item.expires_at)
    return [r for r in out if not (unread_only and r.read)]


async def mark_read(session: AsyncSession, user: CurrentUser, reminder_id: uuid.UUID) -> None:
    result = await session.execute(
        update(R).where(R.id == reminder_id, R.user_id == user.id, R.read_at.is_(None)).values(read_at=utcnow())
        .returning(R.id)
    )
    if result.scalar() is None and await session.scalar(
        select(R.id).where(R.id == reminder_id, R.user_id == user.id)
    ) is None:
        raise not_found("Reminder")


async def run_all(session: AsyncSession, now: datetime | None = None) -> int:
    """The scheduled job: every club. Checks for the tables itself (no request has run yet)."""
    present = await session.scalar(text(
        "SELECT to_regclass('membership_renewal_reminders') IS NOT NULL AND to_regclass('memberships') IS NOT NULL"
    ))
    if not present:
        return 0
    return await generate(session, now=now)
