"""The one lock order for everything that touches ticket seats.

    event row  ->  event_ticket_settings row  ->  ticket_categories rows (id order)  ->  booking rows

Booking, cancelling, expiring holds, saving ticket settings and changing an event's
status or times all take their locks through here, in that order, so no two of them
can wait on each other in a circle. Locks last until the transaction ends.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fairseat import TicketBooking, TicketCategory, TicketWaitlistEntry
from app.models.shared import Event
from app.models.tickets import EventTicketSettings


async def lock_event(db: AsyncSession, event_id: uuid.UUID) -> Event | None:
    stmt = select(Event).where(Event.id == event_id).with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def lock_settings(db: AsyncSession, event_id: uuid.UUID) -> EventTicketSettings | None:
    """Call after `lock_event`."""
    stmt = (
        select(EventTicketSettings)
        .where(EventTicketSettings.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await db.scalar(stmt)


async def lock_categories(db: AsyncSession, category_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, TicketCategory]:
    """Lock category rows in id order. Call after `lock_settings`."""
    ids = set(category_ids)
    if not ids:
        return {}
    stmt = (
        select(TicketCategory)
        .where(TicketCategory.id.in_(ids))
        .order_by(TicketCategory.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {category.id: category for category in await db.scalars(stmt)}


async def lock_event_categories(db: AsyncSession, event_id: uuid.UUID) -> dict[uuid.UUID, TicketCategory]:
    """Every category of the event, locked in id order. Call after `lock_settings`."""
    stmt = (
        select(TicketCategory)
        .where(TicketCategory.event_id == event_id)
        .order_by(TicketCategory.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {category.id: category for category in await db.scalars(stmt)}


async def lock_booking(db: AsyncSession, booking_id: uuid.UUID) -> TicketBooking | None:
    """Call last, after the event, settings and category locks."""
    stmt = (
        select(TicketBooking).where(TicketBooking.id == booking_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    return await db.scalar(stmt)


async def lock_waitlist_entry(db: AsyncSession, entry_id: uuid.UUID) -> TicketWaitlistEntry | None:
    """Lock waitlist entry under event/category locks."""
    stmt = (
        select(TicketWaitlistEntry).where(TicketWaitlistEntry.id == entry_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    return await db.scalar(stmt)



async def lock_for_event(
    db: AsyncSession, event_id: uuid.UUID, category_ids: Iterable[uuid.UUID] | None = None
) -> tuple[Event | None, EventTicketSettings | None, dict[uuid.UUID, TicketCategory]]:
    """Event, settings and categories, locked in order. `category_ids=None` locks all of the event's."""
    event = await lock_event(db, event_id)
    if event is None:
        return None, None, {}
    settings = await lock_settings(db, event_id)
    if category_ids is None:
        categories = await lock_event_categories(db, event_id)
    else:
        categories = await lock_categories(db, category_ids)
    return event, settings, categories
