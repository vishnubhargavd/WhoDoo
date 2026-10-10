"""CertiDoo API: verifiable certificate issuance, recipient download, and public verification."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db, require_roles
from app.core.errors import forbidden
from app.schemas.certidoo import (
    CertificateIssueRequest,
    CertificateOut,
    CertificateVerificationOut,
    EligibleRecipient,
)
from app.services import certidoo_service

router = APIRouter(prefix="/certificates", tags=["CertiDoo"])


@router.get("/verify/{verification_code}", response_model=CertificateVerificationOut)
async def verify_certificate(
    verification_code: str,
    db: AsyncSession = Depends(get_db),
):
    """Public verification of a certificate by its unique persistent reference."""
    return await certidoo_service.verify_certificate_by_code(db, verification_code)


@router.get("/my", response_model=list[CertificateOut])
async def list_my_certificates(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all verifiable certificates awarded to the signed-in user."""
    return await certidoo_service.list_user_certificates(db, user.id)


@router.get("/events/{event_id}/eligible", response_model=list[EligibleRecipient])
async def list_eligible_recipients(
    event_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Club officers inspect attendees and volunteers genuinely eligible for certificates."""
    return await certidoo_service.list_eligible_recipients(db, user.club_id, event_id)


@router.get("/events/{event_id}", response_model=list[CertificateOut])
async def list_event_certificates(
    event_id: uuid.UUID,
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Club officers review all certificates issued for a club event."""
    return await certidoo_service.list_event_certificates(db, user.club_id, event_id)


@router.post("/issue", response_model=CertificateOut, status_code=201)
async def issue_certificate(
    req: CertificateIssueRequest,
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    """Club officers issue a verified participation, volunteer, or achievement certificate."""
    cert = await certidoo_service.issue_certificate(db, user.club_id, user.id, req)
    return CertificateOut(
        id=cert.id,
        club_id=cert.club_id,
        event_id=cert.event_id,
        recipient_user_id=cert.recipient_user_id,
        certificate_type=cert.certificate_type,
        verification_code=cert.verification_code,
        title=cert.title,
        issued_by=cert.issued_by,
        issued_at=cert.issued_at,
        revoked_at=cert.revoked_at,
        metadata_json=cert.metadata_json,
    )


@router.get("/{cert_id}", response_model=CertificateOut)
async def get_certificate(
    cert_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch certificate details for display or download."""
    cert = await certidoo_service.get_certificate_by_id(db, cert_id)
    # Access control: must be recipient or officer of the same club
    is_recipient = cert.recipient_user_id == user.id
    is_officer = cert.club_id == user.club_id and user.role in ("president", "treasurer", "event_lead")
    if not (is_recipient or is_officer):
        raise forbidden("You do not have permission to view this certificate.")

    return CertificateOut(
        id=cert.id,
        club_id=cert.club_id,
        event_id=cert.event_id,
        recipient_user_id=cert.recipient_user_id,
        certificate_type=cert.certificate_type,
        verification_code=cert.verification_code,
        title=cert.title,
        issued_by=cert.issued_by,
        issued_at=cert.issued_at,
        revoked_at=cert.revoked_at,
        metadata_json=cert.metadata_json,
    )
