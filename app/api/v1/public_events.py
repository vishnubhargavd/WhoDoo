"""Public event registration. No sign-in: any student can see published events and register.

What a participant may do afterwards (pay, resend the email) is authorised by the private
ticket link they were given, which is the `token` in these paths. Organiser routes at the
bottom need sign-in as usual.
"""

import time
import uuid
from collections import defaultdict, deque

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db, get_optional_user
from app.core.errors import AppError, conflict
from app.schemas.registration import (
    PublicEventOut,
    RegistrationCheckoutOut,
    RegistrationIn,
    RegistrationMock,
    RegistrationOut,
    VenueOut,
)
from app.services import email_service, event_service, registration_service, venue_service

router = APIRouter(prefix="/public", tags=["Public events"])
organiser = APIRouter(prefix="/events", tags=["Public events"])

WINDOW_SECONDS = 600
_recent: dict[str, deque[float]] = defaultdict(deque)


def limit_registrations(request: Request) -> None:
    """A soft brake on one address filling an event with made-up people. Per process, in memory."""
    limit = get_settings().registration_rate_limit
    if limit <= 0:
        return
    now, address = time.monotonic(), request.client.host if request.client else "unknown"
    seen = _recent[address]
    while seen and now - seen[0] > WINDOW_SECONDS:
        seen.popleft()
    if len(seen) >= limit:
        raise AppError(429, "too_many_registrations", "Too many registrations from here. Please try again in a few minutes.")
    seen.append(now)


class SessionIn(BaseModel):
    session_id: str = Field(min_length=5, max_length=200)


class PaymentOut(BaseModel):
    outcome: str
    registration: RegistrationOut


@router.get("/events", response_model=list[PublicEventOut])
async def list_events(db: AsyncSession = Depends(get_db)) -> list[PublicEventOut]:
    """Published events of every club that have not ended, with places left."""
    return await registration_service.public_events(db)


@router.get("/events/{event_id}", response_model=PublicEventOut)
async def get_event(event_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> PublicEventOut:
    return await registration_service.public_event(db, event_id)


@router.get("/venues", response_model=list[VenueOut])
async def list_venues(fits: int | None = None, db: AsyncSession = Depends(get_db)) -> list[VenueOut]:
    """College venues, smallest first. `fits=N` keeps those that hold at least N people."""
    return [VenueOut.model_validate(venue) for venue in await venue_service.list_venues(db, fits)]


@router.post("/events/{event_id}/register", response_model=RegistrationOut, status_code=201,
             dependencies=[Depends(limit_registrations)])
async def register(
    event_id: uuid.UUID, body: RegistrationIn, background: BackgroundTasks,
    user: CurrentUser | None = Depends(get_optional_user), db: AsyncSession = Depends(get_db),
) -> RegistrationOut:
    """Take a place. 409 `sold_out` when every place is taken, `already_registered` for a repeat.
    A free event confirms at once and emails the ticket; a paid one holds the place for payment.
    No sign-in is needed, and the price is the same for everyone."""
    registration, event = await registration_service.register(db, event_id, body, user=user)
    await db.commit()
    if registration.status == "registered":
        background.add_task(registration_service.send_ticket_email, registration.id)
    return registration_service.present(registration, event)


@router.post("/registrations/{token}/checkout", response_model=RegistrationCheckoutOut)
async def checkout(token: str, db: AsyncSession = Depends(get_db)) -> RegistrationCheckoutOut:
    """The payment page for a held place. The amount is the event's price."""
    session = await registration_service.create_checkout(db, token)
    await db.commit()
    return session


async def _settled(
    db: AsyncSession, background: BackgroundTasks, token: str, registration_id: uuid.UUID, outcome: str
) -> PaymentOut:
    await db.commit()
    if outcome == "registered":
        background.add_task(registration_service.send_ticket_email, registration_id)
    elif outcome == "refund_required":  # the place could not be given: send the money back
        await registration_service.process_refund(db, registration_id)
        await db.commit()
    registration, event = await registration_service.by_token(db, token)
    return PaymentOut(outcome=outcome, registration=registration_service.present(registration, event))


@router.post("/registrations/{token}/verify", response_model=PaymentOut)
async def verify(token: str, body: SessionIn, background: BackgroundTasks, db: AsyncSession = Depends(get_db)) -> PaymentOut:
    """Back from the payment page: the provider is asked what was paid."""
    registration_id, outcome = await registration_service.verify_payment(db, token, body.session_id)
    return await _settled(db, background, token, registration_id, outcome)


@router.post("/registrations/{token}/mock", response_model=PaymentOut)
async def mock_payment(
    token: str, body: RegistrationMock, background: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> PaymentOut:
    """Mock provider only: pay, or be declined, without leaving the site."""
    registration_id, outcome = await registration_service.simulate_mock_payment(db, token, body.outcome == "success")
    return await _settled(db, background, token, registration_id, outcome)


@router.post("/registrations/{token}/resend")
async def resend(token: str, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Send the ticket email again, to the address given at registration only. Waits for the mail
    server, so the answer says what actually happened."""
    registration, _ = await registration_service.by_token(db, token)
    if registration.status != "registered":
        raise conflict("no_ticket", "There is no valid ticket to send for this registration.")
    if not email_service.configured():
        raise conflict("email_not_configured", "Email sending is not set up on this server, so nothing was sent. "
                                               "Your ticket is this page: keep the link or print it.")
    outcome = await registration_service.send_ticket_email(registration.id, db)
    if outcome != "sent":
        raise AppError(502, "email_failed", "The email could not be sent right now. Your ticket is this page: "
                                            "keep the link or print it, and try again later.")
    return {"status": "sent", "status_label": f"sent to {registration.email}", "title": "Ticket email"}


@router.post("/registrations/webhook")
async def webhook(
    request: Request, background: BackgroundTasks, stripe_signature: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Signed provider events. The signature is the proof."""
    registration_id, outcome = await registration_service.handle_webhook(db, await request.body(), stripe_signature)
    await db.commit()
    if outcome == "registered":
        background.add_task(registration_service.send_ticket_email, registration_id)
    elif outcome == "refund_required":
        await registration_service.process_refund(db, registration_id)
        await db.commit()
    return {"status": outcome}


# --- Organisers -------------------------------------------------------------------------


@organiser.get("/{event_id}/registrations")
async def list_registrations(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Who has registered. The president or this event's lead."""
    event = await event_service.get_visible_event(db, user, event_id)
    found = await registration_service.list_for_event(db, user, event)
    return {
        "max_participants": found["limit"], "places_taken": found["taken"], "checked_in": found["checked_in"],
        "registrations": [
            {"id": r.id, "participant_no": r.participant_no, "participant_code": r.participant_code,
             "full_name": r.full_name, "email": r.email, "phone": r.phone, "student_id": r.student_id,
             "status": r.status, "amount_paise": r.amount_paise, "refund_status": r.refund_status,
             "checked_in_at": r.checked_in_at, "created_at": r.created_at}
            for r in found["registrations"]
        ],
    }


@organiser.post("/registrations/{registration_id}/cancel")
async def cancel_registration(
    registration_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Remove a participant and free the place. A paid registration is refunded."""
    registration = await registration_service.cancel(db, user, registration_id)
    await db.commit()
    refund = None
    if registration.refund_status == "required":
        refund = await registration_service.process_refund(db, registration.id)
        await db.commit()
    return {"status": "cancelled", "refund": refund}
