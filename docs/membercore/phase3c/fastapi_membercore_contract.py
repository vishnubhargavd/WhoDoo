"""Member 3 integration contract; interface only, not backend implementation.

Import the existing FairSeat result type; do not fork its pricing contract.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.fairseat_pricing import Membership


class MembershipSource(Protocol):
    async def membership_for(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        club_id: uuid.UUID,
        now: datetime,
    ) -> Membership | None:
        """Read the trusted current entitlement for this app user and club.

        Return `None` when no record exists. The returned Membership is the
        existing FairSeat type with status: str, club_id: str, starts_at_ms:
        int, expires_at_ms: int, and discount_bps: int. A pending record must
        remain pending/ineligible; never map it to active or trust browser data.
        """
        ...


@dataclass(frozen=True, slots=True)
class VerifiedSupabaseSubject:
    """Construct only after signature and JWT claims have been verified."""

    auth_user_id: uuid.UUID  # verified JWT sub == auth.users.id


@dataclass(frozen=True, slots=True)
class ApplicationIdentity:
    """Identity resolved from the existing public.users row."""

    application_user_id: uuid.UUID  # public.users.id used by WhoDoo modules
    club_id: uuid.UUID
    role: str  # fresh public.users.role, validated against the approved role set


class ApplicationIdentityResolver(Protocol):
    async def resolve(
        self,
        session: AsyncSession,
        *,
        subject: VerifiedSupabaseSubject,
    ) -> ApplicationIdentity | None:
        """Map auth_user_id to public.users.id; never match by email."""
        ...
