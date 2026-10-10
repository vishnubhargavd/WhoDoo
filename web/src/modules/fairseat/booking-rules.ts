import type { DiscountResult } from '../membercore/ticket-discount.ts';

/** Local rule inputs only: these are not proposed database columns. */
export interface BookingEventSnapshot {
  readonly eventId: string;
  readonly capacity: number;
  readonly bookedSeats: number;
  readonly opensAtMs: number;
  readonly closesAtMs: number;
  readonly startsAtMs: number;
  readonly cancelled: boolean;
  readonly waitlistEnabled: boolean;
  readonly basePricePaise: number;
}

export interface BookingRuleInput {
  readonly event: BookingEventSnapshot;
  readonly nowMs: number;
  /** Trusted MemberCore result for this same event, user and server timestamp. */
  readonly pricing: Readonly<DiscountResult>;
}

export type BookingStatus = 'allowed' | 'closed' | 'sold_out' | 'invalid' | 'waitlist_eligible';
export type BookingReason = 'seat_available' | 'not_open' | 'window_closed'
  | 'event_started' | 'event_cancelled' | 'capacity_full' | 'waitlist_available'
  | 'invalid_input' | 'invalid_event' | 'invalid_capacity' | 'inconsistent_capacity'
  | 'invalid_time' | 'invalid_price';

export interface BookingDecision {
  readonly status: BookingStatus;
  readonly reason: BookingReason;
  readonly explanation: string;
  /** A snapshot quote only; waitlisting does not lock this price. */
  readonly pricePaise: number | null;
}

const explanations: Record<BookingReason, string> = {
  seat_available: 'The booking window is open and a seat is available.',
  not_open: 'The booking window has not opened.',
  window_closed: 'The booking window has closed.',
  event_started: 'The event has already started.',
  event_cancelled: 'The event is cancelled.',
  capacity_full: 'The event is full and its waitlist is disabled.',
  waitlist_available: 'The event is full; the request is eligible to join its waitlist.',
  invalid_input: 'Booking inputs are missing or malformed.',
  invalid_event: 'The event configuration is invalid.',
  invalid_capacity: 'Capacity and booked seats must be nonnegative safe integers.',
  inconsistent_capacity: 'Booked seats exceed the event capacity; the snapshot must be checked.',
  invalid_time: 'Booking timestamps or window ordering are invalid.',
  invalid_price: 'The supplied ticket price or MemberCore result is invalid.'
};

function integer(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 0;
}

function timestamp(value: number): boolean {
  return Number.isSafeInteger(value) && Number.isFinite(new Date(value).getTime());
}

function decision(status: BookingStatus, reason: BookingReason, pricePaise: number | null = null): BookingDecision {
  return { status, reason, explanation: explanations[reason], pricePaise };
}

/**
 * Decide one-ticket requests from trusted server snapshots. This function does
 * not authenticate callers or allocate seats. Never accept browser availability
 * or membership flags as authoritative. Later booking must re-read capacity and
 * membership, then allocate atomically in PostgreSQL. Membership arithmetic stays
 * in MemberCore; the checks here validate the shape and consistency of its result.
 * Open is inclusive; close and event start are exclusive. A window may end at,
 * but never after, event start. Cancelled events cannot accept waitlist requests.
 */
export function decideBooking(input: BookingRuleInput): BookingDecision {
  if (!input || typeof input !== 'object' || !input.event || !input.pricing) {
    return decision('invalid', 'invalid_input');
  }
  const { event, pricing, nowMs } = input;
  if (typeof event.eventId !== 'string' || !event.eventId || event.eventId.trim() !== event.eventId
      || typeof event.cancelled !== 'boolean' || typeof event.waitlistEnabled !== 'boolean') {
    return decision('invalid', 'invalid_event');
  }
  if (!integer(event.capacity) || !integer(event.bookedSeats)) return decision('invalid', 'invalid_capacity');
  if (event.bookedSeats > event.capacity) return decision('invalid', 'inconsistent_capacity');
  if (![nowMs, event.opensAtMs, event.closesAtMs, event.startsAtMs].every(timestamp)
      || event.opensAtMs >= event.closesAtMs || event.closesAtMs > event.startsAtMs) {
    return decision('invalid', 'invalid_time');
  }
  if (!integer(event.basePricePaise) || !integer(pricing.pricePaise) || !integer(pricing.savingsPaise)
      || pricing.pricePaise > event.basePricePaise
      || pricing.savingsPaise !== event.basePricePaise - pricing.pricePaise
      || !integer(pricing.appliedDiscountBps) || pricing.appliedDiscountBps > 10000
      || typeof pricing.eligible !== 'boolean' || typeof pricing.discountApplied !== 'boolean'
      || pricing.discountApplied !== (pricing.eligible && pricing.appliedDiscountBps > 0)
      || (!pricing.eligible && pricing.appliedDiscountBps !== 0)
      || (pricing.appliedDiscountBps === 0 && pricing.pricePaise !== event.basePricePaise)
      || typeof pricing.reason !== 'string' || typeof pricing.explanation !== 'string') {
    return decision('invalid', 'invalid_price');
  }
  if (event.cancelled) return decision('closed', 'event_cancelled');
  if (nowMs >= event.startsAtMs) return decision('closed', 'event_started');
  if (nowMs < event.opensAtMs) return decision('closed', 'not_open');
  if (nowMs >= event.closesAtMs) return decision('closed', 'window_closed');
  if (event.bookedSeats === event.capacity) {
    return event.waitlistEnabled
      ? decision('waitlist_eligible', 'waitlist_available', pricing.pricePaise)
      : decision('sold_out', 'capacity_full');
  }
  return decision('allowed', 'seat_available', pricing.pricePaise);
}
