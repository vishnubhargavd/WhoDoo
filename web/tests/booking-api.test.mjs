import test from 'node:test';
import assert from 'node:assert/strict';
import { createBookingApi, parseBookingApiResponse } from '../src/modules/fairseat/booking-api.ts';

const request = {
  eventId: 'event-1', ticketCategoryId: 'category-1', quantity: 2,
  idempotencyKey: 'retry-key-00000001'
};
const command = { request, acceptedQuoteId: 'quote-00000001' };
const auth = async () => 'Bearer server-issued-token';

function fakeResponse(body, ok = true) {
  return { ok, json: async () => body };
}

test('missing endpoint fails clearly and never calls the transport', async () => {
  let calls = 0;
  const api = createBookingApi({ reservationEndpoint: undefined, getAuthorizationHeader: auth,
    fetcher: async () => { calls += 1; return fakeResponse({}); } });
  assert.deepEqual(await api.reserve(command), {
    ok: false, error: 'not_configured', message: 'FairSeat booking API is not connected.'
  });
  assert.equal(calls, 0);
});

test('requires a valid explicit secure endpoint', async () => {
  const api = createBookingApi({ reservationEndpoint: 'https://user:pass@example.test/reserve#fragment', getAuthorizationHeader: auth });
  assert.equal((await api.reserve(command)).error, 'invalid_configuration');
  const httpApi = createBookingApi({ reservationEndpoint: 'http://api.example.test/reserve', getAuthorizationHeader: auth });
  assert.equal((await httpApi.reserve(command)).error, 'invalid_configuration');
  const queryApi = createBookingApi({ reservationEndpoint: 'https://api.example.test/reserve?token=unsafe', getAuthorizationHeader: auth });
  assert.equal((await queryApi.reserve(command)).error, 'invalid_configuration');
});

test('sends only parsed booking selectors, accepted quote reference and server auth', async () => {
  let captured;
  const api = createBookingApi({
    reservationEndpoint: 'https://booking.example.test/approved/path', getAuthorizationHeader: auth,
    fetcher: async (url, init) => {
      captured = { url, init };
      return fakeResponse({ kind: 'reserved', bookingId: 'booking-1', status: 'pending_payment',
        paymentStatus: 'pending', totalPricePaise: 20002, holdExpiresAt: '2026-10-10T12:05:00Z' });
    }
  });
  const result = await api.reserve(command);
  assert.equal(result.ok, true);
  assert.equal(captured.url, 'https://booking.example.test/approved/path');
  assert.equal(captured.init.method, 'POST');
  assert.equal(captured.init.cache, 'no-store');
  assert.equal(captured.init.headers.authorization, 'Bearer server-issued-token');
  assert.deepEqual(JSON.parse(captured.init.body), { ...request, acceptedQuoteId: 'quote-00000001' });
});

test('rejects client-supplied price, identity, membership and authority fields before transport', async () => {
  let calls = 0;
  const api = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: auth,
    fetcher: async () => { calls += 1; return fakeResponse({}); } });
  for (const extra of [{ pricePaise: 1 }, { purchaserId: 'user-1' }, { membership: { active: true } }, { role: 'president' }]) {
    assert.equal((await api.reserve({ ...command, request: { ...request, ...extra } })).error, 'invalid_request');
  }
  assert.equal(calls, 0);
});

test('requires server-provided bearer authorization and maps provider failures safely', async () => {
  const noAuth = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: async () => null });
  assert.equal((await noAuth.reserve(command)).error, 'not_authenticated');
  const failedAuth = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: async () => { throw new Error('secret'); } });
  assert.equal((await failedAuth.reserve(command)).message, 'Sign in again before booking.');
});

test('validates response variants and refuses malformed or invented success', () => {
  assert.deepEqual(parseBookingApiResponse({ kind: 'waitlisted', entryId: 'wait-1', status: 'waiting', position: 1 }),
    { kind: 'waitlisted', entryId: 'wait-1', status: 'waiting', position: 1 });
  assert.deepEqual(parseBookingApiResponse({ kind: 'price_changed', message: 'Review the updated quote.' }),
    { kind: 'price_changed', message: 'Review the updated quote.' });
  assert.equal(parseBookingApiResponse({ kind: 'reserved', bookingId: 'b', status: 'confirmed', paymentStatus: 'pending', totalPricePaise: 10, holdExpiresAt: null }), null);
  assert.equal(parseBookingApiResponse({ kind: 'reserved', bookingId: 'b', status: 'confirmed', paymentStatus: 'paid', totalPricePaise: Number.MAX_SAFE_INTEGER + 1, holdExpiresAt: null }), null);
  assert.equal(parseBookingApiResponse({ kind: 'success' }), null);
  assert.equal(parseBookingApiResponse({ kind: 'waitlisted', entryId: 'w', status: 'waiting', position: 1, authToken: 'secret' }), null);
});

test('transport, HTTP and unreadable response errors do not expose sensitive details', async () => {
  const network = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: auth,
    fetcher: async () => { throw new Error('private token'); } });
  assert.deepEqual(await network.reserve(command), { ok: false, error: 'network_error', message: 'The booking service could not be reached.' });
  const http = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: auth,
    fetcher: async () => fakeResponse({ kind: 'reserved', bookingId: 'b', status: 'confirmed', paymentStatus: 'paid', totalPricePaise: 100, holdExpiresAt: null }, false) });
  assert.equal((await http.reserve(command)).error, 'http_error');
  const unreadable = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: auth,
    fetcher: async () => ({ ok: true, json: async () => { throw new Error('bad json'); } }) });
  assert.equal((await unreadable.reserve(command)).error, 'invalid_response');
});

test('invalid command is rejected locally', async () => {
  const api = createBookingApi({ reservationEndpoint: 'https://booking.example.test/reserve', getAuthorizationHeader: auth });
  assert.equal((await api.reserve({ ...command, acceptedQuoteId: '' })).error, 'invalid_request');
  assert.equal((await api.reserve({ ...command, extra: true })).error, 'invalid_request');
});
