"""Pages anyone can open without signing in: upcoming events, registering, and a participant's ticket.

A ticket page is reached only through the private link given at registration (and emailed)."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_db, get_optional_user
from app.core.errors import AppError
from app.services import payment_service, registration_service
from app.web.templating import render

router = APIRouter(include_in_schema=False)


async def public_events_page(request: Request, user: CurrentUser | None, db: AsyncSession):
    await registration_service.purge_if_due(db)
    return render(request, "public/events.html", user, events=await registration_service.public_events(db))


@router.get("/explore")
async def explore(request: Request, user: CurrentUser | None = Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Upcoming events of every club. The same page a signed-out visitor gets at /events."""
    return await public_events_page(request, user, db)


@router.get("/e/{event_id}")
async def public_event(
    event_id: uuid.UUID, request: Request, user: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    return render(request, "public/event.html", user, event=await registration_service.public_event(db, event_id))


@router.get("/ticket/{token}")
async def ticket(
    token: str, request: Request, background: BackgroundTasks, session_id: str = "",
    user: CurrentUser | None = Depends(get_optional_user), db: AsyncSession = Depends(get_db),
):
    """A participant's ticket. Coming back from the payment page with a session id, the payment
    is checked with the provider first."""
    registration, event = await registration_service.by_token(db, token)
    if session_id and registration.status == "pending_payment":
        try:
            registration_id, outcome = await registration_service.verify_payment(db, token, session_id)
            await db.commit()
            if outcome == "registered":
                background.add_task(registration_service.send_ticket_email, registration_id)
            elif outcome == "refund_required":
                await registration_service.process_refund(db, registration_id)
                await db.commit()
        except AppError:
            await db.rollback()
        registration, event = await registration_service.by_token(db, token)
    return render(
        request, "public/ticket.html", user, r=registration, event=event, token=token,
        qr_svg=registration_service.qr_for(registration, event),
        expired_hold=registration.status == "pending_payment"
        and registration.hold_expires_at <= registration_service.utcnow(),
    )
