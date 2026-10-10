"""College venues, and the rules tying an event's registration to the venue it is held in."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import bad_request, not_found
from app.models.registration import Venue
from app.models.shared import Event


async def list_venues(db: AsyncSession, fits: int | None = None) -> list[Venue]:
    """Venues in use, smallest first. `fits` keeps only those that hold at least that many people."""
    stmt = select(Venue).where(Venue.is_active)
    if fits:
        stmt = stmt.where(Venue.capacity >= fits)
    return list(await db.scalars(stmt.order_by(Venue.capacity, Venue.name)))


async def get_venue(db: AsyncSession, venue_id: uuid.UUID) -> Venue:
    venue = await db.get(Venue, venue_id)
    if venue is None or not venue.is_active:
        raise not_found("Venue")
    return venue


async def registration_problems(db: AsyncSession, event: Event) -> list[str]:
    """What stops this event from taking public registrations as it is set up. Empty when fine."""
    if event.registration_mode == "none":
        return []
    problems = []
    if not event.max_participants:
        problems.append("the number of participants")
    if event.registration_mode == "paid" and not event.price_paise:
        problems.append("a ticket price")
    if event.venue_id is None:
        problems.append("a venue from the list")
    elif event.max_participants:
        venue = await db.get(Venue, event.venue_id)
        if venue is None or venue.capacity < event.max_participants:
            problems.append(f"a venue that holds {event.max_participants} people"
                            + (f" ({venue.name} holds {venue.capacity})" if venue else ""))
    return problems


async def check_registration_setup(db: AsyncSession, event: Event) -> None:
    """Refuse a setup that could not be published: used whenever a published event is saved."""
    problems = await registration_problems(db, event)
    if problems:
        raise bad_request("registration_incomplete", "Public registration needs: " + ", ".join(problems), problems)
