"""FairSeat ticket booking. Separate from the merchandise payment routes; takes no payment.

Switched off unless FAIRSEAT_ENABLED=1. The purchaser is whoever `get_current_user` says
is signed in; nothing in a request body can name a user, a club or a role. While sign-in
is the demo picker (anyone can act as anyone) this is for local development and tests
only, which `Settings.check` enforces. When the Supabase Auth bridge is approved, only
`get_purchaser` changes.
"""

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import AppError
from app.schemas.fairseat import (
    BookingOut,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    QuoteOut,
    QuoteRequest,
    ReservationRequest,
)
from app.services import fairseat_service


def require_fairseat() -> None:
    if not get_settings().fairseat_enabled:
        raise AppError(404, "fairseat_disabled", "Ticket booking is not switched on")


async def get_purchaser(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """The verified application user making the booking. The single place to swap in
    Supabase-verified identity later; it must keep returning a row from `users`."""
    return user


router = APIRouter(tags=["FairSeat"], dependencies=[Depends(require_fairseat)])


@router.get("/ticket-categories", response_model=list[CategoryOut])
async def list_categories(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> list[CategoryOut]:
    """Ticket categories for an event in your club, with seats taken and seats left."""
    return await fairseat_service.list_categories(db, user, event_id)


@router.post("/ticket-categories", response_model=CategoryOut, status_code=201)
async def create_category(
    body: CategoryCreate, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> CategoryOut:
    """President, or the lead of this event."""
    category = await fairseat_service.create_category(db, user, body)
    await db.commit()
    return category


@router.patch("/ticket-categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: uuid.UUID,
    body: CategoryUpdate,
    user: CurrentUser = Depends(get_purchaser),
    db: AsyncSession = Depends(get_db),
) -> CategoryOut:
    """President, or the lead of this event. Capacity cannot go below the seats already taken."""
    category = await fairseat_service.update_category(db, user, category_id, body)
    await db.commit()
    return category


@router.post("/ticket-bookings/quotes", response_model=QuoteOut, status_code=201)
async def create_quote(
    body: QuoteRequest, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> QuoteOut:
    """The server's price for you, valid for a few minutes. Holds no seats."""
    quote = await fairseat_service.create_quote(db, user, body)
    await db.commit()
    return QuoteOut.model_validate(quote)


@router.post("/ticket-bookings", response_model=BookingOut, status_code=201)
async def reserve(
    body: ReservationRequest,
    response: Response,
    user: CurrentUser = Depends(get_purchaser),
    db: AsyncSession = Depends(get_db),
) -> BookingOut:
    """Hold seats for 10 minutes (free tickets are confirmed at once). Sending the same
    idempotency key again returns the same booking with 200 instead of making another."""
    booking = await fairseat_service.reserve(db, user, body)
    await db.commit()
    if booking.replayed:
        response.status_code = 200
    return booking


@router.get("/ticket-bookings", response_model=list[BookingOut])
async def my_bookings(
    user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> list[BookingOut]:
    return await fairseat_service.list_my_bookings(db, user)


@router.get("/ticket-bookings/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: uuid.UUID, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> BookingOut:
    return await fairseat_service.get_booking(db, user, booking_id)


@router.post("/ticket-bookings/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(
    booking_id: uuid.UUID, user: CurrentUser = Depends(get_purchaser), db: AsyncSession = Depends(get_db)
) -> BookingOut:
    """Give up your own hold or free booking. Its seats go back on sale and its tickets are revoked."""
    booking = await fairseat_service.cancel_booking(db, user, booking_id)
    await db.commit()
    return booking
