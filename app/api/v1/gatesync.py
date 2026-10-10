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
