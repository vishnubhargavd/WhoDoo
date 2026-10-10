import { test } from 'node:test';
import assert from 'node:assert/strict';
import { calculateTicketDiscount as calculate } from '../src/modules/membercore/ticket-discount.ts';

const membership = { status: 'active', clubId: 'club-a', startsAt: '2026-10-01T00:00:00Z', expiresAt: '2026-11-01T00:00:00Z', discountBps: 1500 };
const input = { basePricePaise: 10000, eventClubId: 'club-a', membership, nowMs: Date.parse('2026-10-10T00:00:00Z') };
const quote = (changes = {}, memberChanges = {}) => calculate({ ...input, ...changes, membership: { ...membership, ...memberChanges } });

test('active valid membership gets discounted price and explanation', () => {
  assert.deepEqual(quote(), { eligible: true, discountApplied: true, reason: 'discount_applied', explanation: 'Active membership for this club is valid; its discount was applied.', appliedDiscountBps: 1500, pricePaise: 8500, savingsPaise: 1500 });
});
test('missing membership receives full price', () => {
  const result = calculate({ ...input, membership: null });
  assert.equal(result.reason, 'missing_membership');
  assert.equal(result.eligible, false);
  assert.equal(result.pricePaise, 10000);
});
test('cancelled and expired statuses are ineligible', () => {
  for (const status of ['cancelled', 'expired']) {
    const result = quote({}, { status });
    assert.equal(result.reason, 'inactive_membership');
    assert.equal(result.pricePaise, 10000);
  }
});
test('future membership is ineligible', () => assert.equal(quote({ nowMs: Date.parse('2026-09-30T23:59:59.999Z') }).reason, 'not_started'));
test('expired membership is ineligible', () => assert.equal(quote({ nowMs: Date.parse('2026-11-02T00:00:00Z') }).reason, 'expired'));
test('membership from another club is ineligible', () => {
  const result = quote({}, { clubId: 'club-b' });
  assert.equal(result.reason, 'different_club');
  assert.equal(result.pricePaise, 10000);
});
test('start inclusive, expiry exclusive, including adjacent milliseconds', () => {
  const start = Date.parse(membership.startsAt), expiry = Date.parse(membership.expiresAt);
  assert.equal(quote({ nowMs: start - 1 }).eligible, false);
  assert.equal(quote({ nowMs: start }).eligible, true);
  assert.equal(quote({ nowMs: expiry - 1 }).eligible, true);
  assert.equal(quote({ nowMs: expiry }).eligible, false);
});
test('zero discount remains eligible without applying a discount', () => {
  const result = quote({}, { discountBps: 0 });
  assert.equal(result.eligible, true);
  assert.equal(result.discountApplied, false);
  assert.equal(result.reason, 'zero_discount');
  assert.equal(result.pricePaise, 10000);
});
test('maximum discount and zero base price', () => {
  assert.equal(quote({}, { discountBps: 10000 }).pricePaise, 0);
  assert.equal(quote({ basePricePaise: 0 }).pricePaise, 0);
});
test('rejects invalid prices and accepts the safe integer limit exactly', () => {
  for (const basePricePaise of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '100', null]) assert.throws(() => quote({ basePricePaise }), RangeError);
  assert.equal(quote({ basePricePaise: Number.MAX_SAFE_INTEGER }, { discountBps: 0 }).pricePaise, Number.MAX_SAFE_INTEGER);
  assert.equal(quote({ basePricePaise: Number.MAX_SAFE_INTEGER }, { discountBps: 5000 }).pricePaise, 4503599627370496);
});
test('rejects invalid discounts even on inactive membership', () => {
  for (const discountBps of [-1, 10001, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '1500']) {
    assert.throws(() => quote({}, { discountBps }), RangeError);
    assert.throws(() => quote({}, { status: 'cancelled', discountBps }), RangeError);
  }
});
test('rejects invalid calendar dates, formats and reversed periods', () => {
  for (const value of ['bad', '2026-02-30T00:00:00Z', '2026-13-01T00:00:00Z', '2026-10-01', '2026-10-01T00:00:00+05:30', '2026-10-01T24:00:00Z', null]) {
    assert.throws(() => quote({}, { startsAt: value }));
    assert.throws(() => quote({}, { expiresAt: value }));
  }
  assert.throws(() => quote({}, { expiresAt: membership.startsAt }), RangeError);
  assert.throws(() => quote({}, { startsAt: membership.expiresAt, expiresAt: membership.startsAt }), RangeError);
  assert.equal(quote({}, { startsAt: '2026-10-01T00:00:00.001Z' }).eligible, true);
});
test('rejects invalid clock, club, status and missing input fields', () => {
  for (const nowMs of [NaN, Infinity, 1.5, Number.MAX_SAFE_INTEGER, '123', null]) assert.throws(() => quote({ nowMs }), RangeError);
  for (const clubId of ['', ' ', ' club-a', null, 123]) {
    assert.throws(() => quote({}, { clubId }), TypeError);
    assert.throws(() => quote({ eventClubId: clubId }), TypeError);
  }
  assert.throws(() => quote({}, { status: 'unknown' }), TypeError);
  assert.throws(() => calculate({ ...input, membership: undefined }), TypeError);
  assert.throws(() => calculate(null), TypeError);
});
test('rounds final price to nearest paise with ties upward', () => {
  assert.equal(quote({ basePricePaise: 101 }).pricePaise, 86); // 85.85
  assert.equal(quote({ basePricePaise: 1 }, { discountBps: 5000 }).pricePaise, 1); // 0.5
  assert.equal(quote({ basePricePaise: 1 }, { discountBps: 5001 }).pricePaise, 0); // 0.4999
  assert.equal(quote({ basePricePaise: 1 }, { discountBps: 4999 }).pricePaise, 1); // 0.5001
});
test('does not mutate inputs', () => {
  const frozen = Object.freeze({ ...input, membership: Object.freeze({ ...membership }) });
  assert.equal(calculate(frozen).pricePaise, 8500);
});
