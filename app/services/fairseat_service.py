"""FairSeat: holds and bookings, the reservation side of ticketing.

The one rule everything here protects: for each ticket category,

    seats in use = confirmed seats + seats on a hold that has not run out
    seats in use <= capacity

and, across an event, the seats in use never exceed the event's ticket capacity.
(The count itself is ticket_inventory's; the same one the event side uses.)

Locking. Anything that can change the seats in use first takes the ticket locks in the
one agreed order (ticket_locks): the event row, its ticket settings row, the category
rows in id order, and only then booking rows. One event's bookings therefore happen one
at a time, and the seat count is always taken while holding those locks. The locks last
until the route commits or rolls back.

Transactions. Like the rest of the app, functions here flush and never commit. The
route commits once, so a booking, its idempotency record and its ticket rows are
stored together or not at all. When a function raises, nothing it did is kept.

Holds. A hold stops counting the moment its deadline passes, whether or not anything
has marked it expired yet. `expire_holds` and the booking path only tidy the status.

Ticket settings and categories are written in one place, ticket_service.save_config.
The category routes here are a second door to that same writer.

Payment, ledger entries and refunds are fairseat_payments. Not built: QR credentials, waitlists.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.models.fairseat import EventTicket, TicketBooking, TicketBookingCommand, TicketCategory, TicketQuote
from app.models.shared import Event
from app.models.tickets import EventTicketSettings
from app.schemas.fairseat import (
    BookingOut,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    QuoteRequest,
    ReservationRequest,
    TicketOut,
)
from app.schemas.tickets import TicketCategoryIn, TicketConfigIn
from app.services import (
    audit_service,
    event_service,
    fairseat_pricing,
    ticket_inventory,
    ticket_locks,
    ticket_service,
)

B = TicketBooking


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Counting --------------------------------------------------------------------------

occupied_seats = ticket_inventory.occupied_seats   # the single definition of "seats in use"
_in_use = ticket_inventory.in_use


async def _seats_held_by(session: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID, now: datetime) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(B.quantity), 0)).where(
            B.event_id == event_id, B.purchaser_user_id == user_id, _in_use(now)
        )
    )
    return int(total)


async def _mark_expired(session: AsyncSession, now: datetime, *conditions) -> int:
    result = await session.execute(
        update(B)
        .where(B.status == "pending_payment", B.hold_expires_at <= now, *conditions)
        .values(status="expired", hold_expires_at=None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


# --- Presenting ------------------------------------------------------------------------


def _why_not_on_sale(
    event: Event, settings: EventTicketSettings | None, category: TicketCategory, now: datetime
) -> tuple[str, str] | None:
    """None when this category can be booked right now; otherwise an error code and message."""
    if event.status != "published":
        return "event_not_bookable", f"This event is {event.status} and is not taking bookings."
    if event.starts_at is None:
        return "event_not_bookable", "This event has no start time yet, so it cannot be booked."
    if settings is None or not settings.sales_enabled:
        return "sales_not_enabled", "Ticket sales are not switched on for this event."
    if not category.active:
        return "category_closed", "This ticket category is not on sale."
    # The category's own window, never wider than the event's.
    opens, closes = ticket_service.effective_window(settings, category)
    if now < opens:
        return "booking_not_open", "Booking for this ticket category has not opened yet."
    if now >= closes or now >= event.starts_at:
        return "booking_closed", "Booking for this ticket category has closed."
    return None


def _category_out(
    event: Event, settings: EventTicketSettings | None, category: TicketCategory, occupied: int, now: datetime
) -> CategoryOut:
    opens, closes = ticket_service.effective_window(settings, category) if settings else (
        category.sales_open_at, category.sales_close_at)
    return CategoryOut(
        id=category.id, event_id=category.event_id, name=category.name, description=category.description,
        base_price_paise=category.base_price_paise, pricing_version=category.pricing_version,
        member_discount_eligible=category.member_discount_eligible, sort_order=category.sort_order,
        capacity=category.capacity, occupied=occupied, available=max(category.capacity - occupied, 0),
        sales_open_at=opens, sales_close_at=closes, active=category.active,
        on_sale=_why_not_on_sale(event, settings, category, now) is None,
    )


async def _present(
    session: AsyncSession, bookings: list[TicketBooking], now: datetime, *, replayed: bool = False
) -> list[BookingOut]:
    if not bookings:
        return []
    tickets: dict[uuid.UUID, list[TicketOut]] = {}
    rows = await session.scalars(
        select(EventTicket)
        .where(EventTicket.booking_id.in_([booking.id for booking in bookings]))
        .order_by(EventTicket.seat_number)
    )
    for ticket in rows:
        tickets.setdefault(ticket.booking_id, []).append(TicketOut.model_validate(ticket))
    statuses = dict(
        (await session.execute(select(Event.id, Event.status).where(Event.id.in_({b.event_id for b in bookings})))).all()
    )
    out = []
    for booking in bookings:
        # A hold whose deadline has passed is expired, even if nothing has tidied its status yet.
        lapsed = booking.status == "pending_payment" and booking.hold_expires_at <= now
        out.append(BookingOut(
            replayed=replayed, booking_id=booking.id, event_id=booking.event_id,
            event_status=statuses[booking.event_id], ticket_category_id=booking.ticket_category_id,
            purchaser_user_id=booking.purchaser_user_id,
            status="expired" if lapsed else booking.status, payment_status=booking.payment_status,
            quantity=booking.quantity, unit_price_paise=booking.unit_price_paise,
            total_price_paise=booking.total_price_paise, currency=booking.currency,
            discount_applied=booking.discount_applied, discount_explanation=booking.discount_explanation,
            hold_expires_at=None if lapsed else booking.hold_expires_at,
            paid_at=booking.paid_at, refund_status=booking.refund_status, refund_reason=booking.refund_reason,
            tickets=tickets.get(booking.id, []), created_at=booking.created_at,
        ))
    return out


# --- Categories ------------------------------------------------------------------------
# A second door to the one category writer, ticket_service.save_config. Each call sends that
# writer the event's whole configuration with one category added or changed, so the partition,
# window and lock rules are exactly those of the organizer page.


async def _managed_event(session: AsyncSession, user: CurrentUser, event_id: uuid.UUID) -> Event:
    event = await ticket_locks.lock_event(session, event_id)
    if event is None or event.club_id != user.club_id:
        raise not_found("Event")
    if not event_service.can_manage(user, event):
        raise forbidden("Only the president or this event's lead can set up its tickets")
    return event


async def _current_config(
    session: AsyncSession, event: Event
) -> tuple[EventTicketSettings, list[TicketCategory]]:
    settings = await ticket_locks.lock_settings(session, event.id)
    if settings is None:
        raise conflict(
            "ticketing_not_configured",
            "Set this event's ticket capacity and booking window first (PUT /events/{id}/tickets).",
        )
    categories = sorted((await ticket_locks.lock_event_categories(session, event.id)).values(),
                        key=lambda c: (c.sort_order, c.name))
    return settings, categories


def _as_input(category: TicketCategory, **changes) -> TicketCategoryIn:
    """An existing category as the writer's input. Fields that are left out keep their values."""
    fields = {
        "id": category.id, "name": category.name, "description": category.description,
        "price_paise": category.base_price_paise, "capacity": category.capacity,
        "member_discount_eligible": category.member_discount_eligible, "is_visible": category.active,
    }
    return TicketCategoryIn(**(fields | changes))


async def _save(
    session: AsyncSession, user: CurrentUser, event: Event, settings: EventTicketSettings,
    items: list[TicketCategoryIn], now: datetime,
) -> list[TicketCategory]:
    return await ticket_service.save_config(session, user, event, TicketConfigIn(
        sales_enabled=settings.sales_enabled, capacity=settings.capacity, opens_at=settings.opens_at,
        closes_at=settings.closes_at, max_per_booking=settings.max_per_booking, categories=items,
    ), now)


def _from_request(changes: dict) -> dict:
    """This API's field names, as the writer's."""
    renamed = {"base_price_paise": "price_paise", "active": "is_visible"}
    return {renamed.get(field, field): value for field, value in changes.items()}


async def create_category(
    session: AsyncSession, user: CurrentUser, data: CategoryCreate, now: datetime | None = None
) -> CategoryOut:
    now = now or utcnow()
    event = await _managed_event(session, user, data.event_id)
    settings, categories = await _current_config(session, event)
    try:
        new = TicketCategoryIn(**_from_request(data.model_dump(exclude={"event_id"}, exclude_unset=True)))
    except ValidationError as exc:
        raise bad_request("invalid_category", "; ".join(error["msg"] for error in exc.errors())) from exc
    if any(c.name.strip().lower() == new.name.strip().lower() for c in categories):
        raise conflict("duplicate_category", f"This event already has a ticket category called {new.name}")
    saved = await _save(session, user, event, settings, [_as_input(c) for c in categories] + [new], now)
    return _category_out(event, settings, saved[-1], 0, now)


async def update_category(
    session: AsyncSession, user: CurrentUser, category_id: uuid.UUID, data: CategoryUpdate, now: datetime | None = None
) -> CategoryOut:
    now = now or utcnow()
    event_id = await session.scalar(select(TicketCategory.event_id).where(TicketCategory.id == category_id))
    if event_id is None:
        raise not_found("Ticket category")
    event = await _managed_event(session, user, event_id)
    settings, categories = await _current_config(session, event)
    changes = _from_request(data.model_dump(exclude_unset=True, exclude_none=True))
    try:
        items = [_as_input(c, **(changes if c.id == category_id else {})) for c in categories]
    except ValidationError as exc:
        raise bad_request("invalid_category", "; ".join(error["msg"] for error in exc.errors())) from exc
    names = [item.name.strip().lower() for item in items]
    if len(set(names)) != len(names):
        raise conflict("duplicate_category", f"This event already has a ticket category called {changes['name']}")
    saved = await _save(session, user, event, settings, items, now)
    category = next(c for c in saved if c.id == category_id)
    occupied = (await occupied_seats(session, [category_id], now))[category_id]
    return _category_out(event, settings, category, occupied, now)


async def list_categories(
    session: AsyncSession, user: CurrentUser, event_id: uuid.UUID, now: datetime | None = None
) -> list[CategoryOut]:
    now = now or utcnow()
    event = await event_service.get_visible_event(session, user, event_id)
    settings = await ticket_service.get_settings_row(session, event.id)
    categories = list(await session.scalars(
        select(TicketCategory).where(TicketCategory.event_id == event.id)
        .order_by(TicketCategory.sort_order, TicketCategory.name, TicketCategory.id)
    ))
    counts = await occupied_seats(session, [category.id for category in categories], now)
    return [_category_out(event, settings, category, counts[category.id], now) for category in categories]


# --- Quotes ----------------------------------------------------------------------------


def _check_quantity(quantity: int, settings: EventTicketSettings | None = None) -> None:
    """The app-wide limit, tightened by the event's own `max_per_booking` when it is smaller."""
    limit = get_settings().fairseat_max_per_booking
    if settings is not None:
        limit = min(limit, settings.max_per_booking)
    if quantity > limit:
        raise bad_request("invalid_quantity", f"You can book at most {limit} tickets at a time", {"limit": limit})


async def _event_and_category(
    session: AsyncSession, user: CurrentUser, event_id: uuid.UUID, category_id: uuid.UUID, *, lock: bool
) -> tuple[Event, EventTicketSettings | None, TicketCategory]:
    """The event, its ticket settings and the category, from the database, for someone in the
    event's club. The club comes from the signed-in user's own record, never from the request.
    With lock=True they are locked in the agreed order: event, settings, category."""
    if lock:
        event, settings, categories = await ticket_locks.lock_for_event(session, event_id, [category_id])
        category = categories.get(category_id)
    else:
        event = await session.get(Event, event_id)
        settings = await ticket_service.get_settings_row(session, event_id) if event else None
        category = await session.get(TicketCategory, category_id)
    if event is None or event.club_id != user.club_id:
        raise not_found("Event")
    if category is None or category.event_id != event.id:
        raise not_found("Ticket category")
    return event, settings, category


async def create_quote(
    session: AsyncSession, user: CurrentUser, data: QuoteRequest, now: datetime | None = None
) -> TicketQuote:
    now = now or utcnow()
    _check_quantity(data.quantity)
    event, settings, category = await _event_and_category(
        session, user, data.event_id, data.ticket_category_id, lock=False)
    closed = _why_not_on_sale(event, settings, category, now)
    if closed:
        raise conflict(*closed)
    _check_quantity(data.quantity, settings)
    return await fairseat_pricing.issue_quote(
        session, user_id=user.id, event=event, category=category, quantity=data.quantity, now=now
    )


async def _accepted_quote(
    session: AsyncSession, user: CurrentUser, data: ReservationRequest, event: Event, category: TicketCategory,
    now: datetime,
) -> TicketQuote:
    """The stored quote this request refers to, after checking it still holds. Every amount
    used for the booking is read from this row; the request carries none."""
    quote = await session.get(TicketQuote, data.accepted_quote_id)
    matches = (
        quote is not None
        and quote.purchaser_user_id == user.id
        and quote.event_id == event.id
        and quote.ticket_category_id == category.id
        and quote.quantity == data.quantity
        and quote.issued_at <= now
    )
    if not matches:
        raise conflict("invalid_quote", "This price quote does not match your request. Ask for a new one.")
    if now >= quote.expires_at:
        raise conflict("quote_expired", "This price quote has expired. Ask for a new one.")
    current = await fairseat_pricing.price_for(session, user_id=user.id, event=event, category=category, now=now)
    still_current = (
        quote.calculator_version == fairseat_pricing.CALCULATOR_VERSION
        and quote.category_pricing_version == category.pricing_version
        and quote.base_unit_price_paise == category.base_price_paise
        and quote.unit_price_paise == current.price_paise
        and quote.applied_discount_bps == current.applied_discount_bps
    )
    if not still_current:
        raise conflict("quote_stale", "The price has changed since this quote was made. Ask for a new one.")
    return quote


# --- Booking ---------------------------------------------------------------------------


def _fingerprint(data: ReservationRequest) -> str:
    canonical = json.dumps(
        {
            "event_id": str(data.event_id),
            "ticket_category_id": str(data.ticket_category_id),
            "quantity": data.quantity,
            "accepted_quote_id": str(data.accepted_quote_id),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _new_tickets(booking: TicketBooking) -> list[EventTicket]:
    return [
        EventTicket(id=uuid.uuid4(), booking_id=booking.id, event_id=booking.event_id,
                    holder_user_id=booking.purchaser_user_id, seat_number=seat, status="pending_issue")
        for seat in range(1, booking.quantity + 1)
    ]


async def reserve(
    session: AsyncSession, user: CurrentUser, data: ReservationRequest, now: datetime | None = None
) -> BookingOut:
    """Hold seats (or confirm them outright when they are free). Runs under the event, settings
    and category locks; the caller commits. Repeating a request with the same idempotency key
    returns the booking it made the first time."""
    now = now or utcnow()
    settings = get_settings()
    _check_quantity(data.quantity)
    event, ticket_settings, category = await _event_and_category(
        session, user, data.event_id, data.ticket_category_id, lock=True)

    # Looked up under the lock, so two copies of one request cannot both find "no earlier request".
    fingerprint = _fingerprint(data)
    earlier = await session.scalar(
        select(TicketBookingCommand).where(
            TicketBookingCommand.purchaser_user_id == user.id,
            TicketBookingCommand.event_id == event.id,
            TicketBookingCommand.idempotency_key == data.idempotency_key,
        )
    )
    if earlier is not None:
        if earlier.request_fingerprint != fingerprint:
            raise conflict("idempotency_conflict", "This idempotency key was already used for a different request.")
        booking = await session.get(TicketBooking, earlier.booking_id)
        return (await _present(session, [booking], now, replayed=True))[0]

    closed = _why_not_on_sale(event, ticket_settings, category, now)
    if closed:
        raise conflict(*closed)
    _check_quantity(data.quantity, ticket_settings)
    quote = await _accepted_quote(session, user, data, event, category, now)
    await _mark_expired(session, now, B.event_id == event.id)

    held = await _seats_held_by(session, event.id, user.id, now)
    limit = settings.fairseat_max_per_purchaser
    if held + data.quantity > limit:
        raise conflict(
            "purchaser_limit", f"You can hold at most {limit} tickets for this event and already have {held}.",
            {"limit": limit, "held": held, "requested": data.quantity},
        )
    # Both limits, counted under the locks: the category's own seats, and the event's ticket
    # capacity across all of its categories. (The partition makes the second follow from the
    # first, but neither is taken on trust.)
    event_categories = list(await session.scalars(select(TicketCategory.id).where(TicketCategory.event_id == event.id)))
    counts = await occupied_seats(session, event_categories, now)
    available = min(category.capacity - counts[category.id], ticket_settings.capacity - sum(counts.values()))
    if available < data.quantity:
        raise conflict(
            "sold_out",
            "This ticket category is sold out." if available <= 0 else f"Only {available} tickets are left.",
            {"available": max(available, 0), "requested": data.quantity, "waitlist": "unavailable"},
        )

    free = quote.total_price_paise == 0
    booking = TicketBooking(
        id=uuid.uuid4(), event_id=event.id, ticket_category_id=category.id, purchaser_user_id=user.id,
        quote_id=quote.id, quantity=data.quantity, unit_price_paise=quote.unit_price_paise,
        total_price_paise=quote.total_price_paise, currency=settings.currency,
        member_eligible=quote.member_eligible, applied_discount_bps=quote.applied_discount_bps,
        discount_applied=quote.discount_applied, discount_reason=quote.discount_reason,
        discount_explanation=quote.discount_explanation,
        status="confirmed" if free else "pending_payment",
        payment_status="not_required" if free else "pending",
        hold_expires_at=None if free else now + timedelta(seconds=settings.fairseat_hold_seconds),
        created_at=now, updated_at=now,
    )
    session.add(booking)
    await session.flush()
    if free:
        session.add_all(_new_tickets(booking))
    session.add(TicketBookingCommand(
        id=uuid.uuid4(), purchaser_user_id=user.id, event_id=event.id, idempotency_key=data.idempotency_key,
        request_fingerprint=fingerprint, booking_id=booking.id,
    ))
    await session.flush()
    audit_service.record(
        session, club_id=event.club_id, actor_id=user.id, action="fairseat.booking_created",
        entity_type="ticket_booking", entity_id=booking.id,
        details={"category_id": str(category.id), "quantity": booking.quantity,
                 "total_price_paise": booking.total_price_paise, "status": booking.status},
    )
    return (await _present(session, [booking], now))[0]


async def _revoke_tickets(session: AsyncSession, booking_id: uuid.UUID, now: datetime, *, even_if_used: bool = False) -> None:
    used = await session.scalar(
        select(func.count()).select_from(EventTicket)
        .where(EventTicket.booking_id == booking_id, EventTicket.status == "checked_in")
    )
    if used and not even_if_used:
        raise conflict("ticket_already_used", "A ticket from this booking has already been used at the door.")
    await session.execute(
        update(EventTicket)
        .where(EventTicket.booking_id == booking_id, EventTicket.status != "revoked")
        .values(status="revoked", revoked_at=now)
        .execution_options(synchronize_session=False)
    )


async def _release(
    session: AsyncSession, booking: TicketBooking, event: Event, now: datetime, *, reason: str = "cancelled_by_purchaser",
    by_organiser: bool = False,
) -> None:
    """Give a locked booking's seats back. The caller holds the event, settings and category locks.

    A paid booking is cancelled here and marked as owing a refund; the money is sent back
    afterwards, outside the locks, by fairseat_payments.process_refund. It is not shown as
    refunded until the provider confirms it.
    """
    if booking.status == "pending_payment":
        booking.status = "expired" if booking.hold_expires_at <= now else "cancelled"
    elif booking.status == "confirmed" and booking.payment_status == "not_required":
        await _revoke_tickets(session, booking.id, now, even_if_used=by_organiser)
        booking.status = "cancelled"
    elif booking.status == "confirmed":
        if not get_settings().fairseat_payments_enabled:
            raise conflict("refund_required", "A paid booking is cancelled through a refund, which is not switched on.")
        if not by_organiser and event.starts_at is not None and now >= event.starts_at:
            raise conflict("event_started", "This event has already started, so the booking can no longer be cancelled.")
        await _revoke_tickets(session, booking.id, now, even_if_used=by_organiser)
        booking.status = "cancelled"
        booking.refund_status = "required"
        booking.refund_reason = reason
    else:
        return  # already cancelled or expired: nothing to give back
    booking.hold_expires_at = None
    booking.updated_at = now
    if booking.status == "cancelled":
        booking.cancelled_at = now


async def cancel_booking(
    session: AsyncSession, user: CurrentUser, booking_id: uuid.UUID, now: datetime | None = None
) -> BookingOut:
    """The purchaser gives up a booking. Cancelling again changes nothing. For a paid booking
    the caller must then run fairseat_payments.process_refund, after committing."""
    now = now or utcnow()
    found = (await session.execute(
        select(B.event_id, B.ticket_category_id, B.purchaser_user_id).where(B.id == booking_id)
    )).first()
    if found is None or found.purchaser_user_id != user.id:
        raise not_found("Booking")
    # Same lock order as booking: event, settings, category, then the booking row itself.
    event, _, _ = await ticket_locks.lock_for_event(session, found.event_id, [found.ticket_category_id])
    booking = await ticket_locks.lock_booking(session, booking_id)
    before = booking.status
    await _release(session, booking, event, now)
    await session.flush()
    if booking.status != before:
        audit_service.record(
            session, club_id=user.club_id, actor_id=user.id, action="fairseat.booking_released",
            entity_type="ticket_booking", entity_id=booking.id, details={"from": before, "to": booking.status},
        )
    return (await _present(session, [booking], now))[0]


async def expire_holds(session: AsyncSession, now: datetime | None = None, batch_size: int = 200) -> int:
    """Mark holds past their deadline as expired. Events are taken in id order and, within each,
    the locks in the agreed order, so two sweeps (or a sweep and a booking) cannot deadlock.
    The caller commits."""
    now = now or utcnow()
    due = (await session.execute(
        select(B.event_id, B.ticket_category_id)
        .where(B.status == "pending_payment", B.hold_expires_at <= now)
        .order_by(B.hold_expires_at, B.id)
        .limit(batch_size)
    )).all()
    by_event: dict[uuid.UUID, set[uuid.UUID]] = {}
    for event_id, category_id in due:
        by_event.setdefault(event_id, set()).add(category_id)
    expired = 0
    for event_id in sorted(by_event):
        await ticket_locks.lock_for_event(session, event_id, by_event[event_id])
        expired += await _mark_expired(session, now, B.event_id == event_id)
    return expired


# --- Reading bookings ------------------------------------------------------------------


async def list_my_bookings(session: AsyncSession, user: CurrentUser, now: datetime | None = None) -> list[BookingOut]:
    bookings = list(await session.scalars(
        select(B).where(B.purchaser_user_id == user.id).order_by(B.created_at.desc(), B.id)
    ))
    return await _present(session, bookings, now or utcnow())


async def get_booking(
    session: AsyncSession, user: CurrentUser, booking_id: uuid.UUID, now: datetime | None = None
) -> BookingOut:
    """Your own booking, or one for an event you manage."""
    booking = await session.get(TicketBooking, booking_id)
    if booking is not None and booking.purchaser_user_id != user.id:
        event = await session.get(Event, booking.event_id)
        if not event_service.can_manage(user, event):
            booking = None
    if booking is None:
        raise not_found("Booking")
    return (await _present(session, [booking], now or utcnow()))[0]


# --- For the event module --------------------------------------------------------------
# Called by ticket_service.guard_status_change, inside the event's own status transaction.


async def release_bookings_for_event(
    session: AsyncSession, event_id: uuid.UUID, now: datetime | None = None
) -> list[uuid.UUID]:
    """Call when an event is cancelled, in the same transaction as the status change.

    Gives back every hold and booking and revokes their tickets. Returns the ids of paid
    bookings, which are now marked as owing a refund: after committing, the caller passes
    each to fairseat_payments.process_refund.

    If ticket payments are switched off and a paid booking exists, it refuses and changes
    nothing, because there would be no way to return the money.
    """
    now = now or utcnow()
    event, _, _ = await ticket_locks.lock_for_event(session, event_id)  # event, settings, every category
    bookings = list(await session.scalars(
        select(B).where(B.event_id == event_id, B.status.in_(("pending_payment", "confirmed")))
        .order_by(B.id).with_for_update().execution_options(populate_existing=True)
    ))
    paid = [booking for booking in bookings if booking.payment_status == "paid"]
    if paid and not get_settings().fairseat_payments_enabled:
        raise conflict(
            "paid_bookings_exist",
            f"{len(paid)} paid booking(s) must be refunded before this event can be cancelled.",
            {"paid_bookings": len(paid)},
        )
    for booking in bookings:
        await _release(session, booking, event, now, reason="event_cancelled", by_organiser=True)
    await session.flush()
    return [booking.id for booking in paid]
