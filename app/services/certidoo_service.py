"""CertiDoo service: verifiable digital certificates backed by real attendance and contributions."""

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, bad_request, conflict, not_found
from app.models.certidoo import Certificate
from app.models.crewpulse import CrewTask
from app.models.shared import Club, Event, User
from app.schemas.certidoo import (
    CertificateIssueRequest,
    CertificateOut,
    CertificateVerificationOut,
    EligibleRecipient,
)


def _generate_verification_code() -> str:
    year = datetime.now(timezone.utc).year
    suffix = secrets.token_hex(4).upper()
    return f"CERT-{year}-{suffix}"


async def get_event_with_club_check(db: AsyncSession, club_id: uuid.UUID, event_id: uuid.UUID) -> Event:
    event = await db.get(Event, event_id)
    if not event or event.club_id != club_id:
        raise not_found("Event")
    return event


async def list_eligible_recipients(
    db: AsyncSession, club_id: uuid.UUID, event_id: uuid.UUID
) -> list[EligibleRecipient]:
    """Find genuine attendees and completed volunteers for a completed event."""
    await get_event_with_club_check(db, club_id, event_id)

    # Fetch existing certificates for this event
    existing_certs_res = await db.execute(
        select(Certificate).where(
            Certificate.event_id == event_id,
            Certificate.club_id == club_id,
            Certificate.revoked_at.is_(None),
        )
    )
    existing_map = {
        (c.recipient_user_id, c.certificate_type): (c.id, c.verification_code)
        for c in existing_certs_res.scalars().all()
    }

    results: list[EligibleRecipient] = []
    seen: set[tuple[uuid.UUID, str]] = set()

    # 1. Genuine attendees from GateSync
    attendee_query = text("""
        SELECT u.id as user_id, u.full_name, u.email, ga.checked_in_at
        FROM gatesync_attendance ga
        JOIN event_tickets et ON et.id = ga.ticket_id
        JOIN users u ON u.id = et.holder_user_id
        WHERE ga.event_id = :event_id AND ga.club_id = :club_id
    """)
    attendee_rows = (await db.execute(attendee_query, {"event_id": event_id, "club_id": club_id})).fetchall()
    for row in attendee_rows:
        key = (row.user_id, "participation")
        if key not in seen:
            seen.add(key)
            existing = existing_map.get(key)
            results.append(
                EligibleRecipient(
                    user_id=row.user_id,
                    full_name=row.full_name or "Participant",
                    email=row.email or "",
                    eligibility_type="participation",
                    detail=f"Verified check-in at {row.checked_in_at.strftime('%Y-%m-%d %H:%M')}",
                    already_issued=existing is not None,
                    certificate_id=existing[0] if existing else None,
                    verification_code=existing[1] if existing else None,
                )
            )

    # 2. Genuine completed volunteers from CrewPulse
    volunteer_query = (
        select(User.id, User.full_name, User.email, func.count(CrewTask.id).label("tasks_done"))
        .join(CrewTask, CrewTask.assignee_id == User.id)
        .where(
            CrewTask.event_id == event_id,
            CrewTask.club_id == club_id,
            CrewTask.status == "done",
        )
        .group_by(User.id, User.full_name, User.email)
    )
    vol_rows = (await db.execute(volunteer_query)).fetchall()
    for row in vol_rows:
        key = (row.id, "volunteer")
        if key not in seen:
            seen.add(key)
            existing = existing_map.get(key)
            results.append(
                EligibleRecipient(
                    user_id=row.id,
                    full_name=row.full_name or "Volunteer",
                    email=row.email or "",
                    eligibility_type="volunteer",
                    detail=f"Completed {row.tasks_done} CrewPulse volunteer task(s)",
                    already_issued=existing is not None,
                    certificate_id=existing[0] if existing else None,
                    verification_code=existing[1] if existing else None,
                )
            )

    return results


async def is_eligible(
    db: AsyncSession, club_id: uuid.UUID, event_id: uuid.UUID, recipient_id: uuid.UUID, cert_type: str
) -> bool:
    """Verify eligibility against server-owned records."""
    if cert_type == "participation":
        # Must have checked in via GateSync
        check = await db.execute(
            text("""
                SELECT 1 FROM gatesync_attendance ga
                JOIN event_tickets et ON et.id = ga.ticket_id
                WHERE ga.event_id = :event_id AND ga.club_id = :club_id AND et.holder_user_id = :recipient_id
                LIMIT 1
            """),
            {"event_id": event_id, "club_id": club_id, "recipient_id": recipient_id},
        )
        return check.scalar() is not None

    if cert_type == "volunteer":
        # Must have at least 1 completed task in CrewPulse
        check = await db.scalar(
            select(func.count(CrewTask.id)).where(
                CrewTask.event_id == event_id,
                CrewTask.club_id == club_id,
                CrewTask.assignee_id == recipient_id,
                CrewTask.status == "done",
            )
        )
        return bool(check and check > 0)

    if cert_type == "achievement":
        # Recipient must exist in the club
        user = await db.get(User, recipient_id)
        return user is not None and user.club_id == club_id

    return False


async def issue_certificate(
    db: AsyncSession,
    club_id: uuid.UUID,
    issuer_id: uuid.UUID,
    req: CertificateIssueRequest,
) -> Certificate:
    """Issue a branded verifiable certificate with duplicate and unauthorized prevention."""
    event = await get_event_with_club_check(db, club_id, req.event_id)

    recipient = await db.get(User, req.recipient_user_id)
    if not recipient:
        raise not_found("Recipient user")

    # Verify real eligibility
    eligible = await is_eligible(db, club_id, req.event_id, req.recipient_user_id, req.certificate_type)
    if not eligible:
        raise bad_request(
            "ineligible_recipient",
            f"User {recipient.full_name} is not eligible for a {req.certificate_type} certificate for this event. "
            "Participation requires verified attendance check-in, and volunteer status requires completed tasks.",
        )

    # Check duplicate issuance
    existing = await db.scalar(
        select(Certificate).where(
            Certificate.event_id == req.event_id,
            Certificate.recipient_user_id == req.recipient_user_id,
            Certificate.certificate_type == req.certificate_type,
            Certificate.revoked_at.is_(None),
        )
    )
    if existing:
        raise conflict(
            "duplicate_certificate",
            f"A {req.certificate_type} certificate has already been issued to this recipient for this event.",
        )

    # Unique verification code
    code = _generate_verification_code()
    # Collision retry
    for _ in range(5):
        collision = await db.scalar(select(Certificate).where(Certificate.verification_code == code))
        if not collision:
            break
        code = _generate_verification_code()

    meta = {
        **req.metadata,
        "event_name": event.name,
        "recipient_name": recipient.full_name,
        "issued_by_user_id": str(issuer_id),
    }

    cert = Certificate(
        id=uuid.uuid4(),
        club_id=club_id,
        event_id=req.event_id,
        recipient_user_id=req.recipient_user_id,
        certificate_type=req.certificate_type,
        verification_code=code,
        title=req.title,
        issued_by=issuer_id,
        issued_at=datetime.now(timezone.utc),
        metadata_json=meta,
    )
    db.add(cert)
    await db.commit()
    await db.refresh(cert)
    return cert


async def get_certificate_by_id(db: AsyncSession, cert_id: uuid.UUID) -> Certificate:
    cert = await db.get(Certificate, cert_id)
    if not cert:
        raise not_found("Certificate")
    return cert


async def list_user_certificates(db: AsyncSession, user_id: uuid.UUID) -> list[CertificateOut]:
    """Recipients view their own earned certificates."""
    query = (
        select(Certificate, Event.name.label("event_name"), User.full_name.label("recipient_name"), User.email.label("recipient_email"))
        .join(Event, Event.id == Certificate.event_id)
        .join(User, User.id == Certificate.recipient_user_id)
        .where(
            Certificate.recipient_user_id == user_id,
            Certificate.revoked_at.is_(None),
        )
        .order_by(Certificate.issued_at.desc())
    )
    rows = (await db.execute(query)).all()
    results = []
    for cert, event_name, rec_name, rec_email in rows:
        out = CertificateOut(
            id=cert.id,
            club_id=cert.club_id,
            event_id=cert.event_id,
            recipient_user_id=cert.recipient_user_id,
            recipient_name=rec_name,
            recipient_email=rec_email,
            event_name=event_name,
            certificate_type=cert.certificate_type,
            verification_code=cert.verification_code,
            title=cert.title,
            issued_by=cert.issued_by,
            issued_at=cert.issued_at,
            revoked_at=cert.revoked_at,
            metadata_json=cert.metadata_json,
        )
        results.append(out)
    return results


async def list_event_certificates(
    db: AsyncSession, club_id: uuid.UUID, event_id: uuid.UUID
) -> list[CertificateOut]:
    """Officers inspect all certificates issued for an event."""
    await get_event_with_club_check(db, club_id, event_id)
    query = (
        select(Certificate, Event.name.label("event_name"), User.full_name.label("recipient_name"), User.email.label("recipient_email"))
        .join(Event, Event.id == Certificate.event_id)
        .join(User, User.id == Certificate.recipient_user_id)
        .where(
            Certificate.club_id == club_id,
            Certificate.event_id == event_id,
        )
        .order_by(Certificate.issued_at.desc())
    )
    rows = (await db.execute(query)).all()
    results = []
    for cert, event_name, rec_name, rec_email in rows:
        results.append(
            CertificateOut(
                id=cert.id,
                club_id=cert.club_id,
                event_id=cert.event_id,
                recipient_user_id=cert.recipient_user_id,
                recipient_name=rec_name,
                recipient_email=rec_email,
                event_name=event_name,
                certificate_type=cert.certificate_type,
                verification_code=cert.verification_code,
                title=cert.title,
                issued_by=cert.issued_by,
                issued_at=cert.issued_at,
                revoked_at=cert.revoked_at,
                metadata_json=cert.metadata_json,
            )
        )
    return results


async def list_club_certificates(
    db: AsyncSession, club_id: uuid.UUID
) -> list[CertificateOut]:
    """Officers inspect all certificates issued across the club."""
    query = (
        select(Certificate, Event.name.label("event_name"), User.full_name.label("recipient_name"), User.email.label("recipient_email"))
        .join(Event, Event.id == Certificate.event_id)
        .join(User, User.id == Certificate.recipient_user_id)
        .where(
            Certificate.club_id == club_id,
        )
        .order_by(Certificate.issued_at.desc())
    )
    rows = (await db.execute(query)).all()
    results = []
    for cert, event_name, rec_name, rec_email in rows:
        results.append(
            CertificateOut(
                id=cert.id,
                club_id=cert.club_id,
                event_id=cert.event_id,
                recipient_user_id=cert.recipient_user_id,
                recipient_name=rec_name,
                recipient_email=rec_email,
                event_name=event_name,
                certificate_type=cert.certificate_type,
                verification_code=cert.verification_code,
                title=cert.title,
                issued_by=cert.issued_by,
                issued_at=cert.issued_at,
                revoked_at=cert.revoked_at,
                metadata_json=cert.metadata_json,
            )
        )
    return results


async def verify_certificate_by_code(
    db: AsyncSession, verification_code: str
) -> CertificateVerificationOut:
    """Public verification against server-owned records."""
    query = (
        select(
            Certificate,
            User.full_name.label("recipient_name"),
            Event.name.label("event_name"),
            Club.name.label("club_name"),
        )
        .join(User, User.id == Certificate.recipient_user_id)
        .join(Event, Event.id == Certificate.event_id)
        .join(Club, Club.id == Certificate.club_id)
        .where(Certificate.verification_code == verification_code)
    )
    row = (await db.execute(query)).first()
    if not row:
        raise not_found("Certificate with this verification code was not found")

    cert, recipient_name, event_name, club_name = row
    status = "revoked" if cert.revoked_at else "valid"

    return CertificateVerificationOut(
        valid=cert.revoked_at is None,
        verification_code=cert.verification_code,
        title=cert.title,
        certificate_type=cert.certificate_type,
        recipient_name=recipient_name or "Recipient",
        event_name=event_name or "Event",
        club_name=club_name or "Club",
        issued_at=cert.issued_at,
        status=status,
    )
