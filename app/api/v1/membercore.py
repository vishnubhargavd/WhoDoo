"""Authenticated read-only application profile and current membership."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import AppError
from app.services.membercore import current_membership, membership_view


def require_membercore() -> None:
    if not get_settings().membercore_enabled:
        raise AppError(404, "membercore_disabled", "Membership access is not switched on")


class ApplicationIdentityOut(BaseModel):
    application_user_id: uuid.UUID
    club_id: uuid.UUID
    role: str
    full_name: str
    email: str


class MembershipOut(BaseModel):
    id: uuid.UUID
    status: Literal["pending", "active", "cancelled", "expired"]
    plan_name: str
    club_name: str
    starts_at: datetime
    expires_at: datetime
    discount_bps_snapshot: int
    eligible: bool
    applied_discount_bps: int
    eligibility_reason: str


class MemberCoreMeOut(BaseModel):
    status: Literal["mapped"] = "mapped"
    identity: ApplicationIdentityOut
    membership: MembershipOut | None


router = APIRouter(prefix="/membercore", tags=["MemberCore"], dependencies=[Depends(require_membercore)])


@router.get("/me", response_model=MemberCoreMeOut)
async def me(
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberCoreMeOut:
    record = await current_membership(db, user_id=user.id, club_id=user.club_id)
    response.headers["Cache-Control"] = "private, no-store"
    return MemberCoreMeOut(
        identity=ApplicationIdentityOut(
            application_user_id=user.id, club_id=user.club_id, role=user.role,
            full_name=user.full_name, email=user.email,
        ),
        membership=MembershipOut(**membership_view(record, now=datetime.now(timezone.utc))) if record else None,
    )
