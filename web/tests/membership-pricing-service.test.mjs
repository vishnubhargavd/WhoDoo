import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applicationUserId } from '../src/modules/membercore/membership-repository.ts';
import { quoteMemberTicket, MembershipScopeError, MembershipRepositoryError } from '../src/modules/membercore/membership-pricing-service.ts';
import { calculateTicketDiscount } from '../src/modules/membercore/ticket-discount.ts';

const userId = applicationUserId('app-user-1');
const membership = { status: 'active', clubId: 'club-a', startsAt: '2026-10-01T00:00:00Z', expiresAt: '2026-11-01T00:00:00Z', discountBps: 1500 };
const record = { applicationUserId: userId, membership };
const context = { applicationUserId: userId, clubId: 'club-a' };
const ticket = { basePricePaise: 101, eventClubId: 'club-a', nowMs: Date.parse('2026-10-10T00:00:00Z') };

class InMemoryRepository {
  calls = [];
  constructor(records = []) { this.records = records; }
  async findForUserAndClub(id, clubId) {
    this.calls.push({ id, clubId });
    const found = this.records.find(r => r.applicationUserId === id && r.membership.clubId === clubId);
    return found ? { ...found, membership: { ...found.membership } } : null;
  }
}

test('service queries application user and club and delegates exact result to calculator', async () => {
  const repo = new InMemoryRepository([record]);
  assert.deepEqual(await quoteMemberTicket(repo, context, ticket), calculateTicketDiscount({ ...ticket, membership }));
  assert.deepEqual(repo.calls, [{ id: userId, clubId: 'club-a' }]);
});
test('missing record receives full price with missing_membership explanation', async () => {
  const result = await quoteMemberTicket(new InMemoryRepository(), context, ticket);
  assert.equal(result.reason, 'missing_membership');
  assert.equal(result.pricePaise, 101);
});
test('repository isolates other application users and clubs', async () => {
  const repo = new InMemoryRepository([
    { ...record, applicationUserId: applicationUserId('other-user') },
    { ...record, membership: { ...membership, clubId: 'club-b' } }
  ]);
  assert.equal((await quoteMemberTicket(repo, context, ticket)).reason, 'missing_membership');
});
test('trusted context from another club is rejected before lookup', async () => {
  const repo = new InMemoryRepository([record]);
  await assert.rejects(quoteMemberTicket(repo, { ...context, clubId: 'club-b' }, ticket), MembershipScopeError);
  assert.equal(repo.calls.length, 0);
});
test('expired membership and exact validity boundaries use calculator rules', async () => {
  const repo = new InMemoryRepository([record]);
  assert.equal((await quoteMemberTicket(repo, context, { ...ticket, nowMs: Date.parse(membership.startsAt) })).eligible, true);
  assert.equal((await quoteMemberTicket(repo, context, { ...ticket, nowMs: Date.parse(membership.expiresAt) })).reason, 'expired');
  assert.equal((await quoteMemberTicket(repo, context, { ...ticket, nowMs: Date.parse('2026-12-01T00:00:00Z') })).pricePaise, 101);
});
test('cancelled membership remains ineligible', async () => {
  const repo = new InMemoryRepository([{ ...record, membership: { ...membership, status: 'cancelled' } }]);
  assert.equal((await quoteMemberTicket(repo, context, ticket)).reason, 'inactive_membership');
});
test('out-of-scope or malformed repository records fail closed', async () => {
  for (const value of [undefined, {}, { ...record, applicationUserId: applicationUserId('other') }, { ...record, membership: { ...membership, clubId: 'club-b' } }]) {
    await assert.rejects(quoteMemberTicket({ findForUserAndClub: async () => value }, context, ticket), error => error instanceof MembershipRepositoryError && error.code === 'invalid_record');
  }
});
test('async and synchronous repository failures never become guest quotes or leak storage details', async () => {
  for (const findForUserAndClub of [async () => { throw new Error('private storage detail'); }, () => { throw new Error('private storage detail'); }]) {
    await assert.rejects(quoteMemberTicket({ findForUserAndClub }, context, ticket), error => error instanceof MembershipRepositoryError && error.code === 'lookup_failed' && !error.message.includes('private'));
  }
});
test('invalid identity and ticket inputs fail before repository lookup', async () => {
  const repo = new InMemoryRepository([record]);
  for (const value of ['', ' bad', undefined]) await assert.rejects(quoteMemberTicket(repo, { ...context, applicationUserId: value }, ticket), TypeError);
  await assert.rejects(quoteMemberTicket(repo, context, { ...ticket, basePricePaise: -1 }), RangeError);
  await assert.rejects(quoteMemberTicket(repo, context, { ...ticket, nowMs: NaN }), RangeError);
  assert.equal(repo.calls.length, 0);
});
test('invalid stored membership data is rejected by the calculator', async () => {
  for (const changes of [{ discountBps: 10001 }, { expiresAt: 'bad-date' }]) {
    const repo = new InMemoryRepository([{ ...record, membership: { ...membership, ...changes } }]);
    await assert.rejects(quoteMemberTicket(repo, context, ticket));
  }
});
