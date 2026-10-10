import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { verifyProfile } from '../src/modules/membercore/auth-flow.ts';
import { buildMembershipView } from '../src/modules/membercore/membership-view.ts';

const now = Date.parse('2026-10-10T12:00:00Z');
const record = {
  membership: { status: 'active', clubId: 'club-fixture', startsAt: '2026-10-01T00:00:00Z', expiresAt: '2026-11-01T00:00:00Z', discountBps: 1250 },
  planName: 'Student annual', clubName: 'Fixture club'
};
const port = (user, error = null) => ({ getUser: async () => ({ data: { user }, error }), getSession: () => { throw new Error('must not use unverified session'); } });

// Compile the real TSX component in memory for actual HTML assertions; no extra runner.
const source = await readFile(new URL('../src/components/membership-dashboard.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { MembershipDashboard } = await import('data:text/javascript;base64,' + Buffer.from(compiled.replaceAll('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')))).toString('base64'));
const render = (view) => renderToStaticMarkup(createElement(MembershipDashboard, { view }));

test('signed-out, expired and rejected identities receive no protected profile content', async () => {
  for (const auth of [port(null), port({ id: 'fixture', email: 'private@example.test' }, { code: 'bad_jwt' })]) {
    const access = await verifyProfile(auth);
    assert.equal(access.status, 'login');
    assert.equal('email' in access, false);
    assert.equal('authUserId' in access, false);
  }
  assert.equal((await verifyProfile(null)).status, 'unavailable');
});
test('profile verification reads identity and email confirmation only from getUser', async () => {
  const access = await verifyProfile(port({ id: 'auth-fixture', email: 'fixture@example.test', email_confirmed_at: '2026-10-01T00:00:00Z', user_metadata: { role: 'president', member: true, discountBps: 10000 } }));
  assert.deepEqual(access, { status: 'authenticated', authUserId: 'auth-fixture', email: 'fixture@example.test', emailVerification: 'verified' });
  assert.equal('role' in access, false);
  assert.equal('discountBps' in access, false);
});
test('email verification distinguishes unknown, unverified and malformed confirmation data', async () => {
  for (const [confirmedAt, expected] of [[undefined, 'unavailable'], [null, 'unverified'], ['invalid', 'unavailable']]) {
    const access = await verifyProfile(port({ id: 'fixture', email: 'fixture@example.test', email_confirmed_at: confirmedAt, user_metadata: { email_verified: true } }));
    assert.equal(access.emailVerification, expected);
  }
  assert.equal((await verifyProfile(port({ id: 'fixture', email_confirmed_at: '2026-10-01T00:00:00Z' }))).emailVerification, 'unavailable');
});
test('profile verification failure never exposes raw upstream details', async () => {
  const access = await verifyProfile({ getUser: async () => { throw new Error('private-token'); } });
  assert.equal(access.status, 'unavailable');
  assert.doesNotMatch(access.message, /private-token/);
});
test('real dashboard markup displays the honest disconnected empty state', () => {
  const view = buildMembershipView(null, now);
  const html = render(view);
  assert.equal(view.state, 'not-connected');
  assert.match(html, /Membership data not connected/);
  for (const label of ['Membership status', 'Membership plan', 'Club', 'Member discounts', 'Start date', 'Expiry date', 'Renewal reminder']) assert.ok(html.includes(label));
  assert.doesNotMatch(html, /Active|Student annual|Fixture club|12.5%|Paid/);
});
test('valid server presentation record uses calculator eligibility and display dates', () => {
  const view = buildMembershipView(record, now);
  assert.equal(view.state, 'record');
  assert.equal(view.status, 'Active');
  assert.equal(view.discount, '12.5% member ticket discount');
  assert.equal(view.startsAt, '2026-10-01');
  assert.equal(view.expiresAt, '2026-11-01');
  assert.match(view.renewal, /within 30 days/);
});
test('expiry is exclusive even when stored status still says active', () => {
  const expiry = Date.parse(record.membership.expiresAt);
  assert.equal(buildMembershipView(record, expiry - 1).status, 'Active');
  const view = buildMembershipView(record, expiry);
  assert.equal(view.status, 'Expired');
  assert.equal(view.discount, 'Not currently eligible');
  assert.match(render(view), /Membership has expired/);
  assert.doesNotMatch(render(view), /12.5%/);
});
test('future, cancelled and explicitly expired memberships do not confirm discounts', () => {
  assert.equal(buildMembershipView(record, Date.parse(record.membership.startsAt) - 1).status, 'Upcoming');
  assert.equal(buildMembershipView(record, Date.parse(record.membership.startsAt)).status, 'Active');
  for (const status of ['cancelled', 'expired']) {
    const view = buildMembershipView({ ...record, membership: { ...record.membership, status } }, now);
    assert.equal(view.discount, 'Not currently eligible');
    assert.notEqual(view.status, 'Active');
  }
});
test('invalid dates, ordering, discounts, status, club IDs and unsafe clocks fail closed', () => {
  for (const patch of [{ startsAt: '2026-02-30T00:00:00Z' }, { expiresAt: 'bad' }, { expiresAt: record.membership.startsAt }, { startsAt: '2026-10-01' }, { discountBps: -1 }, { discountBps: 10001 }, { discountBps: 1.5 }, { status: 'paid' }, { clubId: '' }]) {
    const view = buildMembershipView({ ...record, membership: { ...record.membership, ...patch } }, now);
    assert.equal(view.state, 'invalid');
    assert.equal(view.discount, 'Not available');
    assert.equal(view.status, 'Not available');
  }
  assert.equal(buildMembershipView(record, Number.MAX_SAFE_INTEGER + 1).state, 'invalid');
});
test('missing labels are explicit and malformed labels invalidate the view', () => {
  assert.equal(buildMembershipView({ ...record, planName: null, clubName: null }, now).plan, 'Not available');
  for (const planName of ['', undefined, 42, 'bad\nlabel', 'x'.repeat(121)]) assert.equal(buildMembershipView({ ...record, planName }, now).state, 'invalid');
});
test('metadata-shaped input cannot confirm roles, dues or member benefits', () => {
  const view = buildMembershipView({ user_metadata: { role: 'president', membership: 'active', discountBps: 10000, duesPaid: true } }, now);
  assert.equal(view.state, 'invalid');
  assert.equal(view.discount, 'Not available');
  assert.equal(view.status, 'Not available');
  assert.doesNotMatch(render(view), /president|100%|Paid/);
  const clean = buildMembershipView({ ...record, role: 'president', duesPaid: true, user_metadata: { discountBps: 10000 } }, now);
  assert.equal(clean.discount, '12.5% member ticket discount');
  assert.equal('role' in clean, false);
  assert.equal('duesPaid' in clean, false);
});
test('zero/max discounts reuse validated basis points and display names are escaped', () => {
  assert.equal(buildMembershipView({ ...record, membership: { ...record.membership, discountBps: 0 } }, now).discount, '0% member ticket discount');
  assert.equal(buildMembershipView({ ...record, membership: { ...record.membership, discountBps: 10000 } }, now).discount, '100% member ticket discount');
  const html = render(buildMembershipView({ ...record, planName: '<script>fixture</script>' }, now));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>'));
});
