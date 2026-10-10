"""How many seats are taken. The one definition, used by the event side (ticket settings,
lifecycle guards, discovery) and by the reservation service alike.

    seats in use = confirmed seats + seats on a hold that has not run out

Read in the caller's transaction, so under the ticket locks it is exact.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fairseat import TicketBooking, TicketCategory

B = TicketBooking


def in_use(now: datetime):
    """Bookings that take up seats at `now`. A hold stops counting the instant it runs out."""
    return or_(B.status == "confirmed", and_(B.status == "pending_payment", B.hold_expires_at > now))


async def occupied_seats(db: AsyncSession, category_ids: Iterable[uuid.UUID], now: datetime) -> dict[uuid.UUID, int]:
    """Seats in use per category id. Every id asked for is in the answer, with 0 if none."""
    ids = list(category_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(B.ticket_category_id, func.coalesce(func.sum(B.quantity), 0))
        .where(B.ticket_category_id.in_(ids), in_use(now))
        .group_by(B.ticket_category_id)
    )
    counts = dict.fromkeys(ids, 0)
    counts.update({category_id: int(total) for category_id, total in rows})
    return counts


async def committed_seats(db: AsyncSession, event_id: uuid.UUID) -> dict[uuid.UUID, int] | None:
    """Seats in use per ticket category of `event_id`: confirmed plus unexpired holds."""
    ids = list(await db.scalars(select(TicketCategory.id).where(TicketCategory.event_id == event_id)))
    return await occupied_seats(db, ids, datetime.now(timezone.utc))


async def paid_bookings(db: AsyncSession, event_id: uuid.UUID) -> int:
    """Confirmed bookings of the event that have been paid for. They need refunds to undo."""
    total = await db.scalar(
        select(func.count()).select_from(B)
        .where(B.event_id == event_id, B.status == "confirmed", B.payment_status == "paid")
    )
    return int(total)
