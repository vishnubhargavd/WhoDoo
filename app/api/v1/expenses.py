import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db, require_approver, require_finance_admin
from app.core.errors import AppError, not_found
from app.models.expense import ReceiptUpload
from app.schemas.expense import (
    ApprovalQueue,
    ApproveBody,
    ClaimStatus,
    ExpenseClaimCreate,
    ExpenseClaimDetail,
    ExpenseClaimOut,
    ExpenseClaimResubmit,
    OcrResponse,
    RejectBody,
    RequestChangesBody,
)
from app.services import expense_service, storage_service

router = APIRouter(prefix="/expenses", tags=["Snap-to-Reimburse"])


@router.get("/receipts/{club_id}/{filename}")
async def download_receipt(
    club_id: uuid.UUID, filename: str,
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    if club_id != user.club_id:
        raise not_found("Receipt")
    key = f"{club_id}/{filename}"
    upload = await db.scalar(select(ReceiptUpload).where(
        ReceiptUpload.club_id == user.club_id,
        ReceiptUpload.receipt_url.in_((storage_service.RECEIPT_ROUTE + key, "/uploads/receipts/" + key)),
    ))
    if upload is None:
        raise not_found("Receipt")
    if upload.uploaded_by != user.id:
        if upload.expense_claim_id is None:
            raise not_found("Receipt")
        # Reuse the claim's existing approver, owner and club authorization.
        await expense_service.get_claim_detail(db, user, upload.expense_claim_id)
    content_type = upload.content_type
    await db.commit()  # Finish database work before remote storage I/O.
    data = await run_in_threadpool(storage_service.read_receipt, club_id, filename)
    return Response(data, media_type=content_type, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": f'inline; filename="{filename}"',
    })


@router.post("/ocr", response_model=OcrResponse)
async def read_receipt(
    file: UploadFile = File(..., description="Receipt as PNG, JPEG, WebP or PDF, up to 8 MB"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OcrResponse:
    """Upload a receipt and get back what OCR could read. No claim is created."""
    data = await file.read(storage_service.MAX_RECEIPT_BYTES + 1)
    if len(data) > storage_service.MAX_RECEIPT_BYTES:
        raise AppError(413, "file_too_large", "Receipts must be 8 MB or smaller")
    result = await expense_service.upload_receipt(db, user, data)
    await db.commit()
    return result


@router.post("", response_model=ExpenseClaimDetail, status_code=201)
async def submit_claim(
    body: ExpenseClaimCreate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ExpenseClaimDetail:
    claim = await expense_service.create_claim(db, user, body)
    await db.commit()
    return await expense_service.get_claim_detail(db, user, claim.id)


@router.get("", response_model=list[ExpenseClaimOut])
async def my_claims(
    status: ClaimStatus | None = Query(default=None),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ExpenseClaimOut]:
    return await expense_service.list_my_claims(db, user, status)


# Declared before /{expense_id} so "approvals" is not read as a claim id.
@router.get("/approvals", response_model=ApprovalQueue)
async def approval_queue(
    user: CurrentUser = Depends(require_approver), db: AsyncSession = Depends(get_db)
) -> ApprovalQueue:
    return await expense_service.get_queue(db, user)


@router.get("/{expense_id}", response_model=ExpenseClaimDetail)
async def get_claim(
    expense_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ExpenseClaimDetail:
    return await expense_service.get_claim_detail(db, user, expense_id)


@router.post("/{expense_id}/resubmit", response_model=ExpenseClaimDetail)
async def resubmit_claim(
    expense_id: uuid.UUID,
    body: ExpenseClaimResubmit,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ExpenseClaimDetail:
    await expense_service.resubmit_claim(db, user, expense_id, body)
    await db.commit()
    return await expense_service.get_claim_detail(db, user, expense_id)


@router.post("/{expense_id}/approve", response_model=ExpenseClaimDetail)
async def approve_claim(
    expense_id: uuid.UUID,
    body: ApproveBody | None = None,
    user: CurrentUser = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
) -> ExpenseClaimDetail:
    await expense_service.approve_claim(db, user, expense_id, body.comment if body else None)
    await db.commit()
    return await expense_service.get_claim_detail(db, user, expense_id)


@router.post("/{expense_id}/reject", response_model=ExpenseClaimDetail)
async def reject_claim(
    expense_id: uuid.UUID,
    body: RejectBody,
    user: CurrentUser = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
) -> ExpenseClaimDetail:
    await expense_service.reject_claim(db, user, expense_id, body.reason)
    await db.commit()
    return await expense_service.get_claim_detail(db, user, expense_id)


@router.post("/{expense_id}/request-changes", response_model=ExpenseClaimDetail)
async def request_changes(
    expense_id: uuid.UUID,
    body: RequestChangesBody,
    user: CurrentUser = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
) -> ExpenseClaimDetail:
    await expense_service.request_changes(db, user, expense_id, body.comment)
    await db.commit()
    return await expense_service.get_claim_detail(db, user, expense_id)


@router.post("/{expense_id}/mark-paid", response_model=ExpenseClaimDetail)
async def mark_paid(
    expense_id: uuid.UUID,
    user: CurrentUser = Depends(require_finance_admin),
    db: AsyncSession = Depends(get_db),
) -> ExpenseClaimDetail:
    await expense_service.mark_paid(db, user, expense_id)
    await db.commit()
    return await expense_service.get_claim_detail(db, user, expense_id)
