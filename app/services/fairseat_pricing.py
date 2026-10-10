"""Ticket prices for FairSeat.

`calculate_ticket_discount` is a line-for-line port of MemberCore's TypeScript
calculator (web/src/modules/membercore/ticket-discount.ts). The two are held together
by shared test vectors: `make fairseat-vectors` runs the TypeScript original over a
grid of inputs, and tests/test_fairseat_pricing.py requires this port to give the
same answer for every one. Change either side and that test fails.

Everything is whole paise and integer arithmetic. Nothing here trusts the browser.

Where memberships come from is a separate question (`MembershipSource`). The backend
has no membership records yet, only the `users.is_active_member` flag, which carries
no discount rate or validity dates. Until an approved source exists the default source
reports no membership, so every purchaser is quoted the base price.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.fairseat import TicketCategory, TicketQuote
from app.models.shared import Event

# Bump when the rules below change. A quote made under another version is refused.
CALCULATOR_VERSION = "membercore-ticket-discount/1"

MAX_SAFE_INTEGER = 2**53 - 1  # the TypeScript side's Number.MAX_SAFE_INTEGER
MEMBERSHIP_STATUSES = ("active", "cancelled", "expired")

EXPLANATIONS = {
    "missing_membership": "No membership was provided.",
    "inactive_membership": "Membership status is not active.",
    "different_club": "Membership belongs to a different club.",
    "not_started": "Membership validity has not started.",
    "expired": "Membership validity has ended.",
    "zero_discount": "Membership is eligible but provides a zero discount.",
    "discount_applied": "Active membership for this club is valid; its discount was applied.",
}


@dataclass(frozen=True)
class Membership:
    """A membership record from a trusted server source. Times are Unix milliseconds."""

    status: str
    club_id: str
    starts_at_ms: int
    expires_at_ms: int
    discount_bps: int


@dataclass(frozen=True)
class DiscountResult:
    eligible: bool
    discount_applied: bool
    reason: str
    explanation: str
    applied_discount_bps: int
    price_paise: int
    savings_paise: int


def _integer(value: object, name: str, maximum: int) -> None:
    if type(value) is not int or value < 0 or value > maximum:
        raise ValueError(f"{name} must be a nonnegative integer <= {maximum}")


def _club(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty string without surrounding whitespace")


def calculate_ticket_discount(
    *, base_price_paise: int, event_club_id: str, membership: Membership | None, now_ms: int
) -> DiscountResult:
    """Validity: starts_at <= now < expires_at. Rounding: nearest paise, ties upward."""
    _integer(base_price_paise, "base_price_paise", MAX_SAFE_INTEGER)
    _club(event_club_id, "event_club_id")
    if type(now_ms) is not int or abs(now_ms) > 8_640_000_000_000_000:
        raise ValueError("now_ms must be an integer representing a valid timestamp")

    reason, eligible, applied_bps = "missing_membership", False, 0
    if membership is not None:
        if membership.status not in MEMBERSHIP_STATUSES:
            raise ValueError("Invalid membership status")
        _club(membership.club_id, "membership.club_id")
        _integer(membership.discount_bps, "discount_bps", 10000)
        if membership.expires_at_ms <= membership.starts_at_ms:
            raise ValueError("expires_at must be later than starts_at")
        if membership.status != "active":
            reason = "inactive_membership"
        elif membership.club_id != event_club_id:
            reason = "different_club"
        elif now_ms < membership.starts_at_ms:
            reason = "not_started"
        elif now_ms >= membership.expires_at_ms:
            reason = "expired"
        else:
            eligible, applied_bps = True, membership.discount_bps
            reason = "zero_discount" if applied_bps == 0 else "discount_applied"

    price = (base_price_paise * (10000 - applied_bps) + 5000) // 10000
    return DiscountResult(
        eligible=eligible,
        discount_applied=eligible and applied_bps > 0,
        reason=reason,
        explanation=EXPLANATIONS[reason],
        applied_discount_bps=applied_bps,
        price_paise=price,
        savings_paise=base_price_paise - price,
    )


# --- Where memberships come from -------------------------------------------------------


class MembershipSource(Protocol):
    async def membership_for(
        self, session: AsyncSession, *, user_id: uuid.UUID, club_id: uuid.UUID, now: datetime
    ) -> Membership | None:
        """The purchaser's membership of this club, from server-owned records. Never from the request."""


class NoMembershipRecords:
    """The backend has no membership table yet, so nobody has a discount to apply."""

    async def membership_for(self, session, *, user_id, club_id, now) -> Membership | None:
        return None


_membership_source: MembershipSource = NoMembershipRecords()


def get_membership_source() -> MembershipSource:
    return _membership_source


def set_membership_source(source: MembershipSource) -> None:
    """Plug in MemberCore's records once the team approves where they live."""
    global _membership_source
    _membership_source = source


# --- Quotes ----------------------------------------------------------------------------


def to_ms(moment: datetime) -> int:
    delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta // timedelta(milliseconds=1)


async def price_for(
    session: AsyncSession, *, user_id: uuid.UUID, event: Event, category: TicketCategory, now: datetime
) -> DiscountResult:
    """The unit price this purchaser pays right now, from server data only."""
    membership = None
    if category.member_discount_eligible:
        # A category the organizer marked as not eligible is priced as if no membership
        # were presented. The calculator itself is unchanged.
        membership = await get_membership_source().membership_for(
            session, user_id=user_id, club_id=event.club_id, now=now
        )
    return calculate_ticket_discount(
        base_price_paise=category.base_price_paise,
        event_club_id=str(event.club_id),
        membership=membership,
        now_ms=to_ms(now),
    )


async def issue_quote(
    session: AsyncSession, *, user_id: uuid.UUID, event: Event, category: TicketCategory, quantity: int, now: datetime
) -> TicketQuote:
    """Work out and store a short-lived price for this purchaser, category and quantity."""
    price = await price_for(session, user_id=user_id, event=event, category=category, now=now)
    quote = TicketQuote(
        id=uuid.uuid4(),
        purchaser_user_id=user_id,
        event_id=event.id,
        ticket_category_id=category.id,
        quantity=quantity,
        base_unit_price_paise=category.base_price_paise,
        unit_price_paise=price.price_paise,
        total_price_paise=price.price_paise * quantity,
        member_eligible=price.eligible,
        applied_discount_bps=price.applied_discount_bps,
        discount_applied=price.discount_applied,
        discount_reason=price.reason,
        discount_explanation=price.explanation,
        calculator_version=CALCULATOR_VERSION,
        category_pricing_version=category.pricing_version,
        issued_at=now,
        expires_at=now + timedelta(seconds=get_settings().fairseat_quote_seconds),
    )
    session.add(quote)
    await session.flush()
    return quote
