"""FairSeat event-side rules (Member 2): ticket configuration checks, sale state and
the lifecycle locks that protect bookings.

Pure functions over plain snapshots; no database access, and the current time is
always passed in (same style as syncsphere and autopilot).

Capacity (team decision: strict partition):
- The event's ticket capacity is split exactly across its ticket categories.
- A category never holds more seats than the event, and the categories together never
  hold more than the event. Holding fewer is allowed only while sales are switched off,
  and is reported as a warning ("unallocated seats").
- Hidden categories still own their seats: hiding pauses sales, it does not delete.

Booking window: bookings are possible only when the event is published, sales are
enabled and opens_at <= now < closes_at (open inclusive, close exclusive, the same as
FairSeat's booking-rules.ts). closes_at may equal, but never pass, the event start.

Locks: once sales have been live on a published event and opens_at has passed,
bookings may exist. From then on, changes that could invalidate them (fewer seats,
removing a category, moving the window open time or the event's times, moving the event
back to draft, cancelling) are refused unless the reservation service reports that no
seats are committed. Until that service is connected, the count is unknown and the
locks fail closed.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

MAX_CAPACITY = 100_000
MAX_PRICE_PAISE = 10_000_000  # ₹1,00,000 per ticket
MAX_PER_BOOKING = 6  # the approved limit: at most 6 tickets in one booking
MAX_NAME_LENGTH = 120  # ticket category names are 1 to 120 characters
MAX_CATEGORIES = 20

# Per category id: seats held or sold. None means "unknown" (reservation service not connected).
Committed = Mapping[uuid.UUID, int] | None


@dataclass(frozen=True)
class CategorySpec:
    id: uuid.UUID | None
    name: str
    price_paise: int
    capacity: int
    is_visible: bool = True


@dataclass(frozen=True)
class ConfigSpec:
    sales_enabled: bool
    capacity: int
    opens_at: datetime
    closes_at: datetime
    categories: tuple[CategorySpec, ...]


@dataclass(frozen=True)
class EventTimes:
    status: str
    starts_at: datetime | None
    ends_at: datetime | None


@dataclass(frozen=True)
class Problem:
    code: str
    message: str


# --- Configuration checks ------------------------------------------------------------


def check_config(config: ConfigSpec, event: EventTimes, now: datetime) -> tuple[list[Problem], list[Problem]]:
    """Return (errors, warnings). Errors make the configuration unsaveable; warnings are shown."""
    errors: list[Problem] = []
    warnings: list[Problem] = []

    if event.status in ("cancelled", "completed"):
        errors.append(Problem("event_closed", f"A {event.status} event's ticket settings can no longer be changed"))
    if event.starts_at is None or event.ends_at is None:
        errors.append(Problem("missing_schedule", "Set the event's start and end before configuring tickets"))
    if config.closes_at <= config.opens_at:
        errors.append(Problem("invalid_window", "Booking must close after it opens"))
    if event.starts_at and config.closes_at > event.starts_at:
        errors.append(Problem("window_after_start", "Booking must close no later than the event starts"))

    allocated = sum(c.capacity for c in config.categories)
    for category in config.categories:
        if category.capacity > config.capacity:
            errors.append(Problem(
                "category_over_capacity",
                f"{category.name} has {category.capacity} seats but the event has only {config.capacity}",
            ))
    if allocated > config.capacity:
        errors.append(Problem(
            "over_allocated", f"The categories hold {allocated} seats but the event has only {config.capacity}"
        ))
    elif allocated < config.capacity:
        unallocated = Problem(
            "unallocated_seats",
            f"{config.capacity - allocated} of {config.capacity} seats are not in any category",
        )
        (errors if config.sales_enabled else warnings).append(unallocated)

    if config.sales_enabled and not config.categories:
        errors.append(Problem("no_categories", "Add at least one ticket category before switching sales on"))

    if not config.sales_enabled:
        warnings.append(Problem("sales_disabled", "Ticket sales are switched off"))
    elif event.status == "draft":
        warnings.append(Problem("not_published", "Tickets go on sale once the event is published"))
    if config.categories and not any(c.is_visible for c in config.categories):
        warnings.append(Problem("all_hidden", "Every category is hidden, so nothing can be booked"))
    if config.closes_at <= now:
        warnings.append(Problem("window_over", "The booking window has already closed"))
    return errors, warnings


def sale_state(
    sales_enabled: bool | None, opens_at: datetime | None, closes_at: datetime | None, event: EventTimes, now: datetime
) -> str:
    """not_configured | off | unpublished | not_open | open | closed"""
    if sales_enabled is None or opens_at is None or closes_at is None:
        return "not_configured"
    if event.status in ("cancelled", "completed"):
        return "closed"
    if not sales_enabled:
        return "off"
    if event.status != "published":
        return "unpublished"
    if now < opens_at:
        return "not_open"
    if now >= closes_at or (event.starts_at is not None and now >= event.starts_at):
        return "closed"
    return "open"


# --- Locks -----------------------------------------------------------------------------


def is_locked(sales_activated_at: datetime | None, opens_at: datetime | None, now: datetime) -> bool:
    """True once bookings may exist: sales went live on a published event and the window opened."""
    return sales_activated_at is not None and opens_at is not None and now >= opens_at


def total(committed: Committed) -> int | None:
    return None if committed is None else sum(committed.values())


def structural_problems(
    old: ConfigSpec, new: ConfigSpec, locked: bool, committed: Committed
) -> list[Problem]:
    """Edits refused because they could invalidate existing bookings."""
    if not locked:
        return []
    problems: list[Problem] = []
    unknown = committed is None
    committed = committed or {}
    if new.capacity < old.capacity and (unknown or new.capacity < sum(committed.values())):
        problems.append(Problem(
            "capacity_locked",
            "Ticket sales have started, so capacity cannot be reduced"
            + (" until booked seats can be checked" if unknown else " below the seats already booked"),
        ))
    if new.opens_at != old.opens_at:
        problems.append(Problem("window_locked", "Ticket sales have started, so the opening time cannot change"))
    kept = {c.id: c for c in new.categories if c.id}
    for previous in old.categories:
        booked = committed.get(previous.id, 0) if previous.id else 0
        current = kept.get(previous.id)
        if current is None and (unknown or booked):
            problems.append(Problem(
                "category_locked", f"Ticket sales have started, so {previous.name} cannot be removed; hide it instead"
            ))
        elif current and current.capacity < previous.capacity and (unknown or current.capacity < booked):
            problems.append(Problem(
                "category_locked", f"Ticket sales have started, so {previous.name}'s seats cannot be reduced"
            ))
    return problems


def status_problem(new_status: str, locked: bool, committed: Committed) -> Problem | None:
    """Moving an event that may have bookings back to draft, or cancelling it, needs the
    booking/refund workflow (Member 3). Completing it is always allowed."""
    if not locked or new_status not in ("draft", "cancelled"):
        return None
    booked = total(committed)
    if booked == 0:
        return None
    if new_status == "draft":
        return Problem("ticket_sales_started", "Tickets may already be booked, so this event cannot go back to draft")
    return Problem(
        "cancellation_requires_refunds",
        "Tickets may already be booked. Cancelling needs the booking and refund workflow, which is not connected yet",
    )


def schedule_problem(
    closes_at: datetime | None, locked: bool, committed: Committed, times_changed: bool, new_starts_at: datetime | None
) -> Problem | None:
    """Rescheduling an event with tickets: blocked once bookings may exist, and the booking
    window must still close by the (new) start."""
    if closes_at is None or not times_changed:
        return None
    if locked and total(committed) != 0:
        return Problem("ticket_sales_started", "Tickets may already be booked, so the event's times cannot change")
    if new_starts_at is not None and closes_at > new_starts_at:
        return Problem(
            "booking_window_conflict",
            "Ticket booking closes after the new start time. Move the booking close time first",
        )
    return None
