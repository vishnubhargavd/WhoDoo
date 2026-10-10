"""Read-only MemberCore adapter for the proposed 030/031 schema.

No provisioning, role changes, settlement, or activation is performed here.
The caller supplies application users.id and the club from verified backend context.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.services.fairseat_pricing import Membership, calculate_ticket_discount, to_ms


_SCHEMA = text("""
    SELECT to_regclass('public.memberships') IS NOT NULL
       AND to_regclass('public.membership_plans') IS NOT NULL
       AND to_regclass('public.membership_renewals') IS NOT NULL
       AND to_regclass('public.membership_activation_grants') IS NOT NULL
       AND to_regclass('public.membership_dues_reconciliations') IS NOT NULL
""")
_CURRENT = text("""
    SELECT m.id, m.status, m.club_id, m.starts_at, m.expires_at,
           m.discount_bps_snapshot, p.name AS plan_name, c.name AS club_name,
           EXISTS (
             SELECT 1 FROM public.membership_renewals r
             JOIN public.membership_activation_grants g
               ON g.renewal_id = r.id AND g.membership_id = r.membership_id
                  AND g.club_id = r.club_id
             WHERE r.membership_id = m.id AND r.club_id = m.club_id
               AND r.plan_id = m.plan_id AND r.new_starts_at = m.starts_at
               AND r.new_expires_at = m.expires_at
               AND r.discount_bps_snapshot = m.discount_bps_snapshot
               AND (g.source = 'complimentary_waiver' OR (
                 g.source = 'paid_settlement' AND EXISTS (
                   SELECT 1 FROM public.membership_dues_reconciliations paid
                   WHERE paid.id = g.reconciliation_id AND paid.dues_id = g.dues_id
                     AND paid.club_id = g.club_id AND paid.outcome = 'paid'
                 ) AND NOT EXISTS (
                   SELECT 1 FROM public.membership_dues_reconciliations refunded
                   WHERE refunded.dues_id = g.dues_id AND refunded.club_id = g.club_id
                     AND refunded.outcome = 'refunded'
                 )
               ))
           ) AS has_activation_proof
    FROM public.memberships m
    JOIN public.membership_plans p ON p.id = m.plan_id AND p.club_id = m.club_id
    JOIN public.clubs c ON c.id = m.club_id
    WHERE m.user_id = :user_id AND m.club_id = :club_id
""")


def _unavailable(code: str = "membercore_schema_unavailable") -> AppError:
    return AppError(503, code, "Membership data is temporarily unavailable. Please try again later")


def _aware(moment: datetime) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("MemberCore requires a timezone-aware server timestamp")
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class CurrentMembership:
    id: uuid.UUID
    status: str
    club_id: uuid.UUID
    starts_at: datetime
    expires_at: datetime
    discount_bps_snapshot: int
    plan_name: str
    club_name: str

    def pricing_membership(self) -> Membership | None:
        # The tested existing calculator does not accept the persistence-only
        # pending status. It must never be promoted to active for pricing.
        if self.status == "pending":
            return None
        return Membership(
            status=self.status, club_id=str(self.club_id),
            starts_at_ms=to_ms(self.starts_at), expires_at_ms=to_ms(self.expires_at),
            discount_bps=self.discount_bps_snapshot,
        )


async def current_membership(
    session: AsyncSession, *, user_id: uuid.UUID, club_id: uuid.UUID,
) -> CurrentMembership | None:
    if not isinstance(user_id, uuid.UUID) or not isinstance(club_id, uuid.UUID):
        raise ValueError("MemberCore requires trusted application UUIDs")
    try:
        if not await session.scalar(_SCHEMA):
            raise _unavailable()
        row = (await session.execute(_CURRENT, {"user_id": user_id, "club_id": club_id})).mappings().one_or_none()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        code = "membercore_schema_unavailable" if sqlstate in {"42P01", "42703", "3F000"} else "membercore_unavailable"
        raise _unavailable(code) from None
    if row is None:
        return None
    # A damaged active row must fail closed, not grant benefits or quietly claim
    # there is no membership. No compatibility flag can stand in for this proof.
    if row["status"] == "active" and not row["has_activation_proof"]:
        raise _unavailable("membercore_data_invalid")
    if row["status"] not in {"pending", "active", "cancelled", "expired"}:
        raise _unavailable("membercore_data_invalid")
    return CurrentMembership(**{key: row[key] for key in CurrentMembership.__dataclass_fields__})


class PostgresMembershipSource:
    async def membership_for(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        club_id: uuid.UUID,
        now: datetime,
    ) -> Membership | None:
        _aware(now)
        record = await current_membership(session, user_id=user_id, club_id=club_id)
        return record.pricing_membership() if record is not None else None


def membership_view(record: CurrentMembership, *, now: datetime) -> dict:
    # A zero-base quote reuses the exact eligibility rules without inventing a
    # membership price or a second discount formula.
    price = calculate_ticket_discount(
        base_price_paise=0, event_club_id=str(record.club_id),
        membership=record.pricing_membership(), now_ms=to_ms(_aware(now)),
    )
    return {
        "id": record.id, "status": record.status,
        "plan_name": record.plan_name, "club_name": record.club_name,
        "starts_at": record.starts_at, "expires_at": record.expires_at,
        "discount_bps_snapshot": record.discount_bps_snapshot,
        "eligible": price.eligible, "applied_discount_bps": price.applied_discount_bps,
        "eligibility_reason": "pending_membership" if record.status == "pending" else price.reason,
    }
