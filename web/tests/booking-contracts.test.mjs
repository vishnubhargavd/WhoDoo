import test from 'node:test';
import assert from 'node:assert/strict';
import { parseBookingRequest, compareBookingQuotes, isTicketIssuanceEligible } from '../src/modules/fairseat/booking-contracts.ts';
import { calculateTicketDiscount } from '../src/modules/membercore/ticket-discount.ts';

const request = {
  eventId: 'event-1', ticketCategoryId: 'category-1', quantity: 2,
  idempotencyKey: 'retry-key-00000001'
};
const nowMs = Date.parse('2026-10-10T12:00:00Z');
const memberPricing = calculateTicketDiscount({
  basePricePaise: 10001, eventClubId: 'club-1', membership: null, nowMs
});
function quote(changes = {}) {
  const base = {
    quoteId: 'quote-00000001', eventId: 'event-1', ticketCategoryId: 'category-1', quantity: 2,
    baseUnitPricePaise: 10001, unitPricePaise: 10001, totalPricePaise: 20002,
    memberPricing, issuedAtMs: nowMs, expiresAtMs: nowMs + 60_000
  };
  return { ...base, ...changes };
}

test('parses only event/category selectors, quantity and idempotency key', () => {
  const parsed = parseBookingRequest(request);
  assert.equal(parsed.ok, true);
  if (parsed.ok) assert.deepEqual(parsed.value, request);
});

test('rejects malformed and missing event/category identifiers', () => {
  assert.deepEqual(parseBookingRequest(null), { ok: false, error: 'invalid_shape' });
  assert.equal(parseBookingRequest({ ...request, eventId: ' event-1' }).error, 'invalid_event_id');
  assert.equal(parseBookingRequest({ ...request, ticketCategoryId: '' }).error, 'invalid_ticket_category_id');
  assert.equal(parseBookingRequest({ ...request, ticketCategoryId: 'bad\ncategory' }).error, 'invalid_ticket_category_id');
});

test('rejects zero, negative, fractional and unsafe quantities', () => {
  for (const quantity of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, NaN]) {
    assert.equal(parseBookingRequest({ ...request, quantity }).error, 'invalid_quantity');
  }
});

test('rejects malformed idempotency keys', () => {
  for (const idempotencyKey of ['', 'short', 'contains spaces!!', 'x'.repeat(129)]) {
    assert.equal(parseBookingRequest({ ...request, idempotencyKey }).error, 'invalid_idempotency_key');
  }
});

test('browser cannot submit identity, club, membership, capacity, price or status fields', () => {
  for (const field of [
    { purchaserId: 'user-1' }, { supabaseAuthUserId: 'auth-1' }, { clubId: 'club-1' },
    { membership: { status: 'active', discountBps: 5000 } }, { pricePaise: 1 },
    { capacity: 100 }, { bookedSeats: 0 }, { paymentStatus: 'paid' }, { status: 'confirmed' }
  ]) assert.equal(parseBookingRequest({ ...request, ...field }).error, 'unexpected_field');
});

test('does not carry arbitrary browser fields into the parsed request', () => {
  const parsed = parseBookingRequest(request);
  assert.equal(parsed.ok, true);
  if (parsed.ok) assert.deepEqual(Object.keys(parsed.value).sort(), ['eventId', 'idempotencyKey', 'quantity', 'ticketCategoryId']);
});

test('accepts unchanged server-loaded price and MemberCore snapshot', () => {
  assert.equal(compareBookingQuotes(quote(), quote({ quoteId: 'quote-00000002' }), nowMs), 'accepted');
});

test('requires renewed acceptance when price or membership eligibility changes', () => {
  const increasedPricing = calculateTicketDiscount({
    basePricePaise: 12001, eventClubId: 'club-1', membership: null, nowMs
  });
  assert.equal(compareBookingQuotes(quote(), quote({
    baseUnitPricePaise: 12001, unitPricePaise: increasedPricing.pricePaise,
    totalPricePaise: increasedPricing.pricePaise * 2, memberPricing: increasedPricing
  }), nowMs), 'price_changed');
  const eligibleMemberPricing = calculateTicketDiscount({
    basePricePaise: 10001, eventClubId: 'club-1',
    membership: { status: 'active', clubId: 'club-1', startsAt: '2026-01-01T00:00:00Z', expiresAt: '2027-01-01T00:00:00Z', discountBps: 1000 },
    nowMs
  });
  assert.equal(compareBookingQuotes(quote(), quote({ memberPricing: eligibleMemberPricing, unitPricePaise: eligibleMemberPricing.pricePaise, totalPricePaise: eligibleMemberPricing.pricePaise * 2 }), nowMs), 'price_changed');
});

test('returns expired for an accepted quote at its exclusive expiry boundary', () => {
  const expiring = quote({ expiresAtMs: nowMs + 1 });
  assert.equal(compareBookingQuotes(expiring, expiring, nowMs + 1), 'quote_expired');
});

test('rejects malformed or unsafe price quotes', () => {
  assert.equal(compareBookingQuotes(quote({ totalPricePaise: Number.MAX_SAFE_INTEGER }), quote(), nowMs), 'invalid_quote');
  assert.equal(compareBookingQuotes(quote({ quantity: Number.MAX_SAFE_INTEGER }), quote(), nowMs), 'invalid_quote');
  assert.equal(compareBookingQuotes(quote(), quote(), Number.MAX_SAFE_INTEGER + 1), 'invalid_quote');
});

test('ticket issuance requires a confirmed booking and paid or not-required payment', () => {
  assert.equal(isTicketIssuanceEligible({ status: 'confirmed', paymentStatus: 'paid' }), true);
  assert.equal(isTicketIssuanceEligible({ status: 'confirmed', paymentStatus: 'not_required' }), true);
  for (const status of ['pending_payment', 'cancelled', 'expired']) {
    assert.equal(isTicketIssuanceEligible({ status, paymentStatus: 'paid' }), false);
  }
  for (const paymentStatus of ['pending', 'failed', 'refunded']) {
    assert.equal(isTicketIssuanceEligible({ status: 'confirmed', paymentStatus }), false);
  }
});
