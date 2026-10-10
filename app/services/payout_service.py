"""Paying approved expense claims back over UPI.

Rules, all checked on the server from the locked claim row:

- Only an `approved` claim can be paid out. A claim reaches `approved` only through the
  existing approval workflow, so the two-person rule for claims of Rs 1,000 or more (two
  different approvers, neither the claimant) has already held.
- Only the treasurer or president starts a payout, and never for their own claim.
- The payee's UPI id must be `verified` by the payout provider. A UPI id that only matches the
  format is not enough.
- The claim becomes `paid` (and its ledger entry `paid`) only when the provider reports the
  payout `processed`. Failed and reversed payouts leave the claim `approved` for a retry.

Locks and the provider. Like FairSeat refunds, a payout is three steps: claim it (locks,
commit), call the provider (no locks), record the answer (locks, commit). The provider gets
the same idempotency key on every retry of one attempt, so a payout is never sent twice.

PAYOUTS_PROVIDER=mock moves no money and is labelled as a mock wherever it shows.
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import FINANCE_ADMINS, CurrentUser
from app.core.errors import AppError, conflict, forbidden, not_found
from app.models.expense import ExpenseClaim
from app.models.ledger import ApprovalLog
from app.models.member2 import ReimbursementPayout, UserPaymentProfile
from app.models.shared import User
from app.schemas.member2 import PayoutOut, PayoutReadiness
from app.services import audit_service, ledger_service, modules, profile_service
from app.services.razorpay_provider import MockPayoutProvider, PayoutProvider, PayoutResult, RazorpayXPayoutProvider

logger = logging.getLogger("whodoo.payouts")
LIVE = ("initiated", "processing", "processed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache
def _mock() -> MockPayoutProvider:
    return MockPayoutProvider()


@lru_cache
def get_payout_provider() -> PayoutProvider | None:
    """The configured payout provider, or None when payouts are off."""
    settings = get_settings()
    choice = settings.payouts_provider.strip().lower()
    if choice == "mock":
        return _mock()
    if choice == "razorpayx":
        key = settings.razorpay_key_id.strip()
        if key.startswith("rzp_test_") and settings.razorpay_key_secret.strip() and settings.razorpayx_account_number.strip():
            return RazorpayXPayoutProvider(key, settings.razorpay_key_secret.strip(), settings.razorpayx_account_number.strip())
        logger.warning("PAYOUTS_PROVIDER=razorpayx needs rzp_test_ keys and RAZORPAYX_ACCOUNT_NUMBER; payouts are off.")
    return None


def _paise(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).to_integral_value())


def _out(row: ReimbursementPayout) -> PayoutOut:
    return PayoutOut(
        id=row.id, expense_claim_id=row.expense_claim_id, attempt=row.attempt, amount_paise=row.amount_paise,
        upi_masked=row.upi_masked, provider=row.provider, is_mock=row.provider == "mock", status=row.status,
        provider_payout_id=row.provider_payout_id, utr=row.utr, failure_reason=row.failure_reason,
        created_at=row.created_at, updated_at=row.updated_at,
    )


async def has_payout_in_flight(session: AsyncSession, payee_id: uuid.UUID) -> bool:
    if not modules.migrated("profile"):
        return False
    found = await session.scalar(
        select(ReimbursementPayout.id).where(
            ReimbursementPayout.payee_id == payee_id, ReimbursementPayout.status.in_(("initiated", "processing"))
        ).limit(1)
    )
    return found is not None


async def live_payout_for_claim(session: AsyncSession, claim_id: uuid.UUID) -> ReimbursementPayout | None:
    if not modules.migrated("profile"):
        return None
    return await session.scalar(
        select(ReimbursementPayout).where(
            ReimbursementPayout.expense_claim_id == claim_id, ReimbursementPayout.status.in_(LIVE)
        )
    )


def _why_not(claim: ExpenseClaim | None, admin: CurrentUser, profile: UserPaymentProfile | None,
             provider: PayoutProvider | None, live: ReimbursementPayout | None) -> AppError | None:
    if claim is None:
        return not_found("Expense claim")
    if admin.role not in FINANCE_ADMINS:
        return forbidden("Only the treasurer or president can pay out a claim")
    if admin.id == claim.submitted_by:
        return AppError(403, "self_payment", "You cannot pay out your own claim")
    if live is not None:
        state = "already been paid out" if live.status == "processed" else "a payout in progress"
        return conflict("payout_exists", f"This claim has {state}.")
    if claim.status != "approved":
        return conflict("not_approved", f"Only approved claims can be paid out. This claim is {claim.status.replace('_', ' ')}.")
    if provider is None:
        return conflict("payouts_off", "UPI payouts are not switched on. Pay the claim another way and mark it paid.")
    if profile is None:
        return conflict("no_upi_id", "The claimant has not added a UPI id to their profile.")
    if profile.verification_status != "verified":
        return conflict("upi_unverified", "The claimant's UPI id has not been verified by the payout provider.")
    return None


async def readiness(session: AsyncSession, admin: CurrentUser, claim_id: uuid.UUID) -> PayoutReadiness:
    if admin.role not in FINANCE_ADMINS:
        raise forbidden("Only the treasurer or president can see payouts")
    claim = await session.scalar(select(ExpenseClaim).where(ExpenseClaim.id == claim_id, ExpenseClaim.club_id == admin.club_id))
    if claim is None:
        raise not_found("Expense claim")
    if not modules.migrated("profile"):
        return PayoutReadiness(claim_id=claim_id, can_pay_out=False, reason="UPI payouts are not set up in this database.")
    provider = get_payout_provider()
    profile = await profile_service.payment_profile_for(session, claim.submitted_by, admin.club_id)
    error = _why_not(claim, admin, profile, provider, await live_payout_for_claim(session, claim.id))
    rows = await session.scalars(
        select(ReimbursementPayout).where(ReimbursementPayout.expense_claim_id == claim.id).order_by(ReimbursementPayout.attempt)
    )
    return PayoutReadiness(
        claim_id=claim.id, can_pay_out=error is None, reason=error.message if error else None,
        provider=provider.name if provider else None, is_mock=bool(provider and provider.is_mock),
        payee_upi_masked=profile.upi_masked if profile else None,
        payee_verification=profile.verification_status if profile else None,
        payouts=[_out(r) for r in rows],
    )


async def _claim_attempt(session: AsyncSession, admin: CurrentUser, claim_id: uuid.UUID) -> ReimbursementPayout:
    """Step 1, under the claim lock: check everything and write the attempt down."""
    claim = await session.scalar(
        select(ExpenseClaim).where(ExpenseClaim.id == claim_id, ExpenseClaim.club_id == admin.club_id).with_for_update()
    )
    provider = get_payout_provider()
    profile = await profile_service.payment_profile_for(session, claim.submitted_by, admin.club_id) if claim else None
    live = await live_payout_for_claim(session, claim_id) if claim else None
    if error := _why_not(claim, admin, profile, provider, live):
        raise error
    attempt = (await session.scalar(
        select(func.coalesce(func.max(ReimbursementPayout.attempt), 0)).where(ReimbursementPayout.expense_claim_id == claim.id)
    )) + 1
    row = ReimbursementPayout(
        id=uuid.uuid4(), club_id=admin.club_id, expense_claim_id=claim.id, payee_id=claim.submitted_by,
        initiated_by=admin.id, attempt=attempt, amount_paise=_paise(claim.amount), currency="INR",
        upi_masked=profile.upi_masked, provider=provider.name, status="initiated",
        idempotency_key=f"whodoo-claim-{claim.id.hex}-{attempt}",
    )
    session.add(row)
    await session.flush()
    audit_service.record(
        session, club_id=admin.club_id, actor_id=admin.id, action="payout.initiated", entity_type="expense_claim",
        entity_id=claim.id, details={"attempt": attempt, "paise": row.amount_paise, "provider": provider.name,
                                     "upi": profile.upi_masked},
    )
    return row


async def _send(session: AsyncSession, payout_id: uuid.UUID) -> PayoutResult:
    """Step 2, no locks held: ask the provider to pay. Retrying sends the same idempotency key."""
    row = await session.get(ReimbursementPayout, payout_id)
    profile = await profile_service.payment_profile_for(session, row.payee_id, row.club_id)
    payee = await session.get(User, row.payee_id)
    if profile is None or profile.verification_status != "verified":
        return PayoutResult(id=None, status="failed", failure_reason="the payee's verified UPI id was removed")
    provider = get_payout_provider()
    if provider is None or provider.name != row.provider:
        return PayoutResult(id=None, status="failed", failure_reason="the payout provider is no longer configured")
    try:
        return await run_in_threadpool(
            provider.create_payout, vpa=profile_service.decrypt_upi(profile.upi_ciphertext), name=payee.full_name,
            fund_account_id=profile.verification_reference if provider.name == profile.verification_provider else None,
            amount_paise=row.amount_paise, idempotency_key=row.idempotency_key,
            reference=f"claim-{row.expense_claim_id.hex[:20]}", narration="WhoDoo reimbursement",
        )
    except AppError as exc:
        # Unknown outcome: the provider may or may not have accepted it. Leave it `initiated`;
        # a refresh or the webhook settles it, and a retry reuses the idempotency key.
        logger.warning("Payout %s: provider call failed: %s", row.id, exc.message)
        return PayoutResult(id=None, status="initiated", failure_reason=exc.message)


async def apply_result(session: AsyncSession, payout_id: uuid.UUID, result: PayoutResult,
                       actor_id: uuid.UUID | None) -> ReimbursementPayout:
    """Step 3, under the claim lock: record what the provider said. Safe to repeat."""
    row = await session.get(ReimbursementPayout, payout_id)
    claim = await session.scalar(select(ExpenseClaim).where(ExpenseClaim.id == row.expense_claim_id).with_for_update())
    row = await session.scalar(
        select(ReimbursementPayout).where(ReimbursementPayout.id == payout_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if result.status == "initiated" or row.status in ("failed", "reversed") or row.status == result.status:
        return row  # nothing new, or a late echo for an attempt that is already closed
    before = row.status
    if result.id and not row.provider_payout_id:
        row.provider_payout_id = result.id
    row.utr = result.utr or row.utr
    row.failure_reason = result.failure_reason
    row.updated_at = utcnow()

    if result.status == "processed" and before in ("initiated", "processing"):
        row.status = "processed"
        if claim.status == "approved":
            entry = await ledger_service.get_entry_for_reference(
                session, source_module="expense_claim", reference_id=claim.id, entry_type="reimbursement"
            )
            if entry is None:
                raise AppError(500, "ledger_entry_missing", "This approved claim has no ledger entry")
            await ledger_service.set_payment_status(session, entry, "paid", actor_id=actor_id)
            claim.status = "paid"
            actor = await session.get(User, actor_id or row.initiated_by)
            session.add(ApprovalLog(expense_claim_id=claim.id, approver_id=actor.id, approver_role=actor.role,
                                    action="paid", comment=f"Paid by UPI to {row.upi_masked} ({row.provider} payout)"))
    elif result.status == "processing" and before == "initiated":
        row.status = "processing"
    elif result.status in ("failed", "reversed"):
        row.status = result.status
        if before == "processed":
            # Money came back after the claim was recorded as paid. History is not rewritten:
            # the treasurer has to sort it out (and the audit log says so).
            audit_service.record(
                session, club_id=row.club_id, actor_id=None, action="payout.reversed_after_paid",
                entity_type="expense_claim", entity_id=row.expense_claim_id, details={"payout_id": str(row.id)},
            )
    await session.flush()
    audit_service.record(
        session, club_id=row.club_id, actor_id=actor_id, action=f"payout.{row.status}", entity_type="expense_claim",
        entity_id=row.expense_claim_id,
        details={"attempt": row.attempt, "from": before, "provider_payout_id": row.provider_payout_id,
                 "reason": row.failure_reason},
    )
    return row


async def pay_out(session: AsyncSession, admin: CurrentUser, claim_id: uuid.UUID) -> PayoutOut:
    """Start a payout and record the provider's first answer. Commits between the steps."""
    row = await _claim_attempt(session, admin, claim_id)
    payout_id = row.id
    await session.commit()
    result = await _send(session, payout_id)
    row = await apply_result(session, payout_id, result, admin.id)
    await session.commit()
    return _out(row)


async def refresh(session: AsyncSession, admin: CurrentUser, payout_id: uuid.UUID) -> PayoutOut:
    """Ask the provider again about an attempt that has not settled."""
    if admin.role not in FINANCE_ADMINS:
        raise forbidden("Only the treasurer or president can manage payouts")
    row = await session.scalar(
        select(ReimbursementPayout).where(ReimbursementPayout.id == payout_id, ReimbursementPayout.club_id == admin.club_id)
    )
    if row is None:
        raise not_found("Payout")
    if row.status in ("initiated", "processing"):
        provider = get_payout_provider()
        if row.provider_payout_id and provider is not None:
            result = await run_in_threadpool(provider.fetch_payout, row.provider_payout_id)
        else:
            result = await _send(session, row.id)   # same idempotency key: never a second payout
        if result is not None:
            row = await apply_result(session, row.id, result, admin.id)
            await session.commit()
    return _out(row)


async def handle_provider_event(session: AsyncSession, kind: str, payload: dict[str, Any]) -> str:
    """A verified RazorpayX payout event. The status is read back from the API, never the body alone."""
    entity = (payload.get("payout") or {}).get("entity") or {}
    payout_id = entity.get("id")
    if not payout_id or not modules.migrated("profile"):
        return "ignored"
    row = await session.scalar(select(ReimbursementPayout).where(ReimbursementPayout.provider_payout_id == payout_id))
    if row is None:
        return "ignored"
    provider = get_payout_provider()
    fetched = await run_in_threadpool(provider.fetch_payout, payout_id) if provider else None
    if fetched is None:
        return "ignored"
    await apply_result(session, row.id, fetched, None)
    return "processed"
