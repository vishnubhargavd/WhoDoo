"""GateSync: QR credentials for tickets, and check-in at the door.

The QR code holds one thing: `whodoo.ticket.v1.` followed by 32 unpredictable bytes. No
name, seat, event or price is in it. The bytes are an HMAC of the ticket's id under a server
secret, so the buyer's page can show the same code again without the code ever being stored;
the database keeps only its SHA-256 digest, which is what a scan is looked up by. The format
and the digest match web/src/modules/gatesync/credential.ts.

The same credential is used for a public registration (registration_service), looked up in
event_registrations. A ticket or registration is admitted once. The scan locks the booking, then the ticket (the order the
booking service uses), and the admission row's primary key is the ticket, so two scanners
reading the same code at the same moment cannot both let it in.
"""

import base64
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timezone

import segno
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import ROLE_EVENT_LEAD, ROLE_PRESIDENT, ROLE_TREASURER, ROLE_VOLUNTEER, CurrentUser
from app.core.errors import forbidden, not_found
from app.models.community import TicketRedemption
from app.models.fairseat import EventTicket, TicketBooking, TicketCategory
from app.models.registration import EventRegistration as R
from app.models.shared import Event, User
from app.services import audit_service, modules
from app.services.syncsphere import IST

PREFIX = "whodoo.ticket.v1."
# 32 bytes are 43 base64url characters; the last one carries four zero padding bits.
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
DOOR_STAFF = (ROLE_PRESIDENT, ROLE_TREASURER, ROLE_EVENT_LEAD, ROLE_VOLUNTEER)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """UTC with milliseconds and a Z, the form the web scanner's contract expects."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# --- Credentials -----------------------------------------------------------------------


def _secret() -> bytes:
    settings = get_settings()
    return (settings.ticket_credential_secret or settings.token_secret).encode()


def credential_for(ticket_id: uuid.UUID) -> str:
    token = hmac.new(_secret(), b"gatesync.ticket.v1:" + ticket_id.bytes, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(token).rstrip(b"=").decode()


def digest_of(credential: object) -> bytes | None:
    """The stored digest for a scanned value, or None if it is not in the credential format."""
    if not isinstance(credential, str) or not credential.startswith(PREFIX):
        return None
    token = credential[len(PREFIX):]
    if not _TOKEN.match(token):
        return None
    raw = base64.urlsafe_b64decode(token + "=")
    if len(raw) != 32:
        return None
    return hashlib.sha256(PREFIX.encode() + raw).digest()


def qr_svg(credential: str) -> str:
    """The credential as an inline SVG QR code. Rendered on request, never saved."""
    # omitsize: the image carries a viewBox instead of a fixed pixel size, so it scales to the box
    # it is placed in. With a fixed size, a box smaller than the code showed only its corner.
    return segno.make(credential, error="m").svg_inline(scale=6, border=2, dark="#111", light="#fff", omitsize=True)


async def issue_for_booking(session: AsyncSession, booking: TicketBooking, now: datetime | None = None) -> int:
    """Give every seat of a confirmed booking its credential. Does nothing for any other booking."""
    if booking.status != "confirmed":
        return 0
    now = now or utcnow()
    tickets = list(await session.scalars(
        select(EventTicket)
        .where(EventTicket.booking_id == booking.id, EventTicket.status == "pending_issue")
        .with_for_update()
    ))
    for ticket in tickets:
        ticket.credential_digest = digest_of(credential_for(ticket.id))
        ticket.status, ticket.issued_at = "issued", now
    if tickets:
        await session.flush()
    return len(tickets)


async def my_tickets(session: AsyncSession, user: CurrentUser, now: datetime | None = None) -> list[dict]:
    """The caller's confirmed bookings, newest event first, each seat with its QR code."""
    now = now or utcnow()
    rows = (await session.execute(
        select(TicketBooking, Event, TicketCategory)
        .join(Event, Event.id == TicketBooking.event_id)
        .join(TicketCategory, TicketCategory.id == TicketBooking.ticket_category_id)
        .where(TicketBooking.purchaser_user_id == user.id, TicketBooking.status == "confirmed")
        .order_by(Event.starts_at.desc().nulls_last(), TicketBooking.created_at.desc())
    )).all()
    out = []
    for booking, event, category in rows:
        # Covers bookings confirmed before credentials existed.
        await issue_for_booking(session, booking, now)
        tickets = await session.scalars(
            select(EventTicket).where(EventTicket.booking_id == booking.id).order_by(EventTicket.seat_number)
        )
        seats = []
        for ticket in tickets:
            usable = ticket.status == "issued" and event.status == "published"
            credential = credential_for(ticket.id) if usable else None
            seats.append({
                "ticket_id": ticket.id, "seat_number": ticket.seat_number, "status": ticket.status,
                "checked_in_at": ticket.checked_in_at, "credential": credential,
                "qr_svg": qr_svg(credential) if credential else None,
            })
        out.append({"booking": booking, "event": event, "category": category, "tickets": seats})
    return out


# --- The door --------------------------------------------------------------------------


def can_scan(user: CurrentUser, event: Event) -> bool:
    return user.club_id == event.club_id and user.role in DOOR_STAFF


async def _door_event(session: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> Event:
    event = await session.get(Event, event_id)
    if event is None or event.club_id != user.club_id:
        raise not_found("Event")
    if not can_scan(user, event):
        raise forbidden("Only volunteers, event leads, the treasurer and the president can check people in")
    return event


async def events_for_door(session: AsyncSession, user: CurrentUser) -> list[dict]:
    """Published events in the caller's club that have anyone to admit, with how many are in."""
    if user.role not in DOOR_STAFF:
        raise forbidden("Only volunteers, event leads, the treasurer and the president can check people in")
    events = list(await session.scalars(
        select(Event).where(Event.club_id == user.club_id, Event.status == "published")
        .order_by(Event.starts_at.asc().nulls_last(), Event.name)
    ))
    counts: dict[uuid.UUID, list[int]] = {event.id: [0, 0] for event in events}

    def add(rows) -> None:
        for event_id, total, inside in rows:
            if event_id in counts:
                counts[event_id][0] += total
                counts[event_id][1] += inside

    add((await session.execute(
        select(R.event_id, func.count(), func.count().filter(R.status == "checked_in"))
        .where(R.club_id == user.club_id, R.status.in_(("registered", "checked_in")))
        .group_by(R.event_id)
    )).all())
    if modules.has("tickets"):
        add((await session.execute(
            select(EventTicket.event_id, func.count(), func.count().filter(EventTicket.status == "checked_in"))
            .where(EventTicket.status.in_(("issued", "checked_in")))
            .group_by(EventTicket.event_id)
        )).all())
    now = utcnow()
    listed = [{"event": event, "tickets": counts[event.id][0], "checked_in": counts[event.id][1],
               "today": bool(event.starts_at and event.starts_at.astimezone(IST).date() == now.astimezone(IST).date())}
              for event in events if counts[event.id][0]]
    # Whatever is on today comes first: that is the door someone is standing at.
    listed.sort(key=lambda row: not row["today"])
    return listed


async def _admit(
    session: AsyncSession, scanner: CurrentUser, event: Event, ticket_id: uuid.UUID, method: str, now: datetime
) -> dict:
    """Admit one FairSeat ticket, or say exactly why not. Booking first, then ticket: the booking service's order."""
    booking_id = await session.scalar(select(EventTicket.booking_id).where(EventTicket.id == ticket_id))
    booking = await session.scalar(select(TicketBooking).where(TicketBooking.id == booking_id).with_for_update())
    ticket = await session.scalar(select(EventTicket).where(EventTicket.id == ticket_id).with_for_update())
    if ticket is None or booking is None:
        return {"status": "unrecognized"}
    if ticket.event_id != event.id:
        return {"status": "wrong_event"}
    holder = await session.get(User, ticket.holder_user_id)
    category = await session.get(TicketCategory, booking.ticket_category_id)
    who = {"holderName": holder.full_name, "seatNumber": ticket.seat_number, "categoryName": category.name,
           "partySize": booking.quantity}
    if ticket.status == "checked_in":
        return {"status": "already_checked_in", "checkedInAt": _iso(ticket.checked_in_at), **who}
    if ticket.status != "issued" or booking.status != "confirmed":
        return {"status": "revoked", **who}
    if event.status != "published":
        return {"status": "expired", **who}

    session.add(TicketRedemption(
        ticket_id=ticket.id, event_id=event.id, club_id=event.club_id, scanned_by=scanner.id,
        method=method, checked_in_at=now,
    ))
    ticket.status, ticket.checked_in_at = "checked_in", now
    await session.flush()
    audit_service.record(
        session, club_id=event.club_id, actor_id=scanner.id, action="gatesync.checked_in",
        entity_type="event_ticket", entity_id=ticket.id, details={"event_id": str(event.id), "method": method},
    )
    return {"status": "redeemed", "redemptionId": str(ticket.id), "checkedInAt": _iso(now),
            "eventId": str(event.id), "eventName": event.name, **who}


async def _admit_registration(
    session: AsyncSession, scanner: CurrentUser, event: Event, registration_id: uuid.UUID, method: str, now: datetime
) -> dict:
    """Admit one registered participant, once. The row lock makes two scanners take turns:
    the second one finds the registration already checked in."""
    registration = await session.scalar(select(R).where(R.id == registration_id).with_for_update())
    if registration is None:
        return {"status": "unrecognized"}
    if registration.event_id != event.id:
        return {"status": "wrong_event"}
    # Shown to door staff so the name can be matched against a college ID card.
    who = {"holderName": registration.full_name, "studentId": registration.student_id,
           "participantCode": registration.participant_code, "categoryName": "Registration"}
    if registration.status == "checked_in":
        return {"status": "already_checked_in", "checkedInAt": _iso(registration.checked_in_at), **who}
    if registration.status != "registered":
        return {"status": "revoked", **who}
    if event.status != "published":
        return {"status": "expired", **who}
    registration.status, registration.checked_in_at, registration.checked_in_by = "checked_in", now, scanner.id
    registration.updated_at = now
    await session.flush()
    audit_service.record(
        session, club_id=event.club_id, actor_id=scanner.id, action="gatesync.checked_in",
        entity_type="event_registration", entity_id=registration.id,
        details={"event_id": str(event.id), "method": method},
    )
    return {"status": "redeemed", "redemptionId": str(registration.id), "checkedInAt": _iso(now),
            "eventId": str(event.id), "eventName": event.name, **who}


async def redeem(
    session: AsyncSession, scanner: CurrentUser, event_id: uuid.UUID, credential: str, now: datetime | None = None
) -> dict:
    """Check a scanned QR code in for this event: a FairSeat ticket or a public registration."""
    event = await _door_event(session, scanner, event_id)
    digest = digest_of(credential)
    if digest is None:
        return {"status": "invalid_credential"}
    now = now or utcnow()
    if modules.has("tickets"):
        ticket_id = await session.scalar(select(EventTicket.id).where(EventTicket.credential_digest == digest))
        if ticket_id is not None:
            return await _admit(session, scanner, event, ticket_id, "qr", now)
    registration_id = await session.scalar(select(R.id).where(R.credential_digest == digest))
    if registration_id is None:
        return {"status": "unrecognized"}
    return await _admit_registration(session, scanner, event, registration_id, "qr", now)


async def admit_from_list(
    session: AsyncSession, scanner: CurrentUser, event_id: uuid.UUID, ticket_id: uuid.UUID | None = None,
    registration_id: uuid.UUID | None = None, now: datetime | None = None,
) -> dict:
    """Check in from the guest list, for a holder whose phone is flat. Same rules as a scan."""
    event = await _door_event(session, scanner, event_id)
    if registration_id is not None:
        return await _admit_registration(session, scanner, event, registration_id, "manual", now or utcnow())
    if ticket_id is None or not modules.has("tickets"):
        return {"status": "unrecognized"}
    return await _admit(session, scanner, event, ticket_id, "manual", now or utcnow())


async def attendance(session: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> dict:
    """The guest list for an event: everyone with a valid ticket or registration, and who is already in."""
    event = await _door_event(session, user, event_id)
    guests = [
        {"ticket_id": None, "registration_id": row.id, "holder_name": row.full_name,
         "category": f"{row.participant_code} · {row.student_id}", "seat_number": None,
         "status": row.status, "checked_in_at": row.checked_in_at}
        for row in await session.scalars(
            select(R).where(R.event_id == event.id, R.status.in_(("registered", "checked_in"))).order_by(R.full_name)
        )
    ]
    if modules.has("tickets"):
        rows = (await session.execute(
            select(EventTicket, User.full_name, TicketCategory.name)
            .join(TicketBooking, TicketBooking.id == EventTicket.booking_id)
            .join(User, User.id == EventTicket.holder_user_id)
            .join(TicketCategory, TicketCategory.id == TicketBooking.ticket_category_id)
            .where(EventTicket.event_id == event.id, EventTicket.status.in_(("issued", "checked_in")),
                   TicketBooking.status == "confirmed")
            .order_by(User.full_name, EventTicket.booking_id, EventTicket.seat_number)
        )).all()
        guests += [
            {"ticket_id": ticket.id, "registration_id": None, "holder_name": name, "category": category,
             "seat_number": ticket.seat_number, "status": ticket.status, "checked_in_at": ticket.checked_in_at}
            for ticket, name, category in rows
        ]
    return {"event": event, "guests": guests, "total": len(guests),
            "checked_in": sum(guest["status"] == "checked_in" for guest in guests)}
