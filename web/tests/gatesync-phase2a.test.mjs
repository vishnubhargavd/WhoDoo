import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { isOpaqueCredentialFormat } from '../src/modules/gatesync/credential-format.ts';
import { buildTicketDisplayView } from '../src/modules/gatesync/ticket-display.ts';
import { cameraFailure, createDuplicateScanGuard, createScannerLifecycle, parseScannedCredential, redemptionResultToFeedback } from '../src/modules/gatesync/scanner-state.ts';
import { getScannerAccess } from '../src/modules/gatesync/scanner-access.ts';
import { parseGateSyncEndpoint, parseRedemptionPayload } from '../src/modules/gatesync/redemption-contract.ts';

// Fixed predictable fixture is intentionally never issued or stored; it only exercises SVG rendering.
const token = `whodoo.ticket.v1.${Buffer.alloc(32, 7).toString('base64url')}`;
const record = {
  ticketId: 'test-ticket', eventName: 'Test Hackathon', venue: 'Main Hall',
  startsAt: '2026-11-02T14:00:00.000Z', category: 'Student', holderName: 'Test Student',
  bookingStatus: 'confirmed', paymentStatus: 'paid', credentialState: 'issued', credential: token
};

const componentUrl = new URL('../src/modules/gatesync/components/student-ticket-list.tsx', import.meta.url);
const source = await readFile(componentUrl, 'utf8');
let js = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
js = js.replaceAll("'qrcode.react'", JSON.stringify(import.meta.resolve('qrcode.react')))
  .replaceAll("'../ticket-display.ts'", JSON.stringify(new URL('../ticket-display.ts', componentUrl).href))
  .replaceAll('"qrcode.react"', JSON.stringify(import.meta.resolve('qrcode.react')))
  .replaceAll('"../ticket-display.ts"', JSON.stringify(new URL('../ticket-display.ts', componentUrl).href))
  .replaceAll('"react/jsx-runtime"', JSON.stringify(import.meta.resolve('react/jsx-runtime')));
const { TicketCard, StudentTicketList } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
const render = (node) => renderToStaticMarkup(node);

test('existing GateSync parser accepts canonical v1 shape and rejects malformed or absent credentials', () => {
  assert.equal(isOpaqueCredentialFormat(token), true);
  assert.equal(isOpaqueCredentialFormat(''), false);
  assert.equal(isOpaqueCredentialFormat(undefined), false);
  assert.equal(isOpaqueCredentialFormat(token.slice(0, -1) + 'B'), false);
  assert.equal(isOpaqueCredentialFormat('whodoo.ticket.v2.' + token.split('.').at(-1)), false);
});

test('confirmed booking without a credential is distinct from issued QR', () => {
  const view = buildTicketDisplayView({ ...record, credentialState: null, credential: null });
  assert.equal(view.state, 'confirmed');
  assert.match(view.message, /has not issued/);
  assert.equal(view.credential, null);
  assert.doesNotMatch(render(createElement(TicketCard, { record: { ...record, credentialState: null, credential: null } })), /<svg/);
});

test('pending payment never displays a QR even if a credential-shaped field is present', () => {
  const pending = { ...record, bookingStatus: 'pending_payment', paymentStatus: 'pending' };
  assert.equal(buildTicketDisplayView(pending).state, 'pending');
  const html = render(createElement(TicketCard, { record: pending }));
  assert.doesNotMatch(html, /<svg/);
  assert.match(html, /not a ticket/i);
});

test('cancelled, refunded, revoked and checked-in tickets never render the credential QR', () => {
  const cases = [
    { bookingStatus: 'cancelled' },
    { paymentStatus: 'refunded' },
    { credentialState: 'revoked' },
    { credentialState: 'checked_in' },
  ];
  for (const patch of cases) {
    const candidate = { ...record, ...patch };
    assert.notEqual(buildTicketDisplayView(candidate).state, 'issued');
    assert.doesNotMatch(render(createElement(TicketCard, { record: candidate })), /<svg/);
  }
});

test('issued QR is rendered without exposing the raw bearer credential as text or an attribute', () => {
  const html = render(createElement(TicketCard, { record }));
  assert.match(html, /<svg/);
  assert.match(html, /Private ticket QR for Test Hackathon/);
  assert.match(html, /Test Hackathon/);
  assert.equal(html.includes(token), false);
});

test('ticket source disconnected state is explicit and contains no fabricated tickets', () => {
  const html = render(createElement(StudentTicketList, { tickets: null }));
  assert.match(html, /Ticket service not connected/);
  assert.match(html, /confirmed booking does not mean/);
  assert.doesNotMatch(html, /Test Hackathon|<svg/);
});

test('malformed ticket fields and malformed credential fail closed without QR', () => {
  for (const candidate of [
    { ...record, startsAt: '2026-02-30T14:00:00Z' },
    { ...record, bookingStatus: 'unknown' },
    { ...record, credential: 'whodoo.ticket.v1.predictable' },
    { ...record, eventName: 'bad\nlabel' },
  ]) {
    const view = buildTicketDisplayView(candidate);
    assert.notEqual(view.state, 'issued');
    assert.equal(view.credential, null);
  }
});

test('locally parsing a valid credential only reports capture, never check-in success', () => {
  assert.deepEqual(parseScannedCredential(token), {
    state: 'credential_captured',
    message: 'Credential captured — server verification unavailable. Attendance was not changed.'
  });
  assert.equal(parseScannedCredential('anything').state, 'invalid_qr');
  assert.notEqual(parseScannedCredential(token).state, 'redeemed');
});

test('rapid duplicate scans are suppressed without retaining the scanned value', () => {
  const guard = createDuplicateScanGuard(1800);
  assert.equal(guard.accept(1000), true);
  assert.equal(guard.accept(1001), false);
  assert.equal(guard.accept(2799), false);
  assert.equal(guard.accept(2800), true);
  assert.equal(guard.accept(2700), false);
  guard.reset();
  assert.equal(guard.accept(1), true);
});

test('camera denial, missing device, and initialization failures are distinct', () => {
  assert.equal(cameraFailure({ name: 'NotAllowedError' }), 'denied');
  assert.equal(cameraFailure({ name: 'NotFoundError' }), 'unavailable');
  assert.equal(cameraFailure({ name: 'OverconstrainedError' }), 'unavailable');
  assert.equal(cameraFailure(new Error('unknown')), 'initialization');
});

test('scanner cleanup stops decoder and every media track idempotently', () => {
  let controlsStopped = 0;
  let tracksStopped = 0;
  const lifecycle = createScannerLifecycle();
  lifecycle.attach({ stop() { controlsStopped += 1; } }, { getTracks: () => [{ stop() { tracksStopped += 1; } }, { stop() { tracksStopped += 1; } }] });
  lifecycle.stop();
  lifecycle.stop();
  assert.equal(controlsStopped, 1);
  assert.equal(tracksStopped, 2);
});

test('only a structured redeemed response becomes success; local ALLOW and malformed replies do not', () => {
  const success = { status: 'redeemed', redemptionId: 'redemption-1', checkedInAt: '2026-10-10T12:00:00.000Z', eventId: 'event-1', eventName: 'Test Hackathon' };
  assert.equal(parseRedemptionPayload(success)?.status, 'redeemed');
  assert.equal(redemptionResultToFeedback(success, 'event-1').state, 'redeemed');
  assert.equal(redemptionResultToFeedback(success, 'event-2').state, 'wrong_event');
  assert.notEqual(redemptionResultToFeedback({ outcome: 'ALLOW', allowed: true }).state, 'redeemed');
  assert.equal(redemptionResultToFeedback({ status: 'redeemed', redemptionId: '', checkedInAt: 'bad', eventName: '' }).state, 'network_unavailable');
  for (const [status, expected] of [
    ['wrong_event', 'wrong_event'], ['expired', 'expired_or_revoked'],
    ['revoked', 'expired_or_revoked'], ['already_checked_in', 'already_checked_in'],
    ['authorization_denied', 'authorization_denied'], ['unavailable', 'network_unavailable']
  ]) assert.equal(redemptionResultToFeedback({ status }).state, expected);
});

test('FastAPI API contract parser accepts only configured HTTPS endpoints and known responses', () => {
  assert.equal(parseGateSyncEndpoint(undefined), null);
  assert.equal(parseGateSyncEndpoint('http://localhost:8000/redeem'), null);
  assert.equal(parseGateSyncEndpoint('https://user:pass@example.test/redeem'), null);
  assert.equal(parseGateSyncEndpoint('https://api.example.test/redeem'), 'https://api.example.test/redeem');
  assert.equal(parseRedemptionPayload({ status: 'ALLOW', allowed: true }), null);
  assert.equal(parseRedemptionPayload({ status: 'redeemed', redemptionId: 'r1', checkedInAt: '2026-10-10T12:00:00.000Z', eventId: 'e1', eventName: 'Event' })?.status, 'redeemed');
  assert.equal(parseRedemptionPayload({ status: 'redeemed', redemptionId: 'r1', checkedInAt: '2026-10-10T12:00:00+00:00', eventName: 'Event' }), null);
});

test('scanner route access uses verified getUser and does not treat a client session as identity', async () => {
  const makeAuth = user => ({ getUser: async () => ({ data: { user }, error: null }), getSession: async () => { throw new Error('unverified session used'); } });
  assert.deepEqual(await getScannerAccess(makeAuth(null)), { status: 'redirect', destination: '/login?next=%2Fcheck-in' });
  assert.equal((await getScannerAccess(null)).status, 'unavailable');
  assert.equal((await getScannerAccess(makeAuth({ id: 'auth-id', email: 'volunteer@example.test', user_metadata: { role: 'president' } }))).status, 'allow-authenticated');
});

test('scanner controls expose instructions, live status and explicit permission button', async () => {
  const source = await readFile(new URL('../src/modules/gatesync/components/mobile-scanner.tsx', import.meta.url), 'utf8');
  assert.match(source, /aria-label="Live camera preview/);
  assert.match(source, /aria-live="polite"/);
  assert.match(source, /onClick=\{\(\) => void startCamera/);
  assert.match(source, /Switch camera/);
  assert.match(source, /lifecycle\.current\.stop\(\)/);
});
