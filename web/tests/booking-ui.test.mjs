import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { loadEvents, parseEvents, selectionError, priceView, formatPaise, filterEvents, bookingStatusLabel } from '../src/modules/fairseat/view-models/booking-view.ts';
import { createSubmissionController, reserveForAccount, bookingLoginDestination, quoteForAccount } from '../src/modules/fairseat/checkout.ts';
import { safeDestination } from '../src/modules/membercore/auth-flow.ts';
import { calculateTicketDiscount } from '../src/modules/membercore/ticket-discount.ts';

// These are test fixtures only. No fixture is imported by production routes.
const now = Date.parse('2026-10-10T12:00:00Z');
const event = { eventId: 'fixture-event', title: '<script>campus</script>', description: 'Fixture description', venue: 'Fixture hall',
  startsAt: '2026-10-11T12:00:00.000Z', endsAt: '2026-10-11T14:00:00.000Z', status: 'published', saleState: 'open',
  categories: [{ categoryId: 'fixture-category', name: 'General', basePricePaise: 10001, maxQuantity: 4, availability: 12 }] };
const memberPricing = calculateTicketDiscount({ basePricePaise: 10001, eventClubId: 'fixture-club', nowMs: now,
  membership: { clubId: 'fixture-club', status: 'active', startsAt: '2026-10-01T00:00:00Z', expiresAt: '2026-11-01T00:00:00Z', discountBps: 1000 } });
const quote = { quoteId: 'fixture-quote', eventId: event.eventId, ticketCategoryId: 'fixture-category', quantity: 2,
  baseUnitPricePaise: 10001, unitPricePaise: memberPricing.pricePaise, totalPricePaise: memberPricing.pricePaise * 2,
  memberPricing, issuedAtMs: now - 1000, expiresAtMs: now + 60000 };
const selection = { eventId: event.eventId, ticketCategoryId: 'fixture-category', quantity: 2, acceptedQuoteId: quote.quoteId };
const auth = { getUser: async () => ({ data: { user: { id: 'auth-fixture', email: 'fixture@example.test' } }, error: null }) };
const command = { request: { eventId: event.eventId, ticketCategoryId: 'fixture-category', quantity: 2, idempotencyKey: 'fixture-retry-key-12345' }, acceptedQuoteId: quote.quoteId };
const success = { ok: true, value: { kind: 'reserved', bookingId: 'server-fixture-booking', status: 'pending_payment', paymentStatus: 'pending', totalPricePaise: 18002, holdExpiresAt: '2026-10-10T12:10:00Z' } };

// Transpile the actual React component in memory and resolve its real runtime imports.
let compiled = ts.transpileModule(await readFile(new URL('../src/modules/fairseat/components/booking-portal.tsx', import.meta.url), 'utf8'),
  { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
compiled = compiled.replace(/import styles from ['"]\.\/booking-portal.module.css['"];?/, 'const styles = new Proxy({}, { get: (_, key) => String(key) });');
for (const [specifier, path] of [['react', import.meta.resolve('react')], ['react/jsx-runtime', import.meta.resolve('react/jsx-runtime')],
  ['../checkout', new URL('../src/modules/fairseat/checkout.ts', import.meta.url).href],
  ['../view-models/booking-view', new URL('../src/modules/fairseat/view-models/booking-view.ts', import.meta.url).href]]) {
  compiled = compiled.replaceAll(`"${specifier}"`, JSON.stringify(path)).replaceAll(`'${specifier}'`, JSON.stringify(path));
}
const { BookingPortal, ReservationNotice } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
const render = listing => renderToStaticMarkup(createElement(BookingPortal, { listing, quoteConnected: false, requestQuote: async () => ({ state: 'not_connected' }), checkoutConnected: false, reserve: async () => success }));

test('no event API yields explicit not-connected UI without fabricated events', async () => {
  const listing = await loadEvents(null);
  assert.equal(listing.state, 'not_connected');
  const html = render(listing);
  assert.match(html, /Event listings not connected yet/);
  assert.match(html, /Ticket reservations are not connected yet/);
  assert.doesNotMatch(html, /server-fixture-booking|&lt;script&gt;campus|Pay now/);
});
test('connected empty listing displays honest empty state', async () => {
  const listing = await loadEvents({ load: async () => [] });
  assert.match(render(listing), /No events available yet/);
  assert.equal(listing.state, 'ready');
});
test('real component escapes server event text and renders date venue and state', async () => {
  const html = render(await loadEvents({ load: async () => [event] }));
  assert.match(html, /&lt;script&gt;campus&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Fixture hall/);
  assert.match(html, /published/);
  assert.match(html, /View ticket options/);
});
test('malformed dates, prices, duplicate IDs and unsafe adapter data fail closed', () => {
  for (const patch of [{ startsAt: '2026-02-30T12:00:00.000Z' }, { endsAt: event.startsAt }, { status: 'live' },
    { categories: [{ ...event.categories[0], basePricePaise: -1 }] }, { categories: [{ ...event.categories[0], maxQuantity: 101 }] },
    { categories: [{ ...event.categories[0], availability: Number.MAX_SAFE_INTEGER + 1 }] }]) assert.equal(parseEvents([{ ...event, ...patch }]), null);
  assert.equal(parseEvents([event, event]), null);
  assert.equal(parseEvents([{ ...event, categories: [...event.categories, ...event.categories] }]), null);
});
test('failed event load does not leak upstream errors', async () => {
  const listing = await loadEvents({ load: async () => { throw new Error('secret'); } });
  assert.equal(listing.state, 'unavailable');
  assert.doesNotMatch(render(listing), /secret/);
});
test('invalid category and quantities rejected without treating availability as allocation', () => {
  assert.match(selectionError(event, 'unknown', 1), /category/);
  for (const quantity of [0, -1, 1.5, NaN, 5, Number.MAX_SAFE_INTEGER + 1]) assert.ok(selectionError(event, 'fixture-category', quantity));
  assert.equal(selectionError({ ...event, categories: [{ ...event.categories[0], availability: 0 }] }, 'fixture-category', 2), null);
});
test('member price display reuses the calculator output and confirmed quote', () => {
  const price = priceView(event, 'fixture-category', 2, quote, now);
  assert.equal(price.kind, 'confirmed');
  assert.equal(price.total, '₹180.02');
  assert.equal(price.savings, '₹10.00');
  assert.equal(price.explanation, memberPricing.explanation);
});
test('missing, expired, future-issued and mismatched quotes stay estimated', () => {
  for (const q of [null, { ...quote, expiresAtMs: now }, { ...quote, issuedAtMs: now + 1 }, { ...quote, eventId: 'other' }, { ...quote, quantity: 1 }]) {
    const price = priceView(event, 'fixture-category', 2, q, now);
    assert.equal(price.kind, 'estimated');
    assert.equal(price.total, '₹200.02');
    assert.equal(price.quoteId, null);
    assert.match(price.explanation, /not verified/);
  }
});
test('paise display and estimate multiplication remain exact at integer limits', () => {
  assert.equal(formatPaise(1), '₹0.01');
  assert.equal(formatPaise(100001), '₹1,000.01');
  assert.equal(formatPaise(Number.MAX_SAFE_INTEGER), '₹9,00,71,99,25,47,409.91');
  for (const price of [-1, 1.1, NaN, Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => formatPaise(price));
  assert.equal(priceView({ ...event, categories: [{ ...event.categories[0], basePricePaise: Number.MAX_SAFE_INTEGER }] }, 'fixture-category', 2, null, now), null);
});
test('search status and date filters operate only on supplied events', () => {
  assert.equal(filterEvents([event], 'campus', 'published', '2026-10-11').length, 1);
  assert.equal(filterEvents([event], 'different', '', '').length, 0);
  assert.equal(filterEvents([event], '', 'cancelled', '').length, 0);
});
test('unconfigured booking service cannot produce reservation success', async () => {
  assert.deepEqual(await reserveForAccount(auth, null, command), { ok: false, error: 'not_configured', message: 'Ticket reservations are not connected yet.' });
});
test('quote action denies missing identity and disconnected integration', async () => {
  let called = false;
  const port = { load: async () => { called = true; return quote; } };
  assert.equal((await quoteForAccount(null, port, selection, now)).state, 'not_authenticated');
  assert.equal(called, false);
  assert.equal((await quoteForAccount(auth, null, selection, now)).state, 'not_connected');
});
test('quote action rejects browser authority and wrong expired or malformed server quotes', async () => {
  const input = { eventId: event.eventId, ticketCategoryId: 'fixture-category', quantity: 2 };
  for (const patch of [{ pricePaise: 1 }, { userId: 'forged' }, { quantity: -1 }]) assert.equal((await quoteForAccount(auth, { load: async () => quote }, { ...input, ...patch }, now)).state, 'unavailable');
  for (const q of [{ ...quote, quantity: 1 }, { ...quote, expiresAtMs: now }, { ...quote, unitPricePaise: -1 }]) assert.equal((await quoteForAccount(auth, { load: async () => q }, input, now)).state, 'unavailable');
});
test('quote action copies only public validated quote fields', async () => {
  const input = { eventId: event.eventId, ticketCategoryId: 'fixture-category', quantity: 2 };
  const result = await quoteForAccount(auth, { load: async () => ({ ...quote, token: 'secret', memberPricing: { ...memberPricing, role: 'president' } }) }, input, now);
  assert.deepEqual(result, { state: 'ready', quote });
});
test('server boundary denies absent or rejected identity before calling any backend', async () => {
  let called = false;
  const config = { reservationEndpoint: 'https://approved.example.test/reserve', getAuthorizationHeader: async () => { called = true; return 'Bearer fixture'; } };
  for (const identity of [null, { getUser: async () => ({ data: { user: null }, error: null }) },
    { getUser: async () => ({ data: { user: { id: 'auth-fixture' } }, error: { code: 'invalid' } }) }]) {
    assert.equal((await reserveForAccount(identity, config, command)).error, 'not_authenticated');
  }
  assert.equal(called, false);
});
test('authenticated reservation goes through existing API parser with selectors only', async () => {
  let body;
  const result = await reserveForAccount(auth, { reservationEndpoint: 'https://approved.example.test/reserve', getAuthorizationHeader: async () => 'Bearer fixture',
    fetcher: async (_, init) => { body = JSON.parse(init.body); return Response.json(success.value); } }, command);
  assert.deepEqual(result, success);
  assert.deepEqual(Object.keys(body).sort(), ['acceptedQuoteId', 'eventId', 'idempotencyKey', 'quantity', 'ticketCategoryId'].sort());
});
test('backend rejection and price change render as failure without success or payment actions', () => {
  for (const value of [{ kind: 'rejected', reason: 'sold_out', message: 'No seats' }, { kind: 'price_changed', message: 'Review quote' }]) {
    const html = renderToStaticMarkup(createElement(ReservationNotice, { result: { ok: true, value } }));
    assert.match(html, /role="alert"/);
    assert.doesNotMatch(html, /Reservation:|Pay now|QR ticket has been issued/);
  }
});
test('controller rejects concurrent double submit and remembers successful operation', async () => {
  const controller = createSubmissionController(() => 'fixture-retry-key-12345');
  let finish; let calls = 0;
  const reserve = async () => { calls++; return new Promise(resolve => { finish = resolve; }); };
  const first = controller.submit(selection, reserve);
  assert.equal(await controller.submit(selection, reserve), null);
  finish(success);
  assert.deepEqual(await first, success);
  assert.deepEqual(await controller.submit(selection, reserve), success);
  assert.equal(calls, 1);
});
test('network retry preserves exact operation key even after selecting another quantity', async () => {
  let count = 0; const commands = [];
  const controller = createSubmissionController(() => `fixture-retry-key-${++count}`);
  const fail = async command => { commands.push(command); throw new Error('secret'); };
  assert.equal((await controller.submit(selection, fail)).error, 'network_error');
  await controller.submit({ ...selection, quantity: 3 }, fail);
  await controller.submit(selection, fail);
  assert.equal(commands[0].request.idempotencyKey, commands[2].request.idempotencyKey);
  assert.notEqual(commands[0].request.idempotencyKey, commands[1].request.idempotencyKey);
});
test('controller cannot display success for malformed reservation response', async () => {
  const controller = createSubmissionController(() => 'fixture-retry-key-12345');
  const result = await controller.submit(selection, async () => ({ ok: true, value: { kind: 'reserved', bookingId: 'invented' } }));
  assert.equal(result.error, 'invalid_response');
});
test('return redirect uses the existing strict internal allowlist', () => {
  assert.equal(bookingLoginDestination(), '/login?next=%2Fbookings');
  assert.equal(safeDestination('/bookings'), '/bookings');
  for (const value of ['//evil.test', 'https://evil.test', '/bookings?next=//evil', '/%2F%2Fevil']) assert.equal(safeDestination(value), '/account');
});
test('exact lifecycle states display without inventing new backend statuses', () => {
  assert.equal(bookingStatusLabel('pending_payment', 'pending'), 'Held · Awaiting payment');
  assert.equal(bookingStatusLabel('confirmed', 'paid'), 'Confirmed');
  assert.equal(bookingStatusLabel('expired', 'pending'), 'Expired');
  assert.equal(bookingStatusLabel('cancelled', 'refunded'), 'Cancelled · Refunded');
  assert.equal(bookingStatusLabel('pending_payment', 'failed'), 'Payment failed');
});
test('pending confirmed and waitlisted responses never provide premature payment or QR actions', () => {
  const values = [success.value, { ...success.value, status: 'confirmed', paymentStatus: 'paid', holdExpiresAt: null },
    { kind: 'waitlisted', entryId: 'fixture-entry', status: 'waiting', position: 2 }];
  for (const value of values) {
    const html = renderToStaticMarkup(createElement(ReservationNotice, { result: { ok: true, value } }));
    assert.doesNotMatch(html, /<button|href=.*pay|<img|<svg/);
  }
});
