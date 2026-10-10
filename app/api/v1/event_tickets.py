"""FairSeat event-side API (Member 2): organizer ticket settings and event discovery."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.tickets import DiscoveryEventOut, TicketConfigIn, TicketConfigOut
from app.services import event_service, ticket_service

router = APIRouter(prefix="/events", tags=["FairSeat · ticket settings"])
discovery = APIRouter(prefix="/discovery", tags=["FairSeat · event discovery"])


@router.get("/{event_id}/tickets", response_model=TicketConfigOut)
async def get_ticket_config(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> TicketConfigOut:
    """Organizers only (president, or this event's lead)."""
    event = await event_service.get_managed_event(db, user, event_id)
    return await ticket_service.config_out(db, event, can_manage=True)


@router.put("/{event_id}/tickets", response_model=TicketConfigOut)
async def put_ticket_config(
    event_id: uuid.UUID,
    body: TicketConfigIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TicketConfigOut:
    """Replace the whole ticket configuration. Organizers only."""
    event = await event_service.get_managed_event(db, user, event_id)
    await ticket_service.save_config(db, user, event, body)
    await db.commit()
    return await ticket_service.config_out(db, event, can_manage=True)


@discovery.get("/events", response_model=list[DiscoveryEventOut])
async def discover_events(
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[DiscoveryEventOut]:
    """Published events of the caller's club with ticket sales on, soonest first."""
    return await ticket_service.discover(db, user)


@discovery.get("/events/{event_id}", response_model=DiscoveryEventOut)
async def discover_event(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> DiscoveryEventOut:
    return await ticket_service.discover_one(db, user, event_id)
