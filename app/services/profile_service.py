"""Editable member profiles and the UPI payment profile.

The person is always whoever is signed in: there is no route that edits someone else's
profile. Only the fields in `ProfileUpdate` can change; role, email (the sign-in identity),
membership status and expiry are owned elsewhere and the schema refuses them.

UPI ids are encrypted at rest (Fernet, key derived from PAYMENT_PROFILE_KEY), shown masked,
and never written to logs or audit details. A UPI id that matches the format is only that:
`unverified` until the payout provider confirms the account, and nothing debits a person
because their UPI id is on file.
"""

import base64
import hashlib
import re
import uuid
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, bad_request, not_found
from app.models.member2 import UserPaymentProfile, UserProfile
from app.models.shared import User
from app.schemas.member2 import PaymentProfileOut, ProfileOut, ProfileUpdate
from app.services import audit_service, modules

# name@handle: the name part is letters, digits, dot, hyphen, underscore; the handle is a bank's.
UPI_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,255}@[a-z][a-z0-9.-]{1,63}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- UPI ids ----------------------------------------------------------------------------


def normalise_upi(raw: str) -> str:
    value = raw.strip().lower()
    if not UPI_PATTERN.match(value):
        raise bad_request("invalid_upi_id", "Enter a UPI id like name@bank (letters, digits, dot, hyphen or underscore).")
    return value


def mask_upi(vpa: str) -> str:
    name, handle = vpa.split("@", 1)
    return f"{name[:2]}{'*' * max(len(name) - 2, 3)}@{handle}"


def _fernet() -> Fernet:
    secret = get_settings().profile_secret
    if secret is None:
        raise AppError(503, "payment_profile_key_missing", "UPI ids cannot be stored until PAYMENT_PROFILE_KEY is set")
    digest = hashlib.sha256(b"whodoo-upi-v1:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_upi(vpa: str) -> str:
    return _fernet().encrypt(vpa.encode()).decode()


def decrypt_upi(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise AppError(500, "payment_profile_unreadable", "This UPI id can no longer be read. Ask the person to enter it again.") from None


def _payment_out(row: UserPaymentProfile | None) -> PaymentProfileOut | None:
    if row is None:
        return None
    return PaymentProfileOut(
        upi_masked=row.upi_masked, verification_status=row.verification_status,
        verification_provider=row.verification_provider, verified_by_mock=row.verification_provider == "mock",
        registered_name=row.registered_name, verified_at=row.verified_at, updated_at=row.updated_at,
    )


async def payment_profile_for(session: AsyncSession, user_id: uuid.UUID, club_id: uuid.UUID) -> UserPaymentProfile | None:
    return await session.scalar(
        select(UserPaymentProfile).where(UserPaymentProfile.user_id == user_id, UserPaymentProfile.club_id == club_id)
    )


async def set_upi(session: AsyncSession, user: CurrentUser, raw: str) -> PaymentProfileOut:
    """Add or replace the caller's UPI id. A change always starts unverified, then asks the payout
    provider (when one is configured) to confirm the account."""
    from app.services import payout_service  # payout_service reads profiles too

    vpa = normalise_upi(raw)
    row = await session.scalar(
        select(UserPaymentProfile).where(UserPaymentProfile.user_id == user.id).with_for_update()
    )
    changed = row is None or decrypt_upi(row.upi_ciphertext) != vpa
    if row is None:
        row = UserPaymentProfile(user_id=user.id, club_id=user.club_id, upi_ciphertext="", upi_masked="")
        session.add(row)
    if changed:
        row.upi_ciphertext, row.upi_masked = encrypt_upi(vpa), mask_upi(vpa)
        row.verification_status, row.verification_provider, row.verification_reference = "unverified", None, None
        row.registered_name, row.verified_at = None, None
        row.updated_at = utcnow()
        await session.flush()

    provider = payout_service.get_payout_provider()
    if provider is not None and row.verification_status != "verified":
        check = await run_in_threadpool(provider.validate_vpa, vpa, user.full_name, f"user-{user.id.hex[:20]}")
        row.verification_status = check.status
        row.verification_reference = check.reference
        row.registered_name = check.registered_name
        if check.status == "verified":
            row.verification_provider, row.verified_at = provider.name, utcnow()
        else:
            row.verification_provider, row.verified_at = None, None
        await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="profile.upi_saved", entity_type="user",
        entity_id=user.id, details={"upi": row.upi_masked, "verification": row.verification_status, "changed": changed},
    )
    return _payment_out(row)


async def remove_upi(session: AsyncSession, user: CurrentUser) -> None:
    from app.services import payout_service

    row = await session.scalar(select(UserPaymentProfile).where(UserPaymentProfile.user_id == user.id).with_for_update())
    if row is None:
        raise not_found("UPI id")
    if await payout_service.has_payout_in_flight(session, user.id):
        raise AppError(409, "payout_in_progress", "A reimbursement is being paid to this UPI id. Try again once it settles.")
    await session.delete(row)
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="profile.upi_removed", entity_type="user",
        entity_id=user.id, details={"upi": row.upi_masked},
    )


# --- Profile ----------------------------------------------------------------------------


async def get_profile(session: AsyncSession, user: CurrentUser) -> ProfileOut:
    person = await session.get(User, user.id)
    out = ProfileOut(user_id=person.id, full_name=person.full_name, email=person.email, role=person.role)
    if not modules.migrated("profile"):
        out.profile_available = False
        return out
    profile = await session.get(UserProfile, user.id)
    if profile is not None:
        out.display_name, out.phone = profile.display_name, profile.phone
        out.department, out.academic_year = profile.department, profile.academic_year
    out.payment = _payment_out(await payment_profile_for(session, user.id, user.club_id))
    return out


async def update_profile(session: AsyncSession, user: CurrentUser, data: ProfileUpdate) -> ProfileOut:
    changes = data.model_dump(exclude_unset=True)
    person = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if "full_name" in changes and changes["full_name"]:
        person.full_name = changes.pop("full_name")
    else:
        changes.pop("full_name", None)
    if changes and not modules.migrated("profile"):
        raise AppError(404, "module_unavailable", "Profile details are not switched on in this deployment")
    if changes:
        profile = await session.get(UserProfile, user.id, with_for_update=True)
        if profile is None:
            profile = UserProfile(user_id=user.id, club_id=user.club_id)
            session.add(profile)
        for name, value in changes.items():
            setattr(profile, name, value)
        profile.updated_at = utcnow()
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="profile.updated", entity_type="user",
        entity_id=user.id, details={"fields": sorted(data.model_dump(exclude_unset=True))},
    )
    return await get_profile(session, user)
