import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decideBooking } from '../src/modules/fairseat/booking-rules.ts';
import { calculateTicketDiscount } from '../src/modules/membercore/ticket-discount.ts';

const opensAtMs = Date.parse('2026-10-10T09:00:00Z');
const closesAtMs = Date.parse('2026-10-10T11:00:00Z');
const startsAtMs = Date.parse('2026-10-10T12:00:00Z');
const nowMs = Date.parse('2026-10-10T10:00:00Z');
const event = { eventId: 'event-a', capacity: 10, bookedSeats: 9, opensAtMs, closesAtMs, startsAtMs, cancelled: false, waitlistEnabled: false, basePricePaise: 101 };
const pricing = calculateTicketDiscount({ basePricePaise: 101, eventClubId: 'club-a', nowMs, membership: null });
const input = { event, pricing, nowMs };
const decide = (eventChanges = {}, changes = {}) => decideBooking({ ...input, ...changes, event: { ...event, ...eventChanges } });

test('available capacity allows exactly one request at the MemberCore price', () => {
  assert.deepEqual(decide(), { status: 'allowed', reason: 'seat_available', explanation: 'The booking window is open and a seat is available.', pricePaise: 101 });
});
test('exactly full capacity is sold out when waitlist is disabled', () => {
  const result = decide({ bookedSeats: 10 });
  assert.equal(result.status, 'sold_out');
  assert.equal(result.reason, 'capacity_full');
  assert.equal(result.pricePaise, null);
});
test('full capacity permits waitlist when enabled', () => {
  const result = decide({ bookedSeats: 10, waitlistEnabled: true });
  assert.equal(result.status, 'waitlist_eligible');
  assert.equal(result.pricePaise, 101);
});
test('overcapacity is invalid even with waitlist enabled', () => {
  assert.equal(decide({ bookedSeats: 11, waitlistEnabled: true }).reason, 'inconsistent_capacity');
  assert.equal(decide({ bookedSeats: 11 }).status, 'invalid');
});
test('opening is inclusive and closing is exclusive', () => {
  assert.equal(decide({}, { nowMs: opensAtMs - 1 }).reason, 'not_open');
  assert.equal(decide({}, { nowMs: opensAtMs }).status, 'allowed');
  assert.equal(decide({}, { nowMs: closesAtMs - 1 }).status, 'allowed');
  assert.equal(decide({}, { nowMs: closesAtMs }).reason, 'window_closed');
});
test('event start is exclusive including when window ends at start', () => {
  assert.equal(decide({ closesAtMs: startsAtMs }, { nowMs: startsAtMs - 1 }).status, 'allowed');
  assert.equal(decide({ closesAtMs: startsAtMs }, { nowMs: startsAtMs }).reason, 'event_started');
  assert.equal(decide({}, { nowMs: startsAtMs + 1 }).reason, 'event_started');
});
test('cancelled and closed events cannot waitlist even when full', () => {
  assert.equal(decide({ cancelled: true, bookedSeats: 10, waitlistEnabled: true }).reason, 'event_cancelled');
  assert.equal(decide({ bookedSeats: 10, waitlistEnabled: true }, { nowMs: closesAtMs }).status, 'closed');
});
test('valid member pricing uses existing MemberCore rounding unchanged', () => {
  const memberPrice = calculateTicketDiscount({ basePricePaise: 101, eventClubId: 'club-a', nowMs, membership: { status: 'active', clubId: 'club-a', startsAt: '2026-10-01T00:00:00Z', expiresAt: '2026-11-01T00:00:00Z', discountBps: 1500 } });
  assert.equal(decide({}, { pricing: memberPrice }).pricePaise, 86);
});
test('zero capacity and independent event snapshots', () => {
  assert.equal(decide({ capacity: 0, bookedSeats: 0 }).status, 'sold_out');
  assert.equal(decide({ capacity: 0, bookedSeats: 0, waitlistEnabled: true }).status, 'waitlist_eligible');
  assert.equal(decide({ eventId: 'event-b', capacity: 20, bookedSeats: 10 }).status, 'allowed');
});
test('invalid capacity values and unsafe integers', () => {
  for (const value of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '10', null]) {
    assert.equal(decide({ capacity: value }).reason, 'invalid_capacity');
    assert.equal(decide({ bookedSeats: value }).reason, 'invalid_capacity');
  }
  assert.equal(decide({ capacity: Number.MAX_SAFE_INTEGER, bookedSeats: Number.MAX_SAFE_INTEGER - 1 }).status, 'allowed');
});
test('invalid timestamps and window ordering', () => {
  for (const field of ['opensAtMs', 'closesAtMs', 'startsAtMs']) {
    for (const value of [NaN, Infinity, 1.5, Number.MAX_SAFE_INTEGER, 'bad-date', null]) assert.equal(decide({ [field]: value }).reason, 'invalid_time');
  }
  assert.equal(decide({}, { nowMs: NaN }).reason, 'invalid_time');
  assert.equal(decide({ closesAtMs: opensAtMs }).reason, 'invalid_time');
  assert.equal(decide({ opensAtMs: closesAtMs }).reason, 'invalid_time');
  assert.equal(decide({ closesAtMs: startsAtMs + 1 }).reason, 'invalid_time');
});
test('invalid prices and inconsistent pricing results', () => {
  for (const value of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '101', null]) {
    assert.equal(decide({ basePricePaise: value }).reason, 'invalid_price');
    assert.equal(decide({}, { pricing: { ...pricing, pricePaise: value } }).reason, 'invalid_price');
  }
  for (const changes of [{ savingsPaise: 1 }, { appliedDiscountBps: 10001 }, { appliedDiscountBps: 1 }, { discountApplied: true }, { pricePaise: 102 }]) assert.equal(decide({}, { pricing: { ...pricing, ...changes } }).reason, 'invalid_price');
  const maximum = calculateTicketDiscount({ basePricePaise: Number.MAX_SAFE_INTEGER, eventClubId: 'club-a', nowMs, membership: null });
  assert.equal(decide({ basePricePaise: Number.MAX_SAFE_INTEGER }, { pricing: maximum }).pricePaise, Number.MAX_SAFE_INTEGER);
  const free = calculateTicketDiscount({ basePricePaise: 0, eventClubId: 'club-a', nowMs, membership: null });
  assert.equal(decide({ basePricePaise: 0 }, { pricing: free }).pricePaise, 0);
});
test('malformed event and missing inputs return structured invalid decisions', () => {
  assert.equal(decideBooking(null).reason, 'invalid_input');
  assert.equal(decideBooking({}).status, 'invalid');
  for (const changes of [{ eventId: '' }, { eventId: ' event-a' }, { cancelled: 'false' }, { waitlistEnabled: 1 }]) assert.equal(decide(changes).reason, 'invalid_event');
});
test('engine is deterministic, does not mutate inputs and never allocates seats', () => {
  const frozen = Object.freeze({ ...input, event: Object.freeze({ ...event }), pricing: Object.freeze({ ...pricing }) });
  assert.deepEqual(decideBooking(frozen), decideBooking(frozen));
  assert.equal(frozen.event.bookedSeats, 9);
});
