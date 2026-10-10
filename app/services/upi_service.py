"""UPI Profile Service: secure user payment details, masking, and reimbursement destinations."""

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import bad_request, forbidden, not_found
from app.models.payment_profile import UserPaymentProfile
from app.schemas.upi import UPIProfileOut, UPIProfileUpdate, UPI_REGEX


def mask_upi_id(upi_id: str | None) -> str | None:
    """Mask sensitive UPI VPA while retaining recognizable handle and bank domain."""
    if not upi_id or "@" not in upi_id:
        return None
    user_part, handle = upi_id.split("@", 1)
    if len(user_part) <= 2:
        masked_user = user_part[0] + "*"
    elif len(user_part) <= 4:
        masked_user = user_part[:2] + "**"
    else:
        masked_user = user_part[:2] + "*" * (len(user_part) - 3) + user_part[-1]
    return f"{masked_user}@{handle}"


def mask_bank_account(account: str | None) -> str | None:
    if not account:
        return None
    clean = re.sub(r"\s+", "", account)
    if len(clean) <= 4:
        return "****"
    return "*" * (len(clean) - 4) + clean[-4:]


async def get_or_create_profile(db: AsyncSession, user_id: uuid.UUID) -> UPIProfileOut:
    """Retrieve payment profile enforcing server-side user ownership."""
    profile = await db.get(UserPaymentProfile, user_id)
    if not profile:
        profile = UserPaymentProfile(
            user_id=user_id,
            upi_id=None,
            preferred_payout_method="upi",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)

    is_syntax_valid = bool(profile.upi_id and UPI_REGEX.match(profile.upi_id))
    # Syntax validity does not imply verified bank account ownership
    verif_status = "unverified" if is_syntax_valid else "not_configured"

    return UPIProfileOut(
        user_id=profile.user_id,
        upi_id_masked=mask_upi_id(profile.upi_id),
        preferred_payout_method=profile.preferred_payout_method,
        is_syntax_valid=is_syntax_valid,
        verification_status=verif_status,
        bank_account_masked=profile.bank_account_number_masked,
        bank_ifsc=profile.bank_ifsc,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


async def update_payment_profile(
    db: AsyncSession, user_id: uuid.UUID, req: UPIProfileUpdate
) -> UPIProfileOut:
    """Update authenticated user's UPI details and preferred reimbursement mode."""
    profile = await db.get(UserPaymentProfile, user_id)
    if not profile:
        profile = UserPaymentProfile(
            user_id=user_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(profile)

    if req.upi_id is not None:
        clean_upi = req.upi_id.strip().lower() if req.upi_id else None
        if clean_upi and not UPI_REGEX.match(clean_upi):
            raise bad_request("invalid_upi_syntax", "UPI ID format is invalid. Use username@handle.")
        profile.upi_id = clean_upi

    profile.preferred_payout_method = req.preferred_payout_method

    if req.bank_account_number:
        profile.bank_account_number_masked = mask_bank_account(req.bank_account_number)
    if req.bank_ifsc:
        profile.bank_ifsc = req.bank_ifsc.strip().upper()

    profile.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(profile)

    is_syntax_valid = bool(profile.upi_id and UPI_REGEX.match(profile.upi_id))
    verif_status = "unverified" if is_syntax_valid else "not_configured"

    return UPIProfileOut(
        user_id=profile.user_id,
        upi_id_masked=mask_upi_id(profile.upi_id),
        preferred_payout_method=profile.preferred_payout_method,
        is_syntax_valid=is_syntax_valid,
        verification_status=verif_status,
        bank_account_masked=profile.bank_account_number_masked,
        bank_ifsc=profile.bank_ifsc,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


async def remove_upi_profile(db: AsyncSession, user_id: uuid.UUID) -> UPIProfileOut:
    """Remove user's saved UPI identifier."""
    profile = await db.get(UserPaymentProfile, user_id)
    if profile:
        profile.upi_id = None
        profile.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(profile)

    return await get_or_create_profile(db, user_id)
