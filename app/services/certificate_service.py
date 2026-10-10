"""CertiDoo: certificates that can be checked by anyone holding the code.

A certificate records the recipient's name as printed, what it is for and who issued it.
The public check (`verify`) needs no sign-in and reveals only what is printed on the
certificate. Certificates are never deleted: a mistake is revoked, with a reason, and the
public check then says so.

Bulk issuing reads other modules' records rather than taking a list from the browser:
participation goes to people GateSync checked in, volunteering to people who finished a
CrewPulse task on the event.
"""

import base64
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import ROLE_EVENT_LEAD, CurrentUser
from app.core.errors import conflict, forbidden, not_found
from app.models.community import Certificate
from app.models.crewpulse import CrewTask
from app.models.fairseat import EventTicket
from app.models.shared import Club, Event, User
from app.schemas.community import BulkCertificatesOut, CertificateIn, CertificateOut
from app.services import audit_service, modules


def _code() -> str:
    return base64.b32encode(secrets.token_bytes(10)).decode()


def can_issue(user: CurrentUser, event: Event | None = None) -> bool:
    """The treasurer and president for anything; an event lead for an event they lead."""
    if user.is_finance_admin:
        return True
    return user.role == ROLE_EVENT_LEAD and event is not None and event.lead_id == user.id


async def _event(session: AsyncSession, user: CurrentUser, event_id: uuid.UUID | None) -> Event | None:
    if event_id is None:
        return None
    event = await session.get(Event, event_id)
    if event is None or event.club_id != user.club_id:
        raise not_found("Event")
    return event


async def _present(session: AsyncSession, certificates: list[Certificate]) -> list[CertificateOut]:
    if not certificates:
        return []
    events = dict((await session.execute(
        select(Event.id, Event.name).where(Event.id.in_({c.event_id for c in certificates if c.event_id}))
    )).all())
    issuers = dict((await session.execute(
        select(User.id, User.full_name).where(User.id.in_({c.issued_by for c in certificates}))
    )).all())
    clubs = dict((await session.execute(
        select(Club.id, Club.name).where(Club.id.in_({c.club_id for c in certificates}))
    )).all())
    out = []
    for certificate in certificates:
        item = CertificateOut.model_validate(certificate)
        item.event_name = events.get(certificate.event_id)
        item.issued_by_name = issuers.get(certificate.issued_by, "")
        item.club_name = clubs.get(certificate.club_id, "")
        out.append(item)
    return out


async def _insert(session: AsyncSession, issuer: CurrentUser, people: list[User], event: Event | None,
                  kind: str, title: str, note: str | None) -> list[uuid.UUID]:
    """Insert certificates, skipping anyone who already holds a live one of this kind for the event."""
    if not people:
        return []
    stmt = insert(Certificate).values([
        {"id": uuid.uuid4(), "club_id": issuer.club_id, "recipient_user_id": person.id,
         "event_id": event.id if event else None, "kind": kind, "title": title, "note": note,
         "recipient_name": person.full_name, "verify_code": _code(), "issued_by": issuer.id}
        for person in people
    ])
    if event is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["recipient_user_id", "event_id", "kind"],
            index_where=Certificate.revoked_at.is_(None) & Certificate.event_id.isnot(None),
        )
    return list(await session.scalars(stmt.returning(Certificate.id)))


async def issue(session: AsyncSession, issuer: CurrentUser, data: CertificateIn) -> CertificateOut:
    event = await _event(session, issuer, data.event_id)
    if not can_issue(issuer, event):
        raise forbidden("Only the treasurer, the president or the lead of the event can issue this certificate")
    person = await session.get(User, data.recipient_user_id)
    if person is None or person.club_id != issuer.club_id:
        raise not_found("Recipient")
    created = await _insert(session, issuer, [person], event, data.kind, data.title, data.note)
    if not created:
        raise conflict("already_issued", f"{person.full_name} already has this certificate for {event.name}.")
    audit_service.record(
        session, club_id=issuer.club_id, actor_id=issuer.id, action="certidoo.issued",
        entity_type="certificate", entity_id=created[0], details={"kind": data.kind, "recipient": str(person.id)},
    )
    return (await _present(session, [await session.get(Certificate, created[0])]))[0]


async def issue_for_event(session: AsyncSession, issuer: CurrentUser, event_id: uuid.UUID, kind: str) -> BulkCertificatesOut:
    """Participation for everyone checked in, or volunteering for everyone who finished a task."""
    event = await _event(session, issuer, event_id)
    if not can_issue(issuer, event):
        raise forbidden("Only the treasurer, the president or the lead of the event can issue certificates for it")
    if kind == "participation":
        if not modules.migrated("tickets"):
            raise conflict("no_attendance", "Check-in is not set up here, so there is no attendance to certify.")
        who = select(EventTicket.holder_user_id).where(
            EventTicket.event_id == event.id, EventTicket.status == "checked_in"
        )
        title = f"Participation in {event.name}"
    else:
        who = select(CrewTask.assignee_id).where(
            CrewTask.event_id == event.id, CrewTask.status == "done", CrewTask.assignee_id.isnot(None)
        )
        title = f"Volunteering at {event.name}"
    people = list(await session.scalars(
        select(User).where(User.id.in_(who), User.club_id == issuer.club_id).order_by(User.full_name)
    ))
    created = await _insert(session, issuer, people, event, kind, title, None)
    if created:
        audit_service.record(
            session, club_id=issuer.club_id, actor_id=issuer.id, action="certidoo.issued_for_event",
            entity_type="event", entity_id=event.id, details={"kind": kind, "issued": len(created)},
        )
    return BulkCertificatesOut(issued=len(created), already_had=len(people) - len(created))


async def mine(session: AsyncSession, user: CurrentUser) -> list[CertificateOut]:
    rows = await session.scalars(
        select(Certificate).where(Certificate.recipient_user_id == user.id).order_by(Certificate.issued_at.desc())
    )
    return await _present(session, list(rows))


async def issued_in_club(session: AsyncSession, user: CurrentUser) -> list[CertificateOut]:
    """Everything issued in the club, for the people who can issue."""
    if not (user.is_finance_admin or user.role == ROLE_EVENT_LEAD):
        raise forbidden()
    rows = await session.scalars(
        select(Certificate).where(Certificate.club_id == user.club_id).order_by(Certificate.issued_at.desc()).limit(500)
    )
    return await _present(session, list(rows))


async def get(session: AsyncSession, user: CurrentUser, certificate_id: uuid.UUID) -> CertificateOut:
    certificate = await session.get(Certificate, certificate_id)
    allowed = certificate is not None and (
        certificate.recipient_user_id == user.id
        or (certificate.club_id == user.club_id and (user.is_finance_admin or user.role == ROLE_EVENT_LEAD))
    )
    if not allowed:
        raise not_found("Certificate")
    return (await _present(session, [certificate]))[0]


async def verify(session: AsyncSession, code: str) -> CertificateOut | None:
    """The public check. Tolerates spaces, dashes and lower case in a typed code."""
    code = "".join(ch for ch in code.upper() if ch.isalnum())
    if len(code) != 16:
        return None
    certificate = await session.scalar(select(Certificate).where(Certificate.verify_code == code))
    return (await _present(session, [certificate]))[0] if certificate else None


async def revoke(session: AsyncSession, user: CurrentUser, certificate_id: uuid.UUID, reason: str) -> CertificateOut:
    certificate = await session.scalar(
        select(Certificate).where(Certificate.id == certificate_id, Certificate.club_id == user.club_id).with_for_update()
    )
    if certificate is None:
        raise not_found("Certificate")
    if not can_issue(user, await _event(session, user, certificate.event_id)):
        raise forbidden("Only the treasurer, the president or the lead of the event can revoke this certificate")
    if certificate.revoked_at is not None:
        raise conflict("already_revoked", "This certificate has already been revoked.")
    certificate.revoked_at, certificate.revoked_by, certificate.revoke_reason = datetime.now(timezone.utc), user.id, reason
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="certidoo.revoked",
        entity_type="certificate", entity_id=certificate.id, details={"reason": reason},
    )
    return (await _present(session, [certificate]))[0]
