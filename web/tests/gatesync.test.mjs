import test from 'node:test';
import assert from 'node:assert/strict';
import { digestOpaqueCredential, resolveCredential } from '../src/modules/gatesync/credential.ts';
import { decideCheckIn } from '../src/modules/gatesync/check-in-decision.ts';

const token = 'whodoo.ticket.v1.' + 'A'.repeat(43); // deterministic, synthetic test fixture
const start = '2026-10-10T09:00:00Z';
const expiry = '2026-10-10T13:00:00Z';
const opens = '2026-10-10T11:00:00Z';
const closes = '2026-10-10T14:00:00Z';
const eventClose = '2026-10-10T12:30:00Z';
const now = Date.parse('2026-10-10T12:00:00Z');
const parsed = digestOpaqueCredential(token);
assert.equal(parsed.ok, true);
const ticket = {
  version: 1, ticketId: 'ticket-fixture', eventId: 'event-fixture', clubId: 'club-fixture',
  issuedAt: start, expiresAt: expiry, credentialId: parsed.digest, state: 'issued', checkedInAt: null
};
const context = (changes = {}) => ({ eventId: 'event-fixture', clubId: 'club-fixture', opensAt: opens, closesAt: closes, cancelled: false, nowMs: now, ...changes });
const resolution = (changes = {}) => ({ status: 'resolved', record: { ...ticket, ...changes } });
const decide = (resolved = resolution(), ctx = context()) => decideCheckIn(resolved, ctx);
const repositoryFor = (credential, record) => ({
  findByCredentialDigest: async (digest) => digest === digestOpaqueCredential(credential).digest ? record : null
});

test('valid opaque credential resolves only by digest and permits an eligible ticket', async () => {
  const result = await resolveCredential(token, repositoryFor(token, ticket));
  assert.equal(result.status, 'resolved');
  assert.equal(decide(result).outcome, 'ALLOW');
});
test('QR contract contains no ticket PII or business fields', () => {
  assert.deepEqual(Object.keys(parsed).sort(), ['digest', 'ok']);
  assert.doesNotMatch(JSON.stringify(parsed), /ticket-fixture|event-fixture|club-fixture|student|email/i);
});
test('malformed credential fails before repository lookup', async () => {
  let calls = 0;
  const repo = { findByCredentialDigest: async () => { calls++; return ticket; } };
  for (const value of [null, undefined, '', 'ticket-fixture', token.slice(0, -1), token + 'A', 'whodoo.ticket.v2.' + 'A'.repeat(43), 'whodoo.ticket.v1.' + 'A'.repeat(42) + '!']) {
    assert.deepEqual(await resolveCredential(value, repo), { status: 'invalid' });
  }
  assert.equal(calls, 0);
  assert.equal(decide({ status: 'invalid' }, context()).outcome, 'INVALID_CREDENTIAL');
});
test('well-formed unknown token is rejected without a ticket record', async () => {
  const result = await resolveCredential(token, { findByCredentialDigest: async () => null });
  assert.deepEqual(result, { status: 'invalid' });
  assert.equal(decide(result).outcome, 'INVALID_CREDENTIAL');
});
test('lookup rejects server rows whose stored credential digest does not match', async () => {
  assert.deepEqual(await resolveCredential(token, repositoryFor(token, { ...ticket, credentialId: 'f'.repeat(64) })), { status: 'invalid' });
});
test('wrong event and wrong club are separate denials', () => {
  assert.equal(decide(resolution(), context({ eventId: 'another-event' })).outcome, 'WRONG_EVENT');
  assert.equal(decide(resolution(), context({ clubId: 'another-club' })).outcome, 'WRONG_CLUB');
});
test('issue time is inclusive and a ticket issued in the future is not yet valid', () => {
  assert.equal(decide(resolution(), context({ opensAt: start, nowMs: Date.parse(start) })).outcome, 'ALLOW');
  assert.equal(decide(resolution({ issuedAt: '2026-10-10T12:00:00.001Z' })).outcome, 'NOT_YET_VALID');
});
test('expiry is exclusive and expired tickets are rejected', () => {
  assert.equal(decide(resolution(), context({ nowMs: Date.parse(expiry) - 1 })).outcome, 'ALLOW');
  assert.equal(decide(resolution(), context({ nowMs: Date.parse(expiry) })).outcome, 'EXPIRED');
  assert.equal(decide(resolution({ expiresAt: '2026-10-10T11:59:59Z' })).outcome, 'EXPIRED');
});
test('revoked and cancelled tickets are rejected', () => {
  assert.equal(decide(resolution({ state: 'revoked' })).outcome, 'REVOKED');
  assert.equal(decide(resolution({ state: 'cancelled' })).outcome, 'CANCELLED');
});
test('previously checked-in tickets are rejected', () => {
  assert.equal(decide(resolution({ state: 'checked_in', checkedInAt: '2026-10-10T11:30:00Z' })).outcome, 'ALREADY_CHECKED_IN');
  assert.equal(decide(resolution({ state: 'issued', checkedInAt: '2026-10-10T11:30:00Z' })).outcome, 'INVALID_CREDENTIAL');
});
test('event opening is inclusive and closing is exclusive', () => {
  assert.equal(decide(resolution(), context({ nowMs: Date.parse(opens) - 1 })).outcome, 'EVENT_NOT_OPEN');
  assert.equal(decide(resolution(), context({ opensAt: start, nowMs: Date.parse(start) })).outcome, 'ALLOW');
  assert.equal(decide(resolution(), context({ closesAt: eventClose, nowMs: Date.parse(eventClose) - 1 })).outcome, 'ALLOW');
  assert.equal(decide(resolution(), context({ closesAt: eventClose, nowMs: Date.parse(eventClose) })).outcome, 'EVENT_NOT_OPEN');
});
test('cancelled event is not open for check-in', () => {
  assert.equal(decide(resolution(), context({ cancelled: true })).outcome, 'EVENT_NOT_OPEN');
});
test('missing, malformed and impossible event dates fail closed', () => {
  for (const changes of [{ opensAt: undefined }, { closesAt: 'bad' }, { opensAt: '2026-02-30T10:00:00Z' }, { closesAt: opens }]) {
    assert.equal(decide(resolution(), context(changes)).outcome, 'EVENT_NOT_OPEN');
  }
});
test('missing ticket and context identifiers fail closed', () => {
  for (const changes of [{ ticketId: '' }, { eventId: '' }, { clubId: '' }, { credentialId: 'bad' }]) {
    assert.equal(decide(resolution(changes)).outcome, 'INVALID_CREDENTIAL');
  }
  assert.equal(decide(resolution(), context({ eventId: '' })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution(), context({ clubId: ' ' })).outcome, 'INVALID_CREDENTIAL');
});
test('unsupported credential versions and inconsistent record states are invalid', () => {
  assert.equal(decide(resolution({ version: 2 })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution({ issuedAt: undefined })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution({ expiresAt: undefined })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution({ issuedAt: '2026-02-30T09:00:00Z' })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution({ state: 'issued', checkedInAt: start })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution({ state: 'checked_in', checkedInAt: null })).outcome, 'INVALID_CREDENTIAL');
  assert.equal(decide(resolution({ expiresAt: start })).outcome, 'INVALID_CREDENTIAL');
});
test('tampered opaque token hashes to a different digest and fails lookup', async () => {
  const tampered = 'whodoo.ticket.v1.' + 'B' + 'A'.repeat(42);
  assert.notEqual(digestOpaqueCredential(tampered).digest, parsed.digest);
  assert.deepEqual(await resolveCredential(tampered, repositoryFor(token, ticket)), { status: 'invalid' });
});
test('repository failures are operational errors, never successful check-ins', async () => {
  const resolved = await resolveCredential(token, { findByCredentialDigest: async () => { throw new Error('storage detail'); } });
  assert.deepEqual(resolved, { status: 'unavailable' });
  assert.doesNotMatch(JSON.stringify(resolved), /storage detail/);
});
test('client flags do not override the resolved server ticket record', () => {
  const verified = resolution();
  const first = decide(verified);
  const withUntrustedFlags = decide({ ...verified, isValid: false, alreadyCheckedIn: true, membershipActive: false, role: 'member' });
  assert.deepEqual(withUntrustedFlags, first);
  assert.equal(first.allowed, true);
});
test('pure decisions are stable across repeat calls and do not mutate snapshots', () => {
  const input = resolution();
  const snapshot = structuredClone(input);
  assert.deepEqual(decide(input), decide(input));
  assert.deepEqual(input, snapshot);
});
test('all outcomes carry a stable explanation and allowed flag', () => {
  const inputs = [
    [{ status: 'invalid' }, context()], [resolution({ eventId: 'other' }), context()],
    [resolution(), context({ clubId: 'other' })], [resolution({ issuedAt: '2026-10-10T12:00:00.001Z' }), context()],
    [resolution({ expiresAt: '2026-10-10T11:30:00Z' }), context()], [resolution({ state: 'revoked' }), context()],
    [resolution({ state: 'cancelled' }), context()], [resolution({ state: 'checked_in', checkedInAt: start }), context()],
    [resolution(), context({ cancelled: true })]
  ];
  for (const [credential, ctx] of inputs) {
    const decision = decideCheckIn(credential, ctx);
    assert.equal(typeof decision.reason, 'string');
    assert.equal(decision.allowed, decision.outcome === 'ALLOW');
  }
});
