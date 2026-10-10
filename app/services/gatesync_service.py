"""Online-only QR issuance and single-use redemption. Never log bearer credentials.

Transactions follow event -> settings -> categories -> booking -> ticket. Routes commit;
no provider or network calls happen here. Ciphertext allows stable authorized redisplay.
"""
import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, forbidden, not_found
from app.models.fairseat import EventTicket, TicketBooking, TicketCategory
from app.models.shared import Event, User
from app.services import audit_service, ticket_locks

PREFIX = b"whodoo.ticket.v1."


def _key() -> bytes:
    try:
        value = get_settings().gatesync_credential_key
        key = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise AppError(503, "gatesync_configuration", "Ticket credentials are not configured") from None


def digest_credential(value: str) -> bytes | None:
    if not isinstance(value, str) or len(value) != len(PREFIX) + 43 or not value.startswith(PREFIX.decode()):
        return None
    token = value[len(PREFIX):]
    try:
        raw = base64.b64decode(token + "=", altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != token:
        return None
    return hashlib.sha256(PREFIX + raw).digest()


async def _staff_allowed(db: AsyncSession, user: CurrentUser, event: Event) -> bool:
    if user.club_id != event.club_id:
        return False
    if user.role == "president" or (user.role == "event_lead" and event.lead_id == user.id):
        return True
    if user.role != "volunteer":
        return False
    return bool(await db.scalar(text(
        "SELECT 1 FROM gatesync_staff WHERE event_id=:event AND club_id=:club AND user_id=:user"
    ), {"event": event.id, "club": user.club_id, "user": user.id}))


async def access(db: AsyncSession, user: CurrentUser) -> dict:
    events = await db.scalars(select(Event).where(Event.club_id == user.club_id, Event.status == "published")
                             .order_by(Event.starts_at, Event.id))
    result = []
    for event in events:
        if await _staff_allowed(db, user, event):
            result.append({"event_id": event.id, "event_name": event.name, "club_id": event.club_id,
                           "starts_at": event.starts_at})
    return {"events": result}


async def assign_staff(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID, staff_id: uuid.UUID) -> None:
    event = await ticket_locks.lock_event(db, event_id)
    if not event or event.club_id != user.club_id:
        raise not_found("Event")
    if user.role != "president" and not (user.role == "event_lead" and event.lead_id == user.id):
        raise forbidden("Only this event's lead or president can assign check-in volunteers")
    staff = await db.get(User, staff_id)
    if not staff or staff.club_id != user.club_id or staff.role != "volunteer":
        raise forbidden("Check-in staff must be an existing volunteer in this club")
    await db.execute(text(
        "INSERT INTO gatesync_staff (event_id,club_id,user_id,assigned_by) VALUES (:event,:club,:staff,:actor) "
        "ON CONFLICT (event_id,user_id) DO NOTHING"
    ), {"event": event_id, "club": user.club_id, "staff": staff_id, "actor": user.id})
    audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="gatesync_staff_assigned",
                         entity_type="event", entity_id=event_id, details={"staff_id": str(staff_id)})


async def tickets(db: AsyncSession, user: CurrentUser) -> list[dict]:
    rows = (await db.execute(select(EventTicket, TicketBooking, Event, TicketCategory)
        .join(TicketBooking, EventTicket.booking_id == TicketBooking.id)
        .join(Event, EventTicket.event_id == Event.id)
        .join(TicketCategory, TicketBooking.ticket_category_id == TicketCategory.id)
        .where(EventTicket.holder_user_id == user.id, Event.club_id == user.club_id)
        .order_by(EventTicket.created_at.desc(), EventTicket.seat_number))).all()
    return [{"ticket_id": t.id, "event_id": e.id, "event_name": e.name, "venue": e.venue,
             "starts_at": e.starts_at, "category": c.name, "holder_name": user.full_name,
             "booking_status": b.status, "payment_status": b.payment_status, "ticket_status": t.status,
             "issued_at": t.issued_at, "checked_in_at": t.checked_in_at} for t, b, e, c in rows]


async def _lock_ticket(db: AsyncSession, ticket_id: uuid.UUID):
    snapshot = (await db.execute(select(EventTicket.event_id, EventTicket.booking_id)
                                .where(EventTicket.id == ticket_id))).first()
    if not snapshot:
        raise not_found("Ticket")
    event, _, _ = await ticket_locks.lock_for_event(db, snapshot.event_id)
    booking = await ticket_locks.lock_booking(db, snapshot.booking_id)
    ticket = await db.scalar(select(EventTicket).where(EventTicket.id == ticket_id).with_for_update()
                             .execution_options(populate_existing=True))
    return ticket, booking, event


def _eligible(ticket: EventTicket, booking: TicketBooking, event: Event, now: datetime) -> bool:
    return (ticket.booking_id == booking.id and ticket.event_id == booking.event_id == event.id
            and ticket.holder_user_id == booking.purchaser_user_id
            and event.status == "published" and event.ends_at is not None and now < event.ends_at
            and booking.status == "confirmed" and booking.payment_status in ("paid", "not_required")
            and ticket.status in ("pending_issue", "issued", "checked_in")
            and ticket.revoked_at is None)


async def issue(db: AsyncSession, user: CurrentUser, ticket_id: uuid.UUID, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ticket, booking, event = await _lock_ticket(db, ticket_id)
    if ticket.holder_user_id != user.id or event.club_id != user.club_id:
        raise not_found("Ticket")
    if not _eligible(ticket, booking, event, now) or ticket.status == "checked_in":
        raise AppError(409, "ticket_not_eligible", "This ticket cannot receive a usable QR credential")
    cipher = AESGCM(_key())
    encrypted = await db.scalar(text("SELECT credential_encrypted FROM event_tickets WHERE id=:id"), {"id": ticket.id})
    if encrypted:
        try:
            encrypted = bytes(encrypted)
            credential = cipher.decrypt(encrypted[:12], encrypted[12:], ticket.id.bytes).decode("ascii")
            if digest_credential(credential) != ticket.credential_digest:
                raise ValueError()
        except Exception:
            raise AppError(503, "credential_unavailable", "Ticket credential recovery is unavailable") from None
    else:
        raw = secrets.token_bytes(32)
        credential = PREFIX.decode() + base64.urlsafe_b64encode(raw).decode().rstrip("=")
        nonce = secrets.token_bytes(12)
        encrypted = nonce + cipher.encrypt(nonce, credential.encode("ascii"), ticket.id.bytes)
        ticket.credential_digest = digest_credential(credential)
        ticket.status = "issued"
        ticket.issued_at = now
        await db.execute(text("UPDATE event_tickets SET credential_encrypted=:value WHERE id=:id"),
                         {"value": encrypted, "id": ticket.id})
        audit_service.record(db, club_id=user.club_id, actor_id=user.id, action="gatesync_credential_issued",
                             entity_type="ticket", entity_id=ticket.id)
        await db.flush()
    return {"ticket_id": ticket.id, "credential": credential, "issued_at": ticket.issued_at}


async def redeem(db: AsyncSession, user: CurrentUser, event_id: uuid.UUID, credential: str,
                 *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    # Verify staff rights before any credential lookup, including malformed/replayed inputs.
    event, _, _ = await ticket_locks.lock_for_event(db, event_id)
    if not event or event.club_id != user.club_id:
        raise not_found("Event")
    if not await _staff_allowed(db, user, event):
        raise forbidden("You are not assigned to check in this event")
    digest = digest_credential(credential)
    ticket = None
    outcome, reason = "not_eligible", "Invalid ticket credential"
    if digest is not None:
        ticket = await db.scalar(select(EventTicket).where(EventTicket.credential_digest == digest))
        if ticket and ticket.event_id != event_id:
            ticket = None
            outcome, reason = "wrong_event", "This credential is not for the selected event"
        elif ticket:
            booking = await ticket_locks.lock_booking(db, ticket.booking_id)
            ticket = await db.scalar(select(EventTicket).where(EventTicket.id == ticket.id).with_for_update()
                                     .execution_options(populate_existing=True))
            if ticket.credential_digest != digest or not _eligible(ticket, booking, event, now):
                outcome, reason = "not_eligible", "Ticket is revoked, expired or not confirmed"
            elif ticket.status == "checked_in":
                outcome, reason = "already_checked_in", "This ticket was already checked in"
            elif ticket.status == "issued":
                ticket.status, ticket.checked_in_at = "checked_in", now
                await db.execute(text(
                    "INSERT INTO gatesync_attendance (ticket_id,event_id,club_id,scanned_by,checked_in_at) "
                    "VALUES (:ticket,:event,:club,:actor,:at)"
                ), {"ticket": ticket.id, "event": event_id, "club": event.club_id, "actor": user.id, "at": now})
                outcome, reason = "checked_in", "Check-in confirmed by WhoDoo"
    audit_service.record(db, club_id=user.club_id, actor_id=user.id,
                         action="gatesync_" + outcome, entity_type="ticket", entity_id=ticket.id if ticket else None,
                         details={"event_id": str(event_id), "outcome": outcome})
    await db.flush()
    return {"outcome": outcome, "reason": reason, "ticket_id": ticket.id if ticket else None,
            "checked_in_at": ticket.checked_in_at if ticket else None}
