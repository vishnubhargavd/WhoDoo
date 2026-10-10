"""Snap-to-Reimburse: expense claims and their approval workflow.

Approval rules (limit = EXPENSE_AUTO_APPROVAL_LIMIT, default ₹1,000):

* Below the limit, one approval is enough: the event lead's, or a treasurer's or
  president's standing in for them.
* At or above the limit, two DIFFERENT people must approve. The first approval comes
  from the event lead (or a treasurer or president standing in). The final approval
  comes from a treasurer or president who did not give the first one. A treasurer who
  approves first has not also given the final approval.
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
    OcrLineItem,
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

    parsed = result.parsed
    found = [name for name, value in
             (("merchant", result.vendor), ("total", result.amount), ("date", result.expense_date)) if value]
    if not result.available:
        message = "We could not read this receipt automatically. Please enter the details yourself."
    elif len(found) == 3:
        message = "Receipt read. Check every detail against the receipt before submitting."
    else:
        message = f"Receipt partly read ({', '.join(found) or 'nothing'} found). Please fill in the rest."
    return OcrResponse(
        receipt_id=upload.id,
        receipt_url=receipt_url,
        ocr_available=result.available,
        ocr_confidence=float(result.confidence),
        extracted_vendor=result.vendor,
        extracted_amount=result.amount,
        extracted_date=result.expense_date,
        amount_status="read_from_total_line" if result.amount is not None else "not_found",
        suggested_amount=parsed.suggested_total,
        line_items=[
            OcrLineItem(description=item.description, quantity=item.quantity, unit_price=item.unit_price,
                        amount=item.amount)
            for item in parsed.items
        ],
        items_total=parsed.items_total,
        subtotal=parsed.subtotal,
        tax_total=parsed.tax_total,
        discount_total=parsed.discount_total,
        warnings=parsed.warnings,
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


def _why_cannot_decide(claim: ExpenseClaim, user: CurrentUser, *, approving: bool = True) -> AppError | None:
    """Return the reason this user may not act on the claim, or None if they may.

    `approving=False` is for rejecting or sending back: the same people may do it, but the
    two-person rule does not apply (the first approver can still stop a claim they approved).
    Everything is read from the locked database row, never from the request.
    """
    if claim.status != "pending_approval":
        return conflict("not_pending", f"This claim is already {claim.status.replace('_', ' ')}")
    if user.id == claim.submitted_by:
        return AppError(403, "self_approval", "You cannot decide on your own claim")
    is_admin = user.role in FINANCE_ADMINS

    if claim.approval_stage == "first":
        if is_admin or user.id == claim.current_approver_id:
            return None
        return forbidden("This claim is waiting for a different approver")

    # Final stage: a treasurer or president, and not the person who gave the first approval.
    if approving and user.id == claim.first_approved_by:
        return conflict(
            "duplicate_approval",
            "You gave the first approval on this claim. A different treasurer or president must give the final one",
        )
    if not is_admin:
        return forbidden("Final approval needs the treasurer or president")
    return None


def allowed_actions(claim: ExpenseClaim, user: CurrentUser) -> list[str]:
    actions: list[str] = []
    if _why_cannot_decide(claim, user) is None:
        actions.append("approve")
    if _why_cannot_decide(claim, user, approving=False) is None:
        actions += ["reject", "request_changes"]
    if claim.status == "approved" and user.role in FINANCE_ADMINS and user.id != claim.submitted_by:
        actions.append("mark_paid")
    if claim.status == "change_requested" and user.id == claim.submitted_by:
        actions.append("resubmit")
    return actions


def approval_blocked_reason(claim: ExpenseClaim, user: CurrentUser) -> str | None:
    """Why an approver cannot approve a claim that is still pending, in words for the page. None if they can."""
    if claim.status != "pending_approval" or user.role not in APPROVERS:
        return None
    error = _why_cannot_decide(claim, user)
    return error.message if error else None


def status_label(claim: ExpenseClaim, waiting_on: str | None, first_approver: str | None = None) -> str:
    """Say where a claim stands in plain words, so a first approval never looks like "nothing happened"."""
    who = waiting_on or "the treasurer"
    if claim.status == "pending_approval":
        if claim.approval_stage == "final":
            return f"First approval by {first_approver or 'an approver'} · awaiting final approval from {who}"
        if needs_final_approval(claim.amount):
            return f"Awaiting first approval from {who}"
        return f"Awaiting approval from {who}"
    return {
        "approved": "Approved · awaiting payment",
        "paid": "Paid",
        "rejected": "Rejected",
        "change_requested": "Changes requested · waiting for the submitter to edit and resubmit",
        "draft": "Draft",
    }[claim.status]


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

    # At or above the limit the first approval, whoever gives it, only moves the claim to the
    # final stage. This holds for a treasurer or president too: approving first is not approving twice.
    if claim.approval_stage == "first" and needs_final_approval(claim.amount):
        final_approver = await _finance_admin(session, claim.club_id, exclude={claim.submitted_by, user.id})
        if final_approver is None:
            raise conflict("no_final_approver", "Nobody else is available to give the final approval")
        claim.first_approved_by = user.id
        claim.approval_stage = "final"
        claim.current_approver_id = final_approver
        _log(session, claim, user, "approved", comment or "First approval. Final approval required.")
        await session.flush()
        return claim

    # Last line of defence before money is committed: a claim at the final stage must have a
    # recorded first approver who is someone else. _why_cannot_decide already enforces this.
    if claim.approval_stage == "final" and claim.first_approved_by in (None, user.id):
        raise conflict("duplicate_approval", "Final approval must come from a different person than the first")

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
    if error := _why_cannot_decide(claim, user, approving=False):
        raise error
    claim.status = "rejected"
    claim.rejection_reason = reason
    claim.current_approver_id = None
    _log(session, claim, user, "rejected", reason)
    await session.flush()
    return claim


async def request_changes(session: AsyncSession, user: CurrentUser, claim_id: uuid.UUID, comment: str) -> ExpenseClaim:
    claim = await _locked(session, user, claim_id)
    if error := _why_cannot_decide(claim, user, approving=False):
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


async def _change_requests(session: AsyncSession, claims: list[ExpenseClaim]) -> dict[uuid.UUID, ApprovalLog]:
    """The latest "changes requested" entry for each claim that is waiting on its submitter."""
    waiting = [claim.id for claim in claims if claim.status == "change_requested"]
    if not waiting:
        return {}
    logs = await session.scalars(
        select(ApprovalLog)
        .where(ApprovalLog.expense_claim_id.in_(waiting), ApprovalLog.action == "change_requested")
        .order_by(ApprovalLog.created_at)
    )
    return {log.expense_claim_id: log for log in logs}  # later entries overwrite earlier ones


def _extras(claim: ExpenseClaim, user: CurrentUser, people: dict, events: dict, request: ApprovalLog | None = None) -> dict:
    def name(user_id):
        return people[user_id].full_name if user_id in people else None

    return {
        "reviewer_note": request.comment if request else None,
        "reviewer_name": name(request.approver_id) if request else None,
        "submitted_by_name": name(claim.submitted_by),
        "current_approver_name": name(claim.current_approver_id),
        "event_name": events.get(claim.event_id),
        "needs_final_approval": needs_final_approval(claim.amount),
        "allowed_actions": allowed_actions(claim, user),
        "status_label": status_label(claim, name(claim.current_approver_id), name(claim.first_approved_by)),
        "first_approved_by_name": name(claim.first_approved_by),
        "final_approver_name": name(claim.final_approver_id),
        "approval_blocked_reason": approval_blocked_reason(claim, user),
        "amount_edited": claim.extracted_amount is not None and claim.extracted_amount != claim.amount,
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
    requests = await _change_requests(session, claims)
    people = await shared_directory.get_users(
        session,
        [uid for claim in claims
         for uid in (claim.submitted_by, claim.current_approver_id, claim.first_approved_by, claim.final_approver_id)]
        + [log.approver_id for log in requests.values()],
    )
    events = {event.id: event.name for event in await shared_directory.list_events(session, user.club_id)}
    return [
        ExpenseClaimOut.model_validate(_ClaimView(claim, _extras(claim, user, people, events, requests.get(claim.id))))
        for claim in claims
    ]


async def list_my_claims(session: AsyncSession, user: CurrentUser, status: str | None = None) -> list[ExpenseClaimOut]:
    stmt = select(ExpenseClaim).where(C.club_id == user.club_id, C.submitted_by == user.id)
    if status:
        stmt = stmt.where(C.status == status)
    claims = list(await session.scalars(stmt.order_by(C.created_at.desc())))
    return await present(session, user, claims)


async def get_queue(session: AsyncSession, user: CurrentUser) -> ApprovalQueue:
    pending = list(await session.scalars(
        select(ExpenseClaim)
        .where(C.club_id == user.club_id, C.status == "pending_approval")
        .order_by(C.created_at)
    ))
    to_approve = [claim for claim in pending if _why_cannot_decide(claim, user) is None]
    awaiting_others = [
        claim for claim in pending if claim.approval_stage == "final" and claim.first_approved_by == user.id
    ]

    to_pay: list[ExpenseClaim] = []
    if user.role in FINANCE_ADMINS:
        approved = await session.scalars(
            select(ExpenseClaim)
            .where(C.club_id == user.club_id, C.status == "approved", C.submitted_by != user.id)
            .order_by(C.updated_at)
        )
        to_pay = list(approved)
    return ApprovalQueue(
        to_approve=await present(session, user, to_approve),
        awaiting_others=await present(session, user, awaiting_others),
        to_pay=await present(session, user, to_pay),
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
        session,
        [claim.submitted_by, claim.current_approver_id, claim.first_approved_by, claim.final_approver_id,
         *(log.approver_id for log in logs)],
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
    request = next((log for log in reversed(logs) if log.action == "change_requested"), None)
    extras = _extras(claim, user, people, events, request if claim.status == "change_requested" else None) | {
        "approval_history": history,
        "ledger_entry_id": entry.id if entry else None,
        "ledger_payment_status": entry.payment_status if entry else None,
    }
    return ExpenseClaimDetail.model_validate(_ClaimView(claim, extras))

