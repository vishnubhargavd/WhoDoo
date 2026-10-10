"""The one place Event Management asks the reservation service (Member 3) how many seats
are taken. Nothing here counts bookings itself or guesses.

Contract for Member 3: return, per ticket category id of `event_id`, the seats that are
confirmed plus held by unexpired holds, read in the caller's transaction. Categories
without bookings may be left out. Return None while bookings are not persisted.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession


async def committed_seats(db: AsyncSession, event_id: uuid.UUID) -> dict[uuid.UUID, int] | None:
    """Seats held or sold per category, or None when the reservation service is not connected."""
    return None
