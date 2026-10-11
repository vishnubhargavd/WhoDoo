"""Public event registration: any student, no account.

    places taken = registered + checked in + unpaid holds that have not run out

Registering locks the event row, counts the places taken and inserts only if one is left,
so the limit holds however many people press the button at once. A person gets one place per
event, by email and by student ID.

Each registration gets a QR credential from GateSync (an HMAC of its id; only the digest is
stored, under a unique constraint) and a private ticket link made the same way. Nobody is
signed in, so that link is the participant's proof of who they are: every participant action
(pay, view, resend) goes through it.

Paid events use the same payment provider as merchandise. The amount is the event's price;
the provider is asked what was paid, never the browser. Money that arrives for a place that
can no longer be given is sent back. Lock order: event row, then registration row.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.db.database import SessionLocal
from app.models.crewpulse import CrewTask
from app.models.membercore import Membership, MembershipPlan
from app.models.registration import LIVE, EventRegistration, Venue
from app.models.shared import Club, Event, User
from app.schemas.registration import PublicEventOut, RegistrationCheckoutOut, RegistrationIn, RegistrationOut
from app.services import audit_service, email_service, event_service, gatesync_service, ledger_service, payment_service
from app.services.payment_service import CheckoutLine, CheckoutRequest, CheckoutSession
from app.services.syncsphere import IST

logger = logging.getLogger("whodoo.registration")
R = EventRegistration
STRIPE_MIN_SESSION_SECONDS = 1800


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rupees(paise: int) -> Decimal:
    return Decimal(paise) / Decimal(100)


def money(paise: int) -> str:
    return "Free" if not paise else f"₹{paise // 100:,}.{paise % 100:02d}"


# --- The private ticket link ------------------------------------------------------------


def link_token(registration_id: uuid.UUID) -> str:
    """The token in a participant's ticket link. Recomputed when needed; never stored."""
    raw = hmac.new(gatesync_service._secret(), b"registration.link.v1:" + registration_id.bytes, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def link_digest(token: str) -> bytes:
    return hashlib.sha256(b"registration.link.v1:" + token.encode()).digest()


def ticket_url(registration_id: uuid.UUID) -> str:
    return f"{get_settings().app_base_url.rstrip('/')}/ticket/{link_token(registration_id)}"


def _out(registration: EventRegistration, event: Event, resumed: bool = False) -> RegistrationOut:
    return RegistrationOut(
        participant_code=registration.participant_code, participant_no=registration.participant_no,
        full_name=registration.full_name, email=registration.email, status=registration.status,
        amount_paise=registration.amount_paise, hold_expires_at=registration.hold_expires_at,
        event_id=event.id, event_name=event.name, ticket_url=ticket_url(registration.id), resumed=resumed,
    )


# --- What the public sees ---------------------------------------------------------------


async def _taken(db: AsyncSession, event_ids: list[uuid.UUID], now: datetime) -> dict[uuid.UUID, int]:
    if not event_ids:
        return {}
    rows = await db.execute(
        select(R.event_id, func.count())
        .where(R.event_id.in_(event_ids),
               or_(R.status.in_(("registered", "checked_in")),
                   (R.status == "pending_payment") & (R.hold_expires_at > now)))
        .group_by(R.event_id)
    )
    return dict(rows.all())


async def is_participant(db: AsyncSession, event_id: uuid.UUID, person: User) -> bool:
    """Whether this club user holds a place at the event: registered while signed in, or with their email."""
    return await db.scalar(
        select(R.id).where(
            R.event_id == event_id, R.status.in_(LIVE),
            or_(R.user_id == person.id, func.lower(R.email) == person.email.lower()),
        ).limit(1)
    ) is not None


async def participant_user_ids(db: AsyncSession, event_id: uuid.UUID, club_id: uuid.UUID) -> set[uuid.UUID]:
    """Club users who hold a place at the event, so they are left out when tasks are handed out."""
    rows = await db.scalars(
        select(User.id).where(
            User.club_id == club_id,
            User.id.in_(select(R.user_id).where(R.event_id == event_id, R.status.in_(LIVE), R.user_id.isnot(None)))
            | func.lower(User.email).in_(select(func.lower(R.email)).where(R.event_id == event_id, R.status.in_(LIVE))),
        )
    )
    return set(rows)


async def price_for(db: AsyncSession, event: Event, user: CurrentUser | None, now: datetime | None = None) -> dict:
    """What this person pays for a place, and why.

    Signed out, or from another club: the event's price. A member of the event's club with an
    active membership: the price less their tier's discount (the rate their current term was
    bought with). The president: nothing, always. A name or email typed into the form never
    earns a discount; only the signed-in account does.
    """
    now = now or utcnow()
    full = event.price_paise if event.registration_mode == "paid" else 0
    offer = {"full_paise": full, "price_paise": full, "discount_bps": 0, "tier": None, "reason": None}
    if not full or user is None or user.club_id != event.club_id:
        return offer
    if user.role == "president":
        return {**offer, "price_paise": 0, "discount_bps": 10000, "tier": "President", "reason": "The president's place is always free."}
    row = (await db.execute(
        select(Membership, MembershipPlan.name)
        .join(MembershipPlan, MembershipPlan.id == Membership.plan_id)
        .where(Membership.user_id == user.id, Membership.club_id == event.club_id)
    )).first()
    if row is None:
        return offer
    membership, plan = row
    if membership.status != "active" or not (membership.starts_at <= now < membership.expires_at):
        return {**offer, "reason": f"Your {plan} membership is not active, so the full price applies."}
    bps = membership.discount_bps_snapshot
    if not bps:
        return {**offer, "tier": plan}
    price = (full * (10000 - bps) + 5000) // 10000
    return {**offer, "price_paise": price, "discount_bps": bps, "tier": plan,
            "reason": f"{plan} membership: {bps / 100:g}% off."}


async def my_registrations(db: AsyncSession, user: CurrentUser) -> list[dict]:
    """The signed-in person's own places at events, newest first, each with its ticket link."""
    rows = (await db.execute(
        select(R, Event).join(Event, Event.id == R.event_id)
        .where(R.user_id == user.id, R.status.in_(LIVE)).order_by(Event.starts_at.desc())
    )).all()
    return [{"registration": r, "event": e, "ticket_path": "/ticket/" + link_token(r.id)} for r, e in rows]


def _state(event: Event, taken: int, now: datetime) -> str:
    if event.registration_mode == "none":
        return "none"
    if event.starts_at <= now:
        return "closed"
    return "full" if taken >= (event.max_participants or 0) else "open"


async def public_events(db: AsyncSession, now: datetime | None = None, event_id: uuid.UUID | None = None) -> list[PublicEventOut]:
    """Published events of every club that have not ended, soonest first. No sign-in needed."""
    now = now or utcnow()
    stmt = (
        select(Event, Club.name, Venue.location)
        .join(Club, Club.id == Event.club_id)
        .outerjoin(Venue, Venue.id == Event.venue_id)
        .where(Event.status == "published", Event.starts_at.isnot(None), Event.ends_at > now)
        .order_by(Event.starts_at, Event.name)
    )
    if event_id is not None:
        stmt = stmt.where(Event.id == event_id)
    rows = (await db.execute(stmt)).all()
    taken = await _taken(db, [event.id for event, _, _ in rows], now)
    out = []
    for event, club_name, location in rows:
        used = taken.get(event.id, 0)
        limit = event.max_participants if event.registration_mode != "none" else None
        out.append(PublicEventOut(
            id=event.id, name=event.name, description=event.description, club_name=club_name,
            starts_at=event.starts_at, ends_at=event.ends_at, venue=event.venue, venue_location=location,
            registration_mode=event.registration_mode, price_paise=event.price_paise, max_participants=limit,
            registered=used, places_left=max(limit - used, 0) if limit is not None else None,
            registration_state=_state(event, used, now),
        ))
    return out


CALENDAR_BACK = timedelta(days=92)
CALENDAR_AHEAD = timedelta(days=366)


async def calendar_events(db: AsyncSession, now: datetime | None = None) -> list[PublicEventOut]:
    """Everything the public calendar shows: published and completed events of every club, from
    about three months back to a year ahead. Drafts and cancelled events never appear."""
    now = now or utcnow()
    rows = (await db.execute(
        select(Event, Club.name, Venue.location)
        .join(Club, Club.id == Event.club_id)
        .outerjoin(Venue, Venue.id == Event.venue_id)
        .where(Event.status.in_(("published", "completed")), Event.starts_at.isnot(None), Event.ends_at.isnot(None),
               Event.starts_at >= now - CALENDAR_BACK, Event.starts_at <= now + CALENDAR_AHEAD)
        .order_by(Event.starts_at, Event.name)
    )).all()
    taken = await _taken(db, [event.id for event, _, _ in rows], now)
    out = []
    for event, club_name, location in rows:
        used = taken.get(event.id, 0)
        takes = event.registration_mode != "none"
        limit = event.max_participants if takes else None
        over = event.status != "published" or event.ends_at <= now
        out.append(PublicEventOut(
            id=event.id, name=event.name, description=event.description, club_name=club_name,
            starts_at=event.starts_at, ends_at=event.ends_at, venue=event.venue, venue_location=location,
            registration_mode=event.registration_mode, price_paise=event.price_paise, max_participants=limit,
            registered=used, places_left=max(limit - used, 0) if limit is not None else None,
            registration_state=("closed" if takes else "none") if over else _state(event, used, now),
        ))
    return out


async def public_event(db: AsyncSession, event_id: uuid.UUID, now: datetime | None = None) -> PublicEventOut:
    found = await public_events(db, now, event_id)
    if not found:
        raise not_found("Event")
    return found[0]


# --- Registering ------------------------------------------------------------------------


async def _expire_holds(db: AsyncSession, event_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        update(R)
        .where(R.event_id == event_id, R.status == "pending_payment", R.hold_expires_at <= now)
        .values(status="expired", hold_expires_at=None, updated_at=now)
    )


async def _duplicates(db: AsyncSession, event_id: uuid.UUID, email: str, student_id: str, skip: uuid.UUID | None = None):
    stmt = select(R).where(
        R.event_id == event_id, R.status.in_(LIVE),
        or_(func.lower(R.email) == email.lower(), func.upper(R.student_id) == student_id.upper()),
    )
    if skip is not None:
        stmt = stmt.where(R.id != skip)
    return list(await db.scalars(stmt))


async def register(
    db: AsyncSession, event_id: uuid.UUID, data: RegistrationIn, now: datetime | None = None,
    user: CurrentUser | None = None,
) -> tuple[EventRegistration, Event]:
    """Take a place for this person, if one is left. Free events confirm at once; paid ones hold the place.

    `user` is whoever is signed in, if anyone. A member of the event's club pays their tier's price
    and the president pays nothing (price_for). Someone with a task on the event is crew, not a
    participant, and is refused."""
    now = now or utcnow()
    if data.website:
        raise bad_request("registration_refused", "This registration could not be accepted.")
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if event is None or event.status != "published" or event.registration_mode == "none":
        raise not_found("Event")
    if event.starts_at <= now:
        raise conflict("registration_closed", "Registration closed when this event started.")
    await _expire_holds(db, event.id, now)

    crew = [CrewTask.assignee_id == user.id] if user is not None and user.club_id == event.club_id else []
    crew.append(CrewTask.assignee_id.in_(
        select(User.id).where(User.club_id == event.club_id, func.lower(User.email) == data.email)))
    if await db.scalar(select(CrewTask.id).where(CrewTask.event_id == event.id, or_(*crew)).limit(1)):
        raise conflict("has_tasks", "You have tasks on this event, so you are part of the crew. Ask the event "
                                    "lead to reassign them if you would rather attend as a participant.")
    if user is not None and user.club_id == event.club_id and await db.scalar(
        select(R.id).where(R.event_id == event.id, R.user_id == user.id, R.status.in_(LIVE)).limit(1)
    ):
        raise conflict("already_registered", "You are already registered for this event.")

    for other in await _duplicates(db, event.id, data.email, data.student_id):
        same_person = other.email.lower() == data.email and other.student_id.upper() == data.student_id
        if other.status == "pending_payment" and same_person:
            # They came back before paying. The old hold is dropped and a fresh one made; the
            # earlier ticket link is not handed to whoever typed these details.
            other.status, other.hold_expires_at, other.updated_at = "expired", None, now
            await db.flush()
            continue
        raise conflict(
            "already_registered",
            "This email or student ID is already registered for this event. The ticket was sent to the "
            "email address used when registering.",
        )

    limit = event.max_participants or 0
    taken = (await _taken(db, [event.id], now)).get(event.id, 0)
    if taken >= limit:
        raise conflict(
            "sold_out", f"All {limit} places for this event are taken. No more registrations can be accepted.",
            {"max_participants": limit, "registered": taken},
        )

    # The price is worked out here from the event and the signed-in person's own membership.
    # Nothing in the request can lower it; signed out, it is always the full price.
    offer = await price_for(db, event, user, now)
    paid = offer["price_paise"] > 0
    registration_id = uuid.uuid4()
    number = (await db.scalar(select(func.max(R.participant_no)).where(R.event_id == event.id)) or 0) + 1
    registration = EventRegistration(
        id=registration_id, event_id=event.id, club_id=event.club_id, participant_no=number,
        participant_code="WD-" + base64.b32encode(secrets.token_bytes(5)).decode()[:6],
        full_name=data.full_name, phone=data.phone, email=data.email, student_id=data.student_id,
        status="pending_payment" if paid else "registered",
        amount_paise=offer["price_paise"], currency="INR",
        user_id=user.id if user is not None and user.club_id == event.club_id else None,
        hold_expires_at=now + timedelta(seconds=get_settings().registration_hold_seconds) if paid else None,
        credential_digest=gatesync_service.digest_of(gatesync_service.credential_for(registration_id)),
        access_digest=link_digest(link_token(registration_id)),
        created_at=now, updated_at=now,
    )
    db.add(registration)
    await db.flush()
    audit_service.record(
        db, club_id=event.club_id, actor_id=None, action="registration.created", entity_type="event_registration",
        entity_id=registration.id, details={"event_id": str(event.id), "participant_no": number, "status": registration.status},
    )
    return registration, event


async def by_token(db: AsyncSession, token: str) -> tuple[EventRegistration, Event]:
    """The registration a ticket link points at. Unknown links are simply not found."""
    registration = await db.scalar(select(R).where(R.access_digest == link_digest(token))) if 20 <= len(token) <= 64 else None
    if registration is None:
        raise not_found("Ticket")
    return registration, await db.get(Event, registration.event_id)


def present(registration: EventRegistration, event: Event) -> RegistrationOut:
    return _out(registration, event)


def qr_for(registration: EventRegistration, event: Event) -> str | None:
    """The QR code, only while it would be accepted at the door."""
    if registration.status != "registered" or event.status != "published":
        return None
    return gatesync_service.qr_svg(gatesync_service.credential_for(registration.id))


# --- Paying -----------------------------------------------------------------------------


async def create_checkout(db: AsyncSession, token: str, now: datetime | None = None) -> RegistrationCheckoutOut:
    """Open (or reopen) the payment page for a held place."""
    now = now or utcnow()
    registration, event = await by_token(db, token)

    def payable(found: EventRegistration) -> None:
        if found.paid_at is not None:
            raise conflict("already_paid", "This registration has already been paid for.")
        if found.status != "pending_payment" or found.hold_expires_at <= now:
            raise conflict("hold_expired", "The hold on this place has run out. Please register again.")

    payable(registration)
    if event.status != "published":
        raise conflict("event_not_open", f"This event is {event.status} and is not taking payments.")
    provider = payment_service.get_provider()
    current: CheckoutSession | None = None
    if registration.checkout_session_id:
        current = await run_in_threadpool(provider.verify_payment, registration.checkout_session_id)
        if current is not None and current.payment_status == "paid":
            raise conflict("payment_received", "Your payment has been received and is being confirmed.")
    if current is None or current.status != "open":
        link = ticket_url(registration.id)
        request = CheckoutRequest(
            amount_paise=registration.amount_paise, currency="inr",
            lines=[CheckoutLine(name=f"{event.name}: registration", unit_paise=registration.amount_paise, quantity=1)],
            metadata={"event_registration_id": str(registration.id), "event_id": str(event.id),
                      "club_id": str(event.club_id), "participant": registration.participant_code},
            success_url=f"{link}?session_id={{CHECKOUT_SESSION_ID}}", cancel_url=link,
            reference=str(registration.id), expires_at=int(now.timestamp()) + STRIPE_MIN_SESSION_SECONDS,
        )
        try:
            current = await run_in_threadpool(provider.create_checkout_session, request)
        except payment_service.ProviderRejectedKey as exc:
            provider = payment_service._fall_back_to_mock(str(exc))
            current = await run_in_threadpool(provider.create_checkout_session, request)
        if current.amount_total != registration.amount_paise:
            raise AppError(502, "payment_provider_error", "The payment service returned the wrong amount")
        locked = await db.scalar(select(R).where(R.id == registration.id).with_for_update())
        payable(locked)
        locked.checkout_session_id, locked.updated_at = current.id, now
        await db.flush()
    return RegistrationCheckoutOut(
        provider=provider.name, is_mock=provider.is_mock, session_id=current.id, checkout_url=current.url,
        amount=registration.amount_paise, currency="INR",
    )


def _ledger_actor(event: Event) -> uuid.UUID:
    """Ledger entries need a club user. A guest's payment is posted under the event's lead."""
    return event.lead_id or event.created_by


async def record_payment(db: AsyncSession, paid: CheckoutSession, now: datetime | None = None) -> str:
    """Apply a checkout session as the provider reports it. Safe to call any number of times.

    Returns registered, refund_required, already_recorded, not_paid, amount_mismatch or unknown.
    """
    now = now or utcnow()
    event_id = await db.scalar(select(R.event_id).where(R.checkout_session_id == paid.id))
    if event_id is None:
        return "unknown"
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update())
    registration = await db.scalar(select(R).where(R.checkout_session_id == paid.id).with_for_update())
    if registration.paid_at is not None:
        return "already_recorded"
    if paid.payment_status != "paid" or not paid.payment_intent_id:
        return "not_paid"
    if paid.amount_total != registration.amount_paise or (paid.currency or "").lower() != "inr":
        logger.error("Registration %s: provider reported a different amount or currency", registration.id)
        return "amount_mismatch"

    await _expire_holds(db, event.id, now)
    await db.refresh(registration)
    registration.paid_at, registration.payment_intent_id, registration.updated_at = now, paid.payment_intent_id, now
    honoured = event.status == "published" and event.starts_at > now
    if honoured and registration.status != "pending_payment":
        # Paid after the hold ran out: the place is given back only if one is still free.
        taken = (await _taken(db, [event.id], now)).get(event.id, 0)
        clash = await _duplicates(db, event.id, registration.email, registration.student_id, skip=registration.id)
        honoured = registration.status == "expired" and taken < (event.max_participants or 0) and not clash
    if honoured:
        registration.status, registration.hold_expires_at = "registered", None
    else:
        if registration.status == "pending_payment":
            registration.status, registration.hold_expires_at = "expired", None
        registration.refund_status = "required"
    await db.flush()
    # The money did arrive, so the ledger says so, once.
    await ledger_service.record_income(
        db, club_id=event.club_id, source_module="event_ticket", amount=rupees(registration.amount_paise),
        created_by=_ledger_actor(event), reference_id=registration.id, reference_type="event_registration",
        event_id=event.id, category="event_registration",
        description=f"Registration {registration.participant_code} for {event.name}",
        idempotency_key=f"{paid.payment_intent_id}:income",
    )
    audit_service.record(
        db, club_id=event.club_id, actor_id=None, action="registration.payment_recorded",
        entity_type="event_registration", entity_id=registration.id,
        details={"paise": registration.amount_paise, "outcome": registration.status},
    )
    return "registered" if honoured else "refund_required"


async def process_refund(db: AsyncSession, registration_id: uuid.UUID, now: datetime | None = None) -> str | None:
    """Send back a payment marked for refund. Call after committing: no locks are held for the provider."""
    now = now or utcnow()
    registration = await db.get(R, registration_id)
    if registration is None or registration.refund_status not in ("required", "failed") or not registration.payment_intent_id:
        return None
    intent, amount = registration.payment_intent_id, registration.amount_paise
    result = await run_in_threadpool(
        payment_service.get_provider().refund, intent, amount, f"event_registration:{registration_id}:refund"
    )
    registration = await db.scalar(select(R).where(R.id == registration_id).with_for_update())
    if result.status == "succeeded" and registration.refund_status != "succeeded":
        event = await db.get(Event, registration.event_id)
        registration.refund_status, registration.updated_at = "succeeded", now
        await ledger_service.record_refund(
            db, club_id=event.club_id, source_module="event_ticket", amount=rupees(amount),
            created_by=_ledger_actor(event), reference_id=registration.id, reference_type="event_registration",
            event_id=event.id, category="event_registration",
            description=f"Refund of registration {registration.participant_code} for {event.name}",
            idempotency_key=f"{intent}:refund",
        )
    elif result.status == "failed":
        registration.refund_status, registration.updated_at = "failed", now
    await db.flush()
    return result.status


async def verify_payment(db: AsyncSession, token: str, session_id: str, now: datetime | None = None) -> tuple[uuid.UUID, str]:
    """The participant is back from the payment page: ask the provider what happened."""
    registration, _ = await by_token(db, token)
    if not session_id or registration.checkout_session_id != session_id:
        raise not_found("Checkout session")
    paid = await run_in_threadpool(payment_service.get_provider().verify_payment, session_id)
    if paid is None:
        raise conflict("session_expired", "This payment page has expired. Start the payment again.")
    return registration.id, await record_payment(db, paid, now)


async def handle_webhook(db: AsyncSession, body: bytes, signature: str, now: datetime | None = None) -> tuple[uuid.UUID | None, str]:
    """A signed provider event. Only completed checkouts for registrations are acted on."""
    provider = payment_service.get_provider()
    try:
        event = provider.handle_webhook(body, signature)
    except payment_service.InvalidWebhookSignature:
        raise AppError(400, "invalid_signature", "Webhook signature could not be verified") from None
    metadata = event.data.get("metadata") or {}
    if event.type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded") \
            or "event_registration_id" not in metadata:
        return None, "ignored"
    paid = await run_in_threadpool(provider.verify_payment, str(event.data.get("id", "")))
    if paid is None:
        return None, "ignored"
    registration_id = await db.scalar(select(R.id).where(R.checkout_session_id == paid.id))
    return registration_id, await record_payment(db, paid, now)


async def simulate_mock_payment(db: AsyncSession, token: str, succeed: bool) -> tuple[uuid.UUID, str]:
    """Mock provider only: stand in for the participant on the payment page."""
    provider = payment_service.get_provider()
    if not isinstance(provider, payment_service.MockPaymentProvider):
        raise not_found("Mock payments")
    registration, _ = await by_token(db, token)
    if not registration.checkout_session_id:
        raise conflict("no_checkout", "Start the payment first.")
    paid = provider.simulate_payment(registration.checkout_session_id, succeed)
    if paid is None:
        raise conflict("session_expired", "This payment page has expired. Start the payment again.")
    return registration.id, await record_payment(db, paid) if succeed else "not_paid"


# --- For organisers ---------------------------------------------------------------------


async def list_for_event(db: AsyncSession, user: CurrentUser, event: Event, now: datetime | None = None) -> dict:
    """The participant list, for whoever manages the event."""
    if not event_service.can_manage(user, event):
        raise forbidden("Only the president or this event's lead can see who has registered")
    now = now or utcnow()
    rows = list(await db.scalars(select(R).where(R.event_id == event.id).order_by(R.participant_no)))
    taken = (await _taken(db, [event.id], now)).get(event.id, 0)
    return {"registrations": rows, "taken": taken, "limit": event.max_participants,
            "checked_in": sum(row.status == "checked_in" for row in rows)}


async def cancel(db: AsyncSession, user: CurrentUser, registration_id: uuid.UUID, now: datetime | None = None) -> EventRegistration:
    """An organiser removes a participant, freeing the place. A paid one is marked for refund."""
    now = now or utcnow()
    event_id = await db.scalar(select(R.event_id).where(R.id == registration_id))
    event = await db.scalar(select(Event).where(Event.id == event_id).with_for_update()) if event_id else None
    if event is None or event.club_id != user.club_id:
        raise not_found("Registration")
    if not event_service.can_manage(user, event):
        raise forbidden("Only the president or this event's lead can cancel a registration")
    registration = await db.scalar(select(R).where(R.id == registration_id).with_for_update())
    if registration.status == "checked_in":
        raise conflict("already_checked_in", "This participant has already been checked in.")
    if registration.status not in ("pending_payment", "registered"):
        raise conflict("not_active", f"This registration is already {registration.status}.")
    registration.status, registration.hold_expires_at, registration.updated_at = "cancelled", None, now
    if registration.paid_at is not None:
        registration.refund_status = "required"
    await db.flush()
    audit_service.record(
        db, club_id=event.club_id, actor_id=user.id, action="registration.cancelled", entity_type="event_registration",
        entity_id=registration.id, details={"refund": registration.refund_status},
    )
    return registration


# --- Email ------------------------------------------------------------------------------


async def send_ticket_email(registration_id: uuid.UUID, db: AsyncSession | None = None) -> str | None:
    """Email a confirmed participant their ticket. Runs after the response, on its own session."""
    async def run(session: AsyncSession) -> str | None:
        registration = await session.get(R, registration_id)
        if registration is None or registration.status != "registered":
            return None
        event = await session.get(Event, registration.event_id)
        club = await session.get(Club, registration.club_id)
        when = f"{event.starts_at.astimezone(IST):%a %d %b %Y, %I:%M %p} IST"
        message = email_service.ticket_email(
            to=registration.email, full_name=registration.full_name, participant_code=registration.participant_code,
            event_name=event.name, club_name=club.name, when=when, venue=event.venue or "To be announced",
            credential=gatesync_service.credential_for(registration.id), ticket_url=ticket_url(registration.id),
            amount=money(registration.amount_paise),
        )
        outcome = await run_in_threadpool(email_service.deliver, message)
        # Recorded as it happened, so the ticket page never claims an email that was not sent.
        registration.email_status = outcome
        if outcome == "sent":
            registration.email_sent_at = utcnow()
        await session.commit()
        return outcome

    try:
        if db is not None:
            return await run(db)
        async with SessionLocal() as session:
            return await run(session)
    except Exception:  # a failed email must never surface as a failed registration
        logger.exception("Ticket email for registration %s failed", registration_id)
        return None


# --- Retention --------------------------------------------------------------------------

_last_purge: datetime | None = None


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete participants of events that ended more than REGISTRATION_RETENTION_DAYS ago.

    Names, phone numbers, emails, student IDs and QR records go. Ledger entries stay: they
    are the club's accounts and hold no participant details beyond the participant code.
    """
    now = now or utcnow()
    cutoff = now - timedelta(days=get_settings().registration_retention_days)
    ended = select(Event.id).where(func.coalesce(Event.ends_at, Event.starts_at) < cutoff)
    result = await db.execute(delete(R).where(R.event_id.in_(ended)))
    return result.rowcount or 0


async def purge_if_due(db: AsyncSession, now: datetime | None = None) -> None:
    """Run the purge at most once a day, from whichever request comes first."""
    global _last_purge
    now = now or utcnow()
    if _last_purge is not None and now - _last_purge < timedelta(days=1):
        return
    _last_purge = now
    removed = await purge_expired(db, now)
    await db.commit()
    if removed:
        logger.info("Deleted %s registrations past their retention period", removed)


# --- Telling participants about a change ------------------------------------------------


async def notify_participants(event_id: uuid.UUID, heading: str, intro: str) -> int:
    """Email everyone holding a place at the event that something about it changed. Runs after the
    response, on its own session; a mail failure never undoes the change. Returns how many were written."""
    try:
        async with SessionLocal() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return 0
            club = await session.get(Club, event.club_id)
            lead = await session.get(User, event.lead_id) if event.lead_id else None
            people = list(await session.scalars(select(R).where(R.event_id == event.id, R.status.in_(("registered", "pending_payment")))))
            when = f"{event.starts_at.astimezone(IST):%a %d %b %Y, %I:%M %p} IST" if event.starts_at else "To be announced"
            for person in people:
                message = email_service.notice_email(
                    to=person.email, full_name=person.full_name, club_name=club.name,
                    subject=f"Update: {event.name}", heading=heading, intro=intro,
                    rows=[("Event", event.name), ("When", when), ("Where", event.venue or "To be announced"),
                          ("Organised by", lead.full_name if lead else "The club"), ("Your participant ID", person.participant_code)],
                    button="Open your ticket", url=ticket_url(person.id),
                )
                await run_in_threadpool(email_service.deliver, message)
            return len(people)
    except Exception:
        logger.exception("Could not notify participants of event %s", event_id)
        return 0
