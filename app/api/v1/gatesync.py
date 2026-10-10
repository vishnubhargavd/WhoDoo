import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import AppError
from app.services import gatesync_service


def require_gatesync():
    settings = get_settings()
    if not settings.gatesync_enabled or not settings.fairseat_enabled:
        raise AppError(404, "gatesync_disabled", "Online check-in is not switched on")


router = APIRouter(prefix="/gatesync", tags=["GateSync"], dependencies=[Depends(require_gatesync)])


class RedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: uuid.UUID
    credential: str = Field(min_length=1, max_length=256)


class StaffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID


@router.get("/access")
async def access(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await gatesync_service.access(db, user)


@router.get("/tickets")
async def tickets(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await gatesync_service.tickets(db, user)


@router.post("/tickets/{ticket_id}/credential")
async def credential(ticket_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    result = await gatesync_service.issue(db, user, ticket_id)
    await db.commit()
    return result


@router.post("/redeem")
async def redeem(body: RedeemRequest, user: CurrentUser = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    result = await gatesync_service.redeem(db, user, body.event_id, body.credential)
    await db.commit()
    return result


@router.post("/events/{event_id}/staff", status_code=201)
async def assign_staff(event_id: uuid.UUID, body: StaffRequest, user: CurrentUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    await gatesync_service.assign_staff(db, user, event_id, body.user_id)
    await db.commit()
    return {"event_id": event_id, "user_id": body.user_id, "assigned": True}


@router.get("/tickets/{ticket_id}/pdf")
async def ticket_pdf(ticket_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    from fastapi.responses import Response
    from app.services import ticket_pdf_service

    result = await gatesync_service.issue(db, user, ticket_id)
    await db.commit()
    ticket, booking, event = await gatesync_service._lock_ticket(db, ticket_id)
    time_str = event.starts_at.strftime("%a %d %b %Y, %H:%M IST") if event.starts_at else "TBA"
    is_free = (booking.payment_status == "not_required" or booking.total_price_paise == 0)
    pdf_bytes = ticket_pdf_service.generate_ticket_pdf(
        event_name=event.name,
        venue=event.venue,
        time_str=time_str,
        holder_name=user.full_name,
        seat_number=ticket.seat_number,
        ticket_id=str(ticket.id),
        credential=result["credential"],
        is_free=is_free,
    )
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="whodoo-ticket-{ticket_id}.pdf"'},
    )


@router.post("/tickets/{ticket_id}/email")
async def email_ticket(ticket_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    from app.services import email_service

    result = await gatesync_service.issue(db, user, ticket_id)
    await db.commit()
    ticket, booking, event = await gatesync_service._lock_ticket(db, ticket_id)
    time_str = event.starts_at.strftime("%a %d %b %Y, %H:%M IST") if event.starts_at else "TBA"
    email_result = email_service.send_ticket_email(
        to_email=user.email,
        recipient_name=user.full_name,
        event_name=event.name,
        venue=event.venue,
        starts_at_str=time_str,
        credential=result["credential"],
        ticket_id=str(ticket.id),
    )
    return email_result.to_dict()

