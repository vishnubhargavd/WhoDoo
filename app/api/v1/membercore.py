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
from app.schemas.member_profile import MemberProfileOut, MemberProfileUpdate
from app.services.membercore import current_membership, membership_view
from app.services.profile_service import (
    dismiss_renewal_reminder,
    generate_renewal_reminders_for_club,
    get_member_profile,
    update_member_profile,
)


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


@router.get("/profile", response_model=MemberProfileOut)
async def get_profile(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberProfileOut:
    """Retrieve full editable profile with UPI and active renewal reminders."""
    return await get_member_profile(db, user.id)


@router.put("/profile", response_model=MemberProfileOut)
async def update_profile(
    update_data: MemberProfileUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberProfileOut:
    """Update permitted member profile fields. Rejects unauthorized fields like roles and status."""
    return await update_member_profile(db, user.id, update_data)


@router.post("/reminders/generate")
async def trigger_renewal_reminders(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Generate in-app renewal reminders for the caller's club at 30, 14, 7, 1 days and on expiry."""
    created = await generate_renewal_reminders_for_club(db, user.club_id)
    return {"created_count": created}


@router.post("/reminders/{reminder_id}/dismiss")
async def dismiss_reminder(
    reminder_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Dismiss an in-app renewal reminder."""
    await dismiss_renewal_reminder(db, reminder_id, user.id)
    return {"status": "dismissed"}

