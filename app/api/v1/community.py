"""GateSync check-in, CrewDoo Connect, CertiDoo and LegacyVault.

Each group answers 404 until its tables exist (services/modules.py). Who may do what is
decided in the services from the signed-in user's row.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.community import (
    AnnouncementIn,
    AnnouncementOut,
    BulkCertificatesIn,
    BulkCertificatesOut,
    CertificateIn,
    CertificateOut,
    HandoverNoteIn,
    HandoverNoteOut,
    HandoverNoteUpdate,
    ManualCheckIn,
    RevokeIn,
    ScanIn,
)
from app.core.errors import AppError, conflict
from app.services import (
    announcement_service,
    certificate_service,
    email_service,
    gatesync_service,
    handover_service,
    modules,
)

User = Depends(get_current_user)
Db = Depends(get_db)

# --- GateSync ---------------------------------------------------------------------------

checkin = APIRouter(prefix="/check-in", tags=["GateSync"], dependencies=[Depends(modules.require("checkin"))])


@checkin.post("/scan")
async def scan(body: ScanIn, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    """Check a scanned QR code in. Always 200 with a `status`: redeemed, already_checked_in,
    wrong_event, revoked, expired, unrecognized or invalid_credential."""
    result = await gatesync_service.redeem(db, user, body.event_id, body.credential)
    await db.commit()
    return result


@checkin.post("/manual")
async def manual(body: ManualCheckIn, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    """Check in from the guest list. Same rules and answers as a scan."""
    result = await gatesync_service.admit_from_list(db, user, body.event_id, body.ticket_id, body.registration_id)
    await db.commit()
    return result


@checkin.get("/events/{event_id}")
async def attendance(event_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    """Totals and the guest list, for the door."""
    found = await gatesync_service.attendance(db, user, event_id)
    return {"event_id": event_id, "event_name": found["event"].name, "total": found["total"],
            "checked_in": found["checked_in"], "guests": found["guests"]}


# --- CrewDoo Connect --------------------------------------------------------------------

announcements = APIRouter(
    prefix="/announcements", tags=["CrewDoo Connect"], dependencies=[Depends(modules.require("announcements"))]
)


@announcements.get("", response_model=list[AnnouncementOut])
async def feed(user: CurrentUser = User, db: AsyncSession = Db) -> list[AnnouncementOut]:
    return await announcement_service.feed(db, user)


@announcements.post("", status_code=201)
async def post(body: AnnouncementIn, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    """Event leads, the treasurer and the president."""
    created = await announcement_service.post(db, user, body)
    await db.commit()
    return {"id": created}


@announcements.post("/read")
async def mark_all_read(user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    marked = await announcement_service.mark_read(db, user)
    await db.commit()
    return {"marked": marked}


@announcements.post("/{announcement_id}/read")
async def mark_read(announcement_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    marked = await announcement_service.mark_read(db, user, announcement_id)
    await db.commit()
    return {"marked": marked}


@announcements.post("/{announcement_id}/remove")
async def remove(announcement_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    """The author or the president."""
    await announcement_service.remove(db, user, announcement_id)
    await db.commit()
    return {"removed": True}


# --- CertiDoo ---------------------------------------------------------------------------

certificates = APIRouter(
    prefix="/certificates", tags=["CertiDoo"], dependencies=[Depends(modules.require("certificates"))]
)


@certificates.get("", response_model=list[CertificateOut])
async def my_certificates(user: CurrentUser = User, db: AsyncSession = Db) -> list[CertificateOut]:
    return await certificate_service.mine(db, user)


@certificates.get("/issued", response_model=list[CertificateOut])
async def issued(user: CurrentUser = User, db: AsyncSession = Db) -> list[CertificateOut]:
    return await certificate_service.issued_in_club(db, user)


@certificates.post("", response_model=CertificateOut, status_code=201)
async def issue(body: CertificateIn, background: BackgroundTasks, user: CurrentUser = User, db: AsyncSession = Db) -> CertificateOut:
    """The treasurer or president; an event lead for an event they lead. The recipient is emailed."""
    certificate = await certificate_service.issue(db, user, body)
    await db.commit()
    background.add_task(certificate_service.send_email, certificate.id)
    return certificate


@certificates.post("/for-event", response_model=BulkCertificatesOut)
async def issue_for_event(
    body: BulkCertificatesIn, background: BackgroundTasks, user: CurrentUser = User, db: AsyncSession = Db
) -> BulkCertificatesOut:
    """Participation for everyone checked in, or volunteering for everyone who finished a task.
    Each new recipient is emailed."""
    result = await certificate_service.issue_for_event(db, user, body.event_id, body.kind)
    await db.commit()
    background.add_task(certificate_service.send_emails, result.issued_ids)
    return result


@certificates.post("/{certificate_id}/email")
async def email_certificate(certificate_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> dict:
    """Send the certificate email again, to its recipient only. Says what actually happened."""
    found = await certificate_service.get(db, user, certificate_id)        # the recipient, or someone who can issue
    if found.revoked_at is not None:
        raise conflict("revoked", "A revoked certificate is not emailed.")
    if not email_service.configured():
        raise conflict("email_not_configured", "Email sending is not set up on this server, so nothing was sent.")
    if await certificate_service.send_email(certificate_id, db) != "sent":
        raise AppError(502, "email_failed", "The email could not be sent right now. Try again later.")
    return {"status": "sent", "status_label": "sent", "title": "Certificate email"}


@certificates.post("/{certificate_id}/revoke", response_model=CertificateOut)
async def revoke(certificate_id: uuid.UUID, body: RevokeIn, user: CurrentUser = User, db: AsyncSession = Db) -> CertificateOut:
    certificate = await certificate_service.revoke(db, user, certificate_id, body.reason)
    await db.commit()
    return certificate


# No sign-in: this is the check a third party runs from the code printed on a certificate.
public = APIRouter(tags=["CertiDoo"], dependencies=[Depends(modules.require("certificates"))])


@public.get("/certificates/verify/{code}")
async def verify(code: str, db: AsyncSession = Db) -> dict:
    found = await certificate_service.verify(db, code)
    if found is None:
        return {"valid": False, "status": "not_found"}
    return {
        "valid": found.revoked_at is None, "status": "revoked" if found.revoked_at else "valid",
        "recipient_name": found.recipient_name, "title": found.title, "kind": found.kind,
        "club_name": found.club_name, "event_name": found.event_name, "issued_at": found.issued_at,
    }


# --- LegacyVault ------------------------------------------------------------------------

handover = APIRouter(prefix="/handover", tags=["LegacyVault"], dependencies=[Depends(modules.require("handover"))])


@handover.get("", response_model=list[HandoverNoteOut])
async def list_notes(include_archived: bool = False, user: CurrentUser = User, db: AsyncSession = Db) -> list[HandoverNoteOut]:
    return await handover_service.list_notes(db, user, include_archived)


@handover.post("", response_model=HandoverNoteOut, status_code=201)
async def create_note(body: HandoverNoteIn, user: CurrentUser = User, db: AsyncSession = Db) -> HandoverNoteOut:
    note = await handover_service.create(db, user, body)
    await db.commit()
    return note


@handover.put("/{note_id}", response_model=HandoverNoteOut)
async def update_note(note_id: uuid.UUID, body: HandoverNoteUpdate, user: CurrentUser = User, db: AsyncSession = Db) -> HandoverNoteOut:
    note = await handover_service.update(db, user, note_id, body)
    await db.commit()
    return note


@handover.post("/{note_id}/archive", response_model=HandoverNoteOut)
async def archive_note(note_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> HandoverNoteOut:
    note = await handover_service.set_archived(db, user, note_id, True)
    await db.commit()
    return note


@handover.post("/{note_id}/restore", response_model=HandoverNoteOut)
async def restore_note(note_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> HandoverNoteOut:
    note = await handover_service.set_archived(db, user, note_id, False)
    await db.commit()
    return note


@handover.get("/{note_id}/versions")
async def note_versions(note_id: uuid.UUID, user: CurrentUser = User, db: AsyncSession = Db) -> list[dict]:
    return await handover_service.versions(db, user, note_id)
