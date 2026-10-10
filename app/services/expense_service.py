"""Snap-to-Reimburse: expense claims and their approval workflow.

Approval rules (limit = EXPENSE_AUTO_APPROVAL_LIMIT, default ₹1,000):

* Below the limit, one approval is enough: the event lead's, or a treasurer's or
  president's standing in for them.
* At or above the limit, two different people must approve: the first approver,
  then a treasurer or president who did not give the first approval.
* Nobody approves, rejects or pays out their own claim.
* The ledger entry is written exactly once, on final approval, and only moves
  the balance when the claim is marked paid.
"""

import uuid
from decimal import Decimal

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import APPROVERS, FINANCE_ADMINS, CurrentUser
from app.core.errors import AppError, conflict, forbidden, not_found
from app.models.expense import ExpenseClaim, ReceiptUpload
from app.models.ledger import ApprovalLog
from app.schemas.expense import (
    ApprovalLogOut,
    ApprovalQueue,
    ExpenseClaimCreate,
    ExpenseClaimDetail,
    ExpenseClaimOut,
    ExpenseClaimResubmit,
    OcrResponse,
)
from app.services import ledger_service, ocr_service, shared_directory, storage_service

C = ExpenseClaim


def needs_final_approval(amount: Decimal) -> bool:
    return amount >= get_settings().expense_auto_approval_limit


# --- Receipts and OCR ----------------------------------------------------------


async def upload_receipt(session: AsyncSession, user: CurrentUser, data: bytes) -> OcrResponse:
    """Store a receipt, read it with OCR and remember the result. Does not create a claim."""
    extension, content_type = storage_service.identify_receipt(data)
    receipt_url = await run_in_threadpool(storage_service.save_receipt, user.club_id, data, extension, content_type)
    result = await run_in_threadpool(ocr_service.read_receipt, data, content_type)

    upload = ReceiptUpload(
        club_id=user.club_id,
        uploaded_by=user.id,
        receipt_url=receipt_url,
        content_type=content_type,
        ocr_raw_response=result.raw,
        ocr_confidence=result.confidence,
        extracted_amount=result.amount,
        extracted_vendor=result.vendor,
        extracted_date=result.expense_date,
    )
    session.add(upload)
    await session.flush()

    found = [name for name, value in
             (("vendor", result.vendor), ("amount", result.amount), ("date", result.expense_date)) if value]
    if not result.available:
        message = "We could not read this receipt automatically. Please enter the details yourself."
    elif len(found) == 3:
        message = "Receipt read. Check the details before submitting."
    else:
        message = f"Receipt partly read ({', '.join(found) or 'nothing'} found). Fill in the rest."
    return OcrResponse(
        receipt_id=upload.id,
        receipt_url=receipt_url,
        ocr_available=result.available,
        ocr_confidence=float(result.confidence),
        extracted_vendor=result.vendor,
        extracted_amount=result.amount,
        extracted_date=result.expense_date,
        message=message,
    )


async def _claim_receipt(session: AsyncSession, user: CurrentUser, receipt_id: uuid.UUID) -> ReceiptUpload:
    upload = await session.scalar(select(ReceiptUpload).where(ReceiptUpload.id == receipt_id).with_for_update())
    if upload is None or upload.uploaded_by != user.id:
        raise not_found("Receipt")
    if upload.expense_claim_id is not None:
        raise conflict("receipt_already_used", "This receipt is already attached to a claim")
    return upload


def _copy_receipt(claim: ExpenseClaim, upload: ReceiptUpload) -> None:
    claim.receipt_url = upload.receipt_url
    claim.ocr_raw_response = upload.ocr_raw_response
    claim.ocr_confidence = upload.ocr_confidence
    claim.extracted_amount = upload.extracted_amount
    claim.extracted_vendor = upload.extracted_vendor
    claim.extracted_date = upload.extracted_date


# --- Workflow helpers ------------------------------------------------------------


def _log(session: AsyncSession, claim: ExpenseClaim, user: CurrentUser, action: str, comment: str | None = None):
    session.add(
        ApprovalLog(
            expense_claim_id=claim.id, approver_id=user.id, approver_role=user.role, action=action, comment=comment
        )
    )


async def _first_approver(session: AsyncSession, claim: ExpenseClaim) -> uuid.UUID:
    """The linked event's lead; otherwise the treasurer, then the president. Never the submitter."""
    if claim.event_id:
        event = await shared_directory.get_event(session, claim.club_id, claim.event_id)
        if event is None:
            raise not_found("Event")
        if event.lead_id and event.lead_id != claim.submitted_by:
            return event.lead_id
    approver = await _finance_admin(session, claim.club_id, exclude={claim.submitted_by})
    if approver is None:
        raise conflict("no_approver", "Nobody is available to approve this claim")
    return approver


async def _finance_admin(session: AsyncSession, club_id: uuid.UUID, exclude: set[uuid.UUID | None]) -> uuid.UUID | None:
    people = await shared_directory.list_users(session, club_id)
    for role in FINANCE_ADMINS:  # treasurer first, then president
        for person in people:
            if person.role == role and person.id not in exclude:
                return person.id
    return None


async def _locked(session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID) -> ExpenseClaim:
    """Load a claim with a row lock, so two people acting at once are handled one after the other."""
    claim = await session.scalar(
        select(ExpenseClaim).where(C.id == claim_id, C.club_id == user.club_id).with_for_update()
    )
    if claim is None:
        raise not_found("Expense claim")
    return claim


def _why_cannot_decide(claim: ExpenseClaim, user: CurrentUser) -> AppError | None:
    """Return the reason this user may not approve, reject or send back the claim, or None if they may."""
    if claim.status != "pending_approval":
        return conflict("not_pending", f"This claim is already {claim.status.replace('_', ' ')}")
    if user.id == claim.submitted_by:
        return AppError(403, "self_approval", "You cannot decide on your own claim")
    is_admin = user.role in FINANCE_ADMINS
    if claim.approval_stage == "first":
        if is_admin or user.id == claim.current_approver_id:
            return None
        return forbidden("This claim is waiting for a different approver")
    if not is_admin:
        return forbidden("Final approval needs the treasurer or president")
    if user.id == claim.first_approved_by:
        return conflict(
            "duplicate_approval",
            "You already gave the first approval. A different treasurer or president must give the final one",
        )
    return None


def allowed_actions(claim: ExpenseClaim, user: CurrentUser) -> list[str]:
    actions: list[str] = []
    if _why_cannot_decide(claim, user) is None:
        actions += ["approve", "reject", "request_changes"]
    if claim.status == "approved" and user.role in FINANCE_ADMINS and user.id != claim.submitted_by:
        actions.append("mark_paid")
    if claim.status == "change_requested" and user.id == claim.submitted_by:
        actions.append("resubmit")
    return actions


def _ledger_key(claim: ExpenseClaim) -> str:
    return f"expense_claim:{claim.id}:reimbursement"


# --- Volunteer actions -------------------------------------------------------------


async def create_claim(session: AsyncSession, user: CurrentUser, data: ExpenseClaimCreate) -> ExpenseClaim:
    upload = await _claim_receipt(session, user, data.receipt_id)
    claim = ExpenseClaim(
        id=uuid.uuid4(),
        club_id=user.club_id,
        submitted_by=user.id,
        event_id=data.event_id,
        title=data.title,
        description=data.description,
        category=data.category,
        amount=data.amount,
        expense_date=data.expense_date,
        vendor_name=data.vendor_name,
        status="pending_approval",
        approval_stage="first",
    )
    _copy_receipt(claim, upload)
    claim.current_approver_id = await _first_approver(session, claim)
    session.add(claim)
    await session.flush()
    upload.expense_claim_id = claim.id
    _log(session, claim, user, "submitted")
    await session.flush()
    return claim


async def resubmit_claim(
    session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID, data: ExpenseClaimResubmit
) -> ExpenseClaim:
    """Send a claim back into the queue after an approver asked for changes. Approval starts over."""
    claim = await _locked(session, user, claim_id)
    if claim.submitted_by != user.id:
        raise forbidden("Only the person who submitted a claim can resubmit it")
    if claim.status != "change_requested":
        raise conflict("not_change_requested", "Only claims sent back for changes can be resubmitted")

    if data.receipt_id:
        upload = await _claim_receipt(session, user, data.receipt_id)
        _copy_receipt(claim, upload)
        upload.expense_claim_id = claim.id
    for name in ("title", "description", "category", "amount", "expense_date", "vendor_name", "event_id"):
        setattr(claim, name, getattr(data, name))
    claim.status = "pending_approval"
    claim.approval_stage = "first"
    claim.first_approved_by = None
    claim.final_approver_id = None
    claim.rejection_reason = None
    claim.current_approver_id = await _first_approver(session, claim)
    _log(session, claim, user, "submitted", "Resubmitted after changes")
    await session.flush()
    return claim


# --- Approver actions --------------------------------------------------------------


async def approve_claim(
    session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID, comment: str | None = None
) -> ExpenseClaim:
    claim = await _locked(session, user, claim_id)
    if error := _why_cannot_decide(claim, user):
        raise error

    if claim.approval_stage == "first" and needs_final_approval(claim.amount):
        final_approver = await _finance_admin(session, claim.club_id, exclude={claim.submitted_by, user.id})
        if final_approver is None:
            raise conflict("no_final_approver", "Nobody else is available to give final approval")
        claim.first_approved_by = user.id
        claim.approval_stage = "final"
        claim.current_approver_id = final_approver
        _log(session, claim, user, "approved", comment or "First approval. Final approval required.")
        await session.flush()
        return claim

    claim.status = "approved"
    claim.final_approver_id = user.id
    claim.current_approver_id = None
    await ledger_service.record_expense(
        session,
        club_id=claim.club_id,
        amount=claim.amount,
        created_by=user.id,
        entry_type="reimbursement",
        source_module="expense_claim",
        reference_id=claim.id,
        reference_type="expense_claim",
        payee_id=claim.submitted_by,
        approved_by=user.id,
        event_id=claim.event_id,
        category=claim.category,
        description=f"Reimbursement: {claim.title}",
        receipt_url=claim.receipt_url,
        payment_status="approved",
        idempotency_key=_ledger_key(claim),
    )
    _log(session, claim, user, "approved", comment)
    await session.flush()
    return claim


async def reject_claim(session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID, reason: str) -> ExpenseClaim:
    claim = await _locked(session, user, claim_id)
    if error := _why_cannot_decide(claim, user):
        raise error
    claim.status = "rejected"
    claim.rejection_reason = reason
    claim.current_approver_id = None
    _log(session, claim, user, "rejected", reason)
    await session.flush()
    return claim


async def request_changes(session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID, comment: str) -> ExpenseClaim:
    claim = await _locked(session, user, claim_id)
    if error := _why_cannot_decide(claim, user):
        raise error
    claim.status = "change_requested"
    claim.current_approver_id = None
    _log(session, claim, user, "change_requested", comment)
    await session.flush()
    return claim


async def mark_paid(session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID) -> ExpenseClaim:
    """Record that the volunteer has been paid back. This is the moment the club balance drops."""
    claim = await _locked(session, user, claim_id)
    if user.role not in FINANCE_ADMINS:
        raise forbidden("Only the treasurer or president can mark a claim as paid")
    if user.id == claim.submitted_by:
        raise AppError(403, "self_payment", "You cannot pay out your own claim")
    if claim.status == "paid":
        raise conflict("already_paid", "This claim has already been paid")
    if claim.status != "approved":
        raise conflict("not_approved", "Only approved claims can be marked as paid")

    entry = await ledger_service.get_entry_for_reference(
        session, source_module="expense_claim", reference_id=claim.id, entry_type="reimbursement"
    )
    if entry is None:
        raise AppError(500, "ledger_entry_missing", "This approved claim has no ledger entry")
    await ledger_service.set_payment_status(session, entry, "paid", actor_id=user.id)
    claim.status = "paid"
    _log(session, claim, user, "paid")
    await session.flush()
    return claim


# --- Reading -----------------------------------------------------------------------


async def _can_view(session: AsyncSession, claim: ExpenseClaim, user: CurrentUser) -> bool:
    if user.id == claim.submitted_by or user.role in FINANCE_ADMINS:
        return True
    if user.role not in APPROVERS:
        return False
    if user.id in (claim.current_approver_id, claim.first_approved_by, claim.final_approver_id):
        return True
    if claim.event_id:
        event = await shared_directory.get_event(session, claim.club_id, claim.event_id)
        return bool(event and event.lead_id == user.id)
    return False


def _extras(claim: ExpenseClaim, user: CurrentUser, people: dict, events: dict) -> dict:
    def name(user_id):
        return people[user_id].full_name if user_id in people else None

    return {
        "submitted_by_name": name(claim.submitted_by),
        "current_approver_name": name(claim.current_approver_id),
        "event_name": events.get(claim.event_id),
        "needs_final_approval": needs_final_approval(claim.amount),
        "allowed_actions": allowed_actions(claim, user),
    }


class _ClaimView:
    """Adds the computed fields the response schema requires on top of an ORM claim."""

    def __init__(self, claim: ExpenseClaim, extras: dict):
        self._claim = claim
        self._extras = extras

    def __getattr__(self, item):
        if item in self._extras:
            return self._extras[item]
        return getattr(self._claim, item)


async def present(session: AsyncSession, user: CurrentUser, claims: list[ExpenseClaim]) -> list[ExpenseClaimOut]:
    """Turn claims into API objects, adding people's and events' names and the caller's allowed actions."""
    people = await shared_directory.get_users(
        session, [uid for claim in claims for uid in (claim.submitted_by, claim.current_approver_id)]
    )
    events = {event.id: event.name for event in await shared_directory.list_events(session, user.club_id)}
    return [
        ExpenseClaimOut.model_validate(_ClaimView(claim, _extras(claim, user, people, events)))
        for claim in claims
    ]


async def list_my_claims(session: AsyncSession, user: CurrentUser, status: str | None = None) -> list[ExpenseClaimOut]:
    stmt = select(ExpenseClaim).where(C.club_id == user.club_id, C.submitted_by == user.id)
    if status:
        stmt = stmt.where(C.status == status)
    claims = list(await session.scalars(stmt.order_by(C.created_at.desc())))
    return await present(session, user, claims)


async def get_queue(session: AsyncSession, user: CurrentUser) -> ApprovalQueue:
    pending = await session.scalars(
        select(ExpenseClaim)
        .where(C.club_id == user.club_id, C.status == "pending_approval")
        .order_by(C.created_at)
    )
    to_approve = [claim for claim in pending if _why_cannot_decide(claim, user) is None]

    to_pay: list[ExpenseClaim] = []
    if user.role in FINANCE_ADMINS:
        approved = await session.scalars(
            select(ExpenseClaim)
            .where(C.club_id == user.club_id, C.status == "approved", C.submitted_by != user.id)
            .order_by(C.updated_at)
        )
        to_pay = list(approved)
    return ApprovalQueue(
        to_approve=await present(session, user, to_approve), to_pay=await present(session, user, to_pay)
    )


async def get_claim_detail(session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID) -> ExpenseClaimDetail:
    claim = await session.scalar(select(ExpenseClaim).where(C.id == claim_id, C.club_id == user.club_id))
    if claim is None:
        raise not_found("Expense claim")
    if not await _can_view(session, claim, user):
        raise forbidden("You cannot view this claim")

    logs = list(
        await session.scalars(
            select(ApprovalLog).where(ApprovalLog.expense_claim_id == claim.id).order_by(ApprovalLog.created_at)
        )
    )
    people = await shared_directory.get_users(
        session, [claim.submitted_by, claim.current_approver_id, *(log.approver_id for log in logs)]
    )
    events = {event.id: event.name for event in await shared_directory.list_events(session, user.club_id)}
    entry = await ledger_service.get_entry_for_reference(
        session, source_module="expense_claim", reference_id=claim.id, entry_type="reimbursement"
    )
    history = [
        ApprovalLogOut.model_validate(log).model_copy(
            update={"approver_name": people[log.approver_id].full_name if log.approver_id in people else None}
        )
        for log in logs
    ]
    extras = _extras(claim, user, people, events) | {
        "approval_history": history,
        "ledger_entry_id": entry.id if entry else None,
        "ledger_payment_status": entry.payment_status if entry else None,
    }
    return ExpenseClaimDetail.model_validate(_ClaimView(claim, extras))

