"""MemberCore: membership plans, enrollment, dues and the member discount.

A membership becomes active in exactly three ways, each leaving proof the database insists on
(migrations/031):

    paid online      the payment provider confirms the checkout session for the dues
    paid in person   a treasurer or president records the money they received
    complimentary    a treasurer or president waives the dues, with a reason

Nothing here reads a status, price or user from the request: the plan and the amount come
from the plan row, the member is whoever is signed in. `users.is_active_member` (which
merchandise pricing reads) is kept in step with the membership by this module only.

Renewing while still active extends the same membership: the new term keeps the original
start and adds the plan's days to the current expiry, so benefits never lapse in between.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, conflict, forbidden, not_found
from app.models.membercore import (
    Membership,
    MembershipActivationGrant,
    MembershipCheckout,
    MembershipDues,
    MembershipDuesReconciliation,
    MembershipPlan,
    MembershipRenewal,
)
from app.models.shared import User
from app.schemas.membercore import MembershipCheckoutOut, MembershipOut, PlanIn, PlanOut, PlanUpdate
from app.services import audit_service, fairseat_pricing, ledger_service, modules, payment_service
from app.services.payment_service import CheckoutLine, CheckoutRequest, CheckoutSession

# An unpaid enrollment is picked up again rather than repeated, while it is this fresh.
REUSE_UNPAID_FOR = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rupees(paise: int) -> Decimal:
    return Decimal(paise) / Decimal(100)


def _require_officer(user: CurrentUser) -> None:
    if not user.is_finance_admin:
        raise forbidden("Only the treasurer or president can manage memberships")


# --- Plans -----------------------------------------------------------------------------


async def list_plans(session: AsyncSession, user: CurrentUser, include_hidden: bool = False) -> list[PlanOut]:
    stmt = select(MembershipPlan).where(MembershipPlan.club_id == user.club_id)
    if not (include_hidden and user.is_finance_admin):
        stmt = stmt.where(MembershipPlan.is_active)
    rows = await session.scalars(stmt.order_by(MembershipPlan.price_paise, MembershipPlan.name))
    return [PlanOut.model_validate(plan) for plan in rows]


async def create_plan(session: AsyncSession, user: CurrentUser, data: PlanIn) -> PlanOut:
    _require_officer(user)
    plan = MembershipPlan(id=uuid.uuid4(), club_id=user.club_id, currency="INR", **data.model_dump())
    session.add(plan)
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="membercore.plan_created",
        entity_type="membership_plan", entity_id=plan.id, details=data.model_dump(),
    )
    return PlanOut.model_validate(plan)


async def update_plan(session: AsyncSession, user: CurrentUser, plan_id: uuid.UUID, data: PlanUpdate) -> PlanOut:
    """Changing a plan affects future enrollments only: bought terms keep the discount they bought."""
    _require_officer(user)
    plan = await session.scalar(
        select(MembershipPlan).where(MembershipPlan.id == plan_id, MembershipPlan.club_id == user.club_id).with_for_update()
    )
    if plan is None:
        raise not_found("Membership plan")
    changes = data.model_dump(exclude_unset=True)
    for name, value in changes.items():
        if value is None and name != "description":
            continue
        setattr(plan, name, value)
    plan.updated_at = utcnow()
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="membercore.plan_updated",
        entity_type="membership_plan", entity_id=plan.id, details=changes,
    )
    return PlanOut.model_validate(plan)


# --- Reading a membership --------------------------------------------------------------


@dataclass
class _Standing:
    membership: Membership | None
    renewal: MembershipRenewal | None = None   # the latest one
    dues: MembershipDues | None = None
    granted: bool = False                      # the latest renewal has its activation proof

    @property
    def owing(self) -> bool:
        return self.renewal is not None and not self.granted


async def _standing(session: AsyncSession, user_id: uuid.UUID, club_id: uuid.UUID, lock: bool = False) -> _Standing:
    stmt = select(Membership).where(Membership.user_id == user_id, Membership.club_id == club_id)
    membership = await session.scalar(stmt.with_for_update() if lock else stmt)
    if membership is None:
        return _Standing(None)
    renewal = await session.scalar(
        select(MembershipRenewal)
        .where(MembershipRenewal.membership_id == membership.id)
        .order_by(MembershipRenewal.renewal_number.desc())
        .limit(1)
    )
    if renewal is None:
        return _Standing(membership)
    dues = await session.scalar(select(MembershipDues).where(MembershipDues.renewal_id == renewal.id))
    granted = await session.scalar(
        select(MembershipActivationGrant.id).where(MembershipActivationGrant.renewal_id == renewal.id)
    )
    return _Standing(membership, renewal, dues, granted is not None)


async def _present(session: AsyncSession, person: User | CurrentUser, standing: _Standing, now: datetime) -> MembershipOut:
    out = MembershipOut(user_id=person.id, full_name=person.full_name, email=person.email)
    membership = standing.membership
    if membership is None:
        return out
    plans = {
        plan.id: PlanOut.model_validate(plan)
        for plan in await session.scalars(
            select(MembershipPlan).where(
                MembershipPlan.id.in_({membership.plan_id} | ({standing.renewal.plan_id} if standing.renewal else set()))
            )
        )
    }
    out.membership_id = membership.id
    out.status = "expired" if membership.status == "active" and membership.expires_at <= now else membership.status
    out.plan = plans.get(membership.plan_id)
    out.starts_at, out.expires_at = membership.starts_at, membership.expires_at
    out.discount_bps = membership.discount_bps_snapshot if out.status == "active" else 0
    if standing.owing and out.status != "cancelled":
        amount = standing.dues.amount_paise if standing.dues else 0
        out.awaiting = "payment" if amount > 0 else "approval"
        out.awaiting_plan = plans.get(standing.renewal.plan_id)
        out.amount_due_paise = amount
        out.new_expires_at = standing.renewal.new_expires_at
    return out


async def expire_lapsed(session: AsyncSession, club_id: uuid.UUID, now: datetime | None = None) -> int:
    """Mark memberships whose term has run out, and stop their member pricing elsewhere."""
    now = now or utcnow()
    lapsed = list(await session.scalars(
        update(Membership)
        .where(Membership.club_id == club_id, Membership.status == "active", Membership.expires_at <= now)
        .values(status="expired", updated_at=now)
        .returning(Membership.user_id)
    ))
    if lapsed:
        await session.execute(update(User).where(User.id.in_(lapsed)).values(is_active_member=False))
    return len(lapsed)


async def my_membership(session: AsyncSession, user: CurrentUser, now: datetime | None = None) -> MembershipOut:
    now = now or utcnow()
    out = await _present(session, user, await _standing(session, user.id, user.club_id), now)
    if user.role == "president":
        # The president is a member at the top tier until they leave the office: never pending,
        # never expired, every event free. Nothing is bought, so there is no term to run out.
        out.status, out.permanent, out.discount_bps = "active", True, 10000
        out.awaiting = out.awaiting_plan = out.new_expires_at = None
        out.amount_due_paise = 0
    return out


async def keep_president_a_member(session: AsyncSession, user) -> None:
    """The president always has member standing (which merchandise pricing reads)."""
    if user.role == "president" and not user.is_active_member:
        await session.execute(update(User).where(User.id == user.id).values(is_active_member=True))


async def list_members(session: AsyncSession, user: CurrentUser, now: datetime | None = None) -> list[MembershipOut]:
    """Everyone in the club with their standing. Treasurer and president only."""
    _require_officer(user)
    now = now or utcnow()
    people = await session.scalars(select(User).where(User.club_id == user.club_id).order_by(User.full_name))
    return [await _present(session, person, await _standing(session, person.id, user.club_id), now) for person in people]


# --- Enrolling and renewing ------------------------------------------------------------


async def enroll(session: AsyncSession, user: CurrentUser, plan_id: uuid.UUID, now: datetime | None = None) -> MembershipOut:
    """Join, rejoin or renew on a plan. Creates what is owed; activates nothing."""
    now = now or utcnow()
    plan = await session.scalar(
        select(MembershipPlan).where(MembershipPlan.id == plan_id, MembershipPlan.club_id == user.club_id)
    )
    if plan is None or not plan.is_active:
        raise not_found("Membership plan")

    standing = await _standing(session, user.id, user.club_id, lock=True)
    membership, latest = standing.membership, standing.renewal
    live = membership is not None and membership.status == "active" and membership.expires_at > now

    same_offer = (
        standing.owing and standing.dues is not None and latest.plan_id == plan.id
        and latest.discount_bps_snapshot == plan.ticket_discount_bps
        and standing.dues.amount_paise == plan.price_paise
        and latest.created_at > now - REUSE_UNPAID_FOR
    )
    if not same_offer:
        length = timedelta(days=plan.validity_days)
        previous = (membership.starts_at, membership.expires_at) if membership is not None else (None, None)
        if live:
            starts, expires = membership.starts_at, membership.expires_at + length
        else:
            starts, expires = now, now + length
        if membership is None:
            membership = Membership(
                id=uuid.uuid4(), user_id=user.id, club_id=user.club_id, plan_id=plan.id, status="pending",
                starts_at=starts, expires_at=expires, discount_bps_snapshot=plan.ticket_discount_bps,
            )
            session.add(membership)
        elif not live:
            membership.status = "pending"
            membership.plan_id, membership.discount_bps_snapshot = plan.id, plan.ticket_discount_bps
            membership.starts_at, membership.expires_at = starts, expires
            membership.updated_at = now
        # A live membership is left exactly as it is until the renewal is paid for.
        await session.flush()
        renewal = MembershipRenewal(
            id=uuid.uuid4(), membership_id=membership.id, club_id=user.club_id,
            renewal_number=(latest.renewal_number if latest else 0) + 1, request_id=uuid.uuid4(), plan_id=plan.id,
            previous_starts_at=previous[0], previous_expires_at=previous[1],
            new_starts_at=starts, new_expires_at=expires,
            discount_bps_snapshot=plan.ticket_discount_bps, actor_user_id=user.id,
        )
        session.add(renewal)
        await session.flush()
        session.add(MembershipDues(
            id=uuid.uuid4(), membership_id=membership.id, club_id=user.club_id, renewal_id=renewal.id,
            amount_paise=plan.price_paise, currency="INR", idempotency_key=uuid.uuid4(),
        ))
        await session.flush()
        audit_service.record(
            session, club_id=user.club_id, actor_id=user.id,
            action="membercore.renewal_requested" if live else "membercore.enrolled",
            entity_type="membership", entity_id=membership.id,
            details={"plan_id": str(plan.id), "paise": plan.price_paise, "renewal_number": renewal.renewal_number},
        )
    return await _present(session, user, await _standing(session, user.id, user.club_id), now)


# --- Activation ------------------------------------------------------------------------


async def _activate(session: AsyncSession, membership: Membership, renewal: MembershipRenewal, now: datetime) -> None:
    """Make the latest renewal the current term. The caller has already inserted its grant."""
    membership.plan_id = renewal.plan_id
    membership.starts_at, membership.expires_at = renewal.new_starts_at, renewal.new_expires_at
    membership.discount_bps_snapshot = renewal.discount_bps_snapshot
    membership.status = "active"
    membership.updated_at = now
    await session.flush()
    await session.execute(update(User).where(User.id == membership.user_id).values(is_active_member=True))


async def _owing_for(session: AsyncSession, admin: CurrentUser, user_id: uuid.UUID) -> _Standing:
    _require_officer(admin)
    standing = await _standing(session, user_id, admin.club_id, lock=True)
    if standing.membership is None:
        raise not_found("Membership")
    if not standing.owing or standing.membership.status == "cancelled":
        raise conflict("nothing_owed", "This person has no enrollment or renewal waiting to be settled.")
    return standing


async def waive(
    session: AsyncSession, admin: CurrentUser, user_id: uuid.UUID, reason: str, now: datetime | None = None
) -> MembershipOut:
    """Complimentary membership: no payment is invented, the waiver and its reason are the proof."""
    now = now or utcnow()
    standing = await _owing_for(session, admin, user_id)
    session.add(MembershipActivationGrant(
        id=uuid.uuid4(), membership_id=standing.membership.id, club_id=admin.club_id, renewal_id=standing.renewal.id,
        source="complimentary_waiver", waived_by_user_id=admin.id, waiver_reason=reason, request_id=uuid.uuid4(),
    ))
    await session.flush()
    await _activate(session, standing.membership, standing.renewal, now)
    audit_service.record(
        session, club_id=admin.club_id, actor_id=admin.id, action="membercore.dues_waived",
        entity_type="membership", entity_id=standing.membership.id, details={"reason": reason},
    )
    return await _present(session, await session.get(User, user_id), await _standing(session, user_id, admin.club_id), now)


async def _settle(
    session: AsyncSession, standing: _Standing, *, now: datetime, source: str, provider: str | None,
    provider_event_id: str | None, verified_by: uuid.UUID | None, evidence: str | None, ledger_key: str,
) -> None:
    """Record dues as paid, grant activation, activate, and put the income in the ledger."""
    membership, renewal, dues = standing.membership, standing.renewal, standing.dues
    reconciliation = MembershipDuesReconciliation(
        id=uuid.uuid4(), dues_id=dues.id, club_id=dues.club_id, source=source, provider=provider,
        provider_event_id=provider_event_id, request_id=uuid.uuid4(), outcome="paid", verified_at=now,
        verified_by_user_id=verified_by, evidence_reference=evidence,
    )
    session.add(reconciliation)
    await session.flush()
    session.add(MembershipActivationGrant(
        id=uuid.uuid4(), membership_id=membership.id, club_id=dues.club_id, renewal_id=renewal.id,
        source="paid_settlement", dues_id=dues.id, reconciliation_id=reconciliation.id, request_id=uuid.uuid4(),
    ))
    await session.flush()
    await _activate(session, membership, renewal, now)
    await ledger_service.record_income(
        session, club_id=dues.club_id, source_module="membership", amount=rupees(dues.amount_paise),
        created_by=verified_by or membership.user_id, reference_id=dues.id, reference_type="membership_dues",
        payer_id=membership.user_id, category="membership_dues",
        description="Membership dues" + (f" ({evidence})" if evidence and source == "offline_admin" else ""),
        idempotency_key=ledger_key,
    )


async def record_offline_payment(
    session: AsyncSession, admin: CurrentUser, user_id: uuid.UUID, reference: str, now: datetime | None = None
) -> MembershipOut:
    """Dues handed over in person. The officer recording it is named on the record."""
    now = now or utcnow()
    standing = await _owing_for(session, admin, user_id)
    if standing.dues is None or standing.dues.amount_paise == 0:
        raise conflict("nothing_to_pay", "This plan is free. Approve it as complimentary instead.")
    await _settle(
        session, standing, now=now, source="offline_admin", provider=None, provider_event_id=None,
        verified_by=admin.id, evidence=reference, ledger_key=f"membership_dues:{standing.dues.id}:income",
    )
    audit_service.record(
        session, club_id=admin.club_id, actor_id=admin.id, action="membercore.offline_payment_recorded",
        entity_type="membership", entity_id=standing.membership.id,
        details={"reference": reference, "paise": standing.dues.amount_paise},
    )
    return await _present(session, await session.get(User, user_id), await _standing(session, user_id, admin.club_id), now)


async def cancel(
    session: AsyncSession, admin: CurrentUser, user_id: uuid.UUID, reason: str, now: datetime | None = None
) -> MembershipOut:
    _require_officer(admin)
    now = now or utcnow()
    standing = await _standing(session, user_id, admin.club_id, lock=True)
    if standing.membership is None:
        raise not_found("Membership")
    if standing.membership.status == "cancelled":
        raise conflict("already_cancelled", "This membership is already cancelled.")
    standing.membership.status = "cancelled"
    standing.membership.updated_at = now
    await session.flush()
    await session.execute(update(User).where(User.id == user_id).values(is_active_member=False))
    audit_service.record(
        session, club_id=admin.club_id, actor_id=admin.id, action="membercore.cancelled",
        entity_type="membership", entity_id=standing.membership.id, details={"reason": reason},
    )
    return await _present(session, await session.get(User, user_id), await _standing(session, user_id, admin.club_id), now)


# --- Paying dues online ----------------------------------------------------------------


async def create_checkout_session(session: AsyncSession, user: CurrentUser, now: datetime | None = None) -> MembershipCheckoutOut:
    """Open (or reopen) a checkout session for what the caller owes. The amount is the dues row's."""
    now = now or utcnow()
    standing = await _standing(session, user.id, user.club_id)
    if standing.membership is None or not standing.owing or standing.membership.status == "cancelled":
        raise conflict("nothing_owed", "You have no membership dues waiting to be paid.")
    dues = standing.dues
    if dues is None or dues.amount_paise == 0:
        raise conflict("nothing_to_pay", "This plan is free. An officer will approve your membership.")
    plan = await session.get(MembershipPlan, standing.renewal.plan_id)

    provider = payment_service.get_provider()
    existing = await session.scalar(
        select(MembershipCheckout)
        .where(MembershipCheckout.dues_id == dues.id, MembershipCheckout.status == "open")
        .order_by(MembershipCheckout.created_at.desc())
        .limit(1)
    )
    current: CheckoutSession | None = None
    if existing is not None:
        current = await run_in_threadpool(provider.verify_payment, existing.checkout_session_id)
        if current is not None and current.payment_status == "paid":
            raise conflict("payment_received", "Your payment has been received and is being confirmed.")
    if current is None or current.status != "open":
        base = get_settings().app_base_url.rstrip("/")
        request = CheckoutRequest(
            amount_paise=dues.amount_paise, currency="inr",
            lines=[CheckoutLine(name=f"{plan.name} membership", unit_paise=dues.amount_paise, quantity=1)],
            metadata={"membership_dues_id": str(dues.id), "user_id": str(user.id), "club_id": str(user.club_id)},
            success_url=f"{base}/memberships/return?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/memberships",
            reference=str(dues.id),
        )
        try:
            current = await run_in_threadpool(provider.create_checkout_session, request)
        except payment_service.ProviderRejectedKey as exc:
            provider = payment_service._fall_back_to_mock(str(exc))
            current = await run_in_threadpool(provider.create_checkout_session, request)
        if current.amount_total != dues.amount_paise:
            raise AppError(502, "payment_provider_error", "The payment service returned the wrong amount")
        session.add(MembershipCheckout(
            id=uuid.uuid4(), dues_id=dues.id, club_id=user.club_id, user_id=user.id, provider=provider.name,
            checkout_session_id=current.id, status="open",
        ))
        await session.flush()
    return MembershipCheckoutOut(
        provider=provider.name, is_mock=provider.is_mock, session_id=current.id, checkout_url=current.url,
        amount=dues.amount_paise, currency="INR",
    )


async def record_payment(session: AsyncSession, paid: CheckoutSession, now: datetime | None = None) -> str:
    """Apply a checkout session the provider itself reports. Safe to call any number of times.

    Returns activated, already_recorded, not_paid, amount_mismatch, refund_due or unknown.
    refund_due: the money arrived for dues that can no longer be honoured (already settled
    another way, or replaced by a newer enrollment), so it is owed back.
    """
    now = now or utcnow()
    checkout = await session.scalar(
        select(MembershipCheckout).where(MembershipCheckout.checkout_session_id == paid.id).with_for_update()
    )
    if checkout is None:
        return "unknown"
    if checkout.status in ("paid", "refund_due", "refunded"):
        return "already_recorded"
    if paid.payment_status != "paid":
        return "not_paid"
    dues = await session.get(MembershipDues, checkout.dues_id)
    if paid.amount_total != dues.amount_paise or (paid.currency or "").lower() != dues.currency.lower():
        checkout.status, checkout.detail, checkout.updated_at = "failed", "amount or currency mismatch", now
        await session.flush()
        return "amount_mismatch"

    standing = await _standing(session, checkout.user_id, checkout.club_id, lock=True)
    checkout.payment_intent_id = paid.payment_intent_id
    checkout.updated_at = now
    still_owed = (
        standing.owing and standing.renewal.id == dues.renewal_id and standing.membership.status != "cancelled"
    )
    if not still_owed:
        checkout.status, checkout.detail = "refund_due", "the dues were already settled or replaced"
        await session.flush()
        audit_service.record(
            session, club_id=checkout.club_id, actor_id=None, action="membercore.payment_refund_due",
            entity_type="membership_dues", entity_id=dues.id, details={"session_id": paid.id},
        )
        return "refund_due"
    await _settle(
        session, standing, now=now, source="provider_webhook", provider=checkout.provider,
        provider_event_id=f"checkout:{paid.id}", verified_by=None, evidence=paid.payment_intent_id,
        ledger_key=f"membership_dues:{dues.id}:income",
    )
    checkout.status = "paid"
    await session.flush()
    audit_service.record(
        session, club_id=checkout.club_id, actor_id=checkout.user_id, action="membercore.payment_recorded",
        entity_type="membership", entity_id=standing.membership.id,
        details={"session_id": paid.id, "paise": dues.amount_paise, "provider": checkout.provider},
    )
    return "activated"


async def refund_unhonoured(session: AsyncSession, session_id: str, now: datetime | None = None) -> str | None:
    """Send back a payment marked refund_due. Call after committing, never while holding locks."""
    now = now or utcnow()
    checkout = await session.scalar(
        select(MembershipCheckout).where(MembershipCheckout.checkout_session_id == session_id)
    )
    if checkout is None or checkout.status != "refund_due" or not checkout.payment_intent_id:
        return None
    dues = await session.get(MembershipDues, checkout.dues_id)
    result = await run_in_threadpool(
        payment_service.get_provider().refund, checkout.payment_intent_id, dues.amount_paise,
        f"membership_checkout:{checkout.id}:refund",
    )
    if result.status == "succeeded":
        checkout.status, checkout.updated_at = "refunded", now
    else:
        checkout.detail = f"refund {result.status}: {result.failure_reason or 'waiting for the provider'}"
    await session.flush()
    return result.status


async def verify_payment(session: AsyncSession, user: CurrentUser, session_id: str, now: datetime | None = None) -> str:
    """The buyer came back from checkout: ask the provider what happened, never the browser."""
    owner = await session.scalar(
        select(MembershipCheckout.user_id).where(MembershipCheckout.checkout_session_id == session_id)
    )
    if owner != user.id:
        raise not_found("Checkout session")
    paid = await run_in_threadpool(payment_service.get_provider().verify_payment, session_id)
    if paid is None:
        raise conflict("session_expired", "This checkout session has expired. Start the payment again.")
    return await record_payment(session, paid, now)


async def handle_webhook(session: AsyncSession, body: bytes, signature: str, now: datetime | None = None) -> str:
    """A signed provider event. Only completed checkouts for membership dues are acted on."""
    provider = payment_service.get_provider()
    try:
        event = provider.handle_webhook(body, signature)
    except payment_service.InvalidWebhookSignature:
        raise AppError(400, "invalid_signature", "Webhook signature could not be verified") from None
    if event.type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return "ignored"
    if "membership_dues_id" not in (event.data.get("metadata") or {}):
        return "ignored"
    # The event says which session; what was paid is read back from the provider.
    paid = await run_in_threadpool(provider.verify_payment, str(event.data.get("id", "")))
    if paid is None:
        return "ignored"
    return await record_payment(session, paid, now)


async def simulate_mock_payment(session: AsyncSession, user: CurrentUser, session_id: str, succeed: bool) -> str:
    """Mock provider only: stand in for the buyer on the checkout page."""
    provider = payment_service.get_provider()
    if not isinstance(provider, payment_service.MockPaymentProvider):
        raise not_found("Mock payments")
    checkout = await session.scalar(
        select(MembershipCheckout).where(MembershipCheckout.checkout_session_id == session_id)
    )
    if checkout is None or checkout.user_id != user.id:
        raise not_found("Checkout session")
    paid = provider.simulate_payment(session_id, succeed)
    if paid is None:
        raise conflict("session_expired", "This checkout session has expired. Start the payment again.")
    if not succeed:
        if checkout.status == "open":
            checkout.status, checkout.detail = "failed", "card declined"
            await session.flush()
        return "not_paid"
    return await record_payment(session, paid)


# --- The member discount FairSeat applies ----------------------------------------------


class MemberCoreMemberships:
    """FairSeat's source of memberships: the purchaser's row for the event's club, as stored.

    A pending membership is reported as no membership: it grants nothing, and the shared
    discount calculator has no pending status. Expiry is left to the calculator, which
    compares the dates itself.
    """

    async def membership_for(self, session, *, user_id, club_id, now) -> fairseat_pricing.Membership | None:
        if "memberships" not in await modules.load(session):
            return None
        row = await session.scalar(
            select(Membership).where(Membership.user_id == user_id, Membership.club_id == club_id)
        )
        if row is None or row.status == "pending":
            return None
        return fairseat_pricing.Membership(
            status=row.status, club_id=str(row.club_id),
            starts_at_ms=fairseat_pricing.to_ms(row.starts_at), expires_at_ms=fairseat_pricing.to_ms(row.expires_at),
            discount_bps=row.discount_bps_snapshot,
        )
