"""MemberCore: plans, enrollment, dues. Answers 404 until its tables exist (services/modules.py).

The member is always whoever is signed in. Officer routes name the person in the path and
check the caller's role from the database.
"""

import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.membercore import (
    EnrollIn,
    MembershipCheckoutOut,
    MembershipOut,
    OfflinePaymentIn,
    PaymentResult,
    PlanIn,
    PlanOut,
    PlanUpdate,
    ReasonIn,
    SessionIn,
)
from app.services import membership_service, modules

router = APIRouter(prefix="/memberships", tags=["MemberCore"], dependencies=[Depends(modules.require("memberships"))])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    include_hidden: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[PlanOut]:
    return await membership_service.list_plans(db, user, include_hidden)


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(
    body: PlanIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PlanOut:
    """Treasurer or president."""
    plan = await membership_service.create_plan(db, user, body)
    await db.commit()
    return plan


@router.put("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID, body: PlanUpdate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PlanOut:
    """Treasurer or president. Terms already bought keep the discount they were bought with."""
    plan = await membership_service.update_plan(db, user, plan_id, body)
    await db.commit()
    return plan


@router.get("/me", response_model=MembershipOut)
async def my_membership(
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MembershipOut:
    await membership_service.expire_lapsed(db, user.club_id)
    await db.commit()
    return await membership_service.my_membership(db, user)


@router.post("/enroll", response_model=MembershipOut)
async def enroll(
    body: EnrollIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MembershipOut:
    """Join or renew on a plan. Nothing is active until the dues are paid or waived."""
    membership = await membership_service.enroll(db, user, body.plan_id)
    await db.commit()
    return membership


@router.post("/checkout", response_model=MembershipCheckoutOut)
async def create_checkout_session(
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MembershipCheckoutOut:
    """A checkout session for what you owe. The amount comes from your dues, never the request."""
    checkout = await membership_service.create_checkout_session(db, user)
    await db.commit()
    return checkout


async def _result(db: AsyncSession, user: CurrentUser, session_id: str, outcome: str) -> PaymentResult:
    await db.commit()
    if outcome == "refund_due":
        await membership_service.refund_unhonoured(db, session_id)
        await db.commit()
    return PaymentResult(outcome=outcome, membership=await membership_service.my_membership(db, user))


@router.post("/checkout/verify", response_model=PaymentResult)
async def verify_payment(
    body: SessionIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaymentResult:
    return await _result(db, user, body.session_id, await membership_service.verify_payment(db, user, body.session_id))


@router.post("/checkout/mock", response_model=PaymentResult)
async def simulate_mock_payment(
    body: SessionIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaymentResult:
    """Mock provider only: pay, or be declined, without leaving the app."""
    outcome = await membership_service.simulate_mock_payment(db, user, body.session_id, body.outcome == "success")
    return await _result(db, user, body.session_id, outcome)


@router.post("/webhook")
async def webhook(
    request: Request, stripe_signature: str = Header(default=""), db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Signed provider events. No sign-in: the signature is the proof."""
    outcome = await membership_service.handle_webhook(db, await request.body(), stripe_signature)
    await db.commit()
    return {"status": outcome}


@router.get("/members", response_model=list[MembershipOut])
async def list_members(
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[MembershipOut]:
    """Treasurer or president."""
    await membership_service.expire_lapsed(db, user.club_id)
    await db.commit()
    return await membership_service.list_members(db, user)


@router.post("/members/{user_id}/waive", response_model=MembershipOut)
async def waive(
    user_id: uuid.UUID, body: ReasonIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MembershipOut:
    """Treasurer or president: activate without payment, with a recorded reason."""
    membership = await membership_service.waive(db, user, user_id, body.reason)
    await db.commit()
    return membership


@router.post("/members/{user_id}/offline-payment", response_model=MembershipOut)
async def record_offline_payment(
    user_id: uuid.UUID, body: OfflinePaymentIn, user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MembershipOut:
    """Treasurer or president: dues received in person. Goes into the ledger as income."""
    membership = await membership_service.record_offline_payment(db, user, user_id, body.reference)
    await db.commit()
    return membership


@router.post("/members/{user_id}/cancel", response_model=MembershipOut)
async def cancel(
    user_id: uuid.UUID, body: ReasonIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MembershipOut:
    membership = await membership_service.cancel(db, user, user_id, body.reason)
    await db.commit()
    return membership
