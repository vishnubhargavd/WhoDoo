"""UPI and Razorpay API routes: secure profile management, test checkout, and payout initiation."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db, require_roles
from app.schemas.upi import (
    RazorpayOrderCreate,
    RazorpayOrderOut,
    RazorpayPaymentVerify,
    ReimbursementPayoutRequest,
    UPIProfileOut,
    UPIProfileUpdate,
)
from app.services import razorpay_service, upi_service

router = APIRouter(tags=["UPI & Razorpay"])


@router.get("/upi/profile", response_model=UPIProfileOut)
async def get_my_upi_profile(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """View signed-in user's payment and masked UPI profile."""
    return await upi_service.get_or_create_profile(db, user.id)


@router.put("/upi/profile", response_model=UPIProfileOut)
async def update_my_upi_profile(
    req: UPIProfileUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add or update signed-in user's UPI ID and preferred reimbursement method."""
    return await upi_service.update_payment_profile(db, user.id, req)


@router.delete("/upi/profile", response_model=UPIProfileOut)
async def delete_my_upi_profile(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove user's saved UPI identifier."""
    return await upi_service.remove_upi_profile(db, user.id)


@router.post("/payments/razorpay/order", response_model=RazorpayOrderOut, status_code=201)
async def create_razorpay_checkout_order(
    req: RazorpayOrderCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a user-authorized Razorpay test-mode checkout order."""
    return await razorpay_service.create_order(db, user.club_id, user.id, req)


@router.post("/payments/razorpay/verify")
async def verify_razorpay_payment(
    req: RazorpayPaymentVerify,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify Razorpay payment via server-side HMAC signature verification."""
    order = await razorpay_service.verify_payment(db, user.club_id, user.id, req)
    return {
        "status": "success",
        "order_id": order.rzp_order_id,
        "payment_id": order.rzp_payment_id,
        "amount_paise": order.amount_paise,
    }


@router.post("/expenses/{claim_id}/payout", status_code=201)
async def initiate_claim_payout(
    claim_id: uuid.UUID,
    req: ReimbursementPayoutRequest,
    user: CurrentUser = Depends(require_roles("president", "treasurer")),
    db: AsyncSession = Depends(get_db),
):
    """Initiate reimbursement payout for an approved claim to claimant's verified UPI/bank destination."""
    from app.models.shared import User as UserModel
    officer_user = await db.get(UserModel, user.id)
    payout = await razorpay_service.initiate_reimbursement_payout(
        db, user.club_id, officer_user, claim_id, mode=req.mode, notes=req.notes
    )
    return {
        "payout_id": str(payout.id),
        "status": payout.status,
        "mode": payout.payout_mode,
        "provider": payout.provider,
        "amount_paise": payout.amount_paise,
        "notes": payout.notes,
    }
