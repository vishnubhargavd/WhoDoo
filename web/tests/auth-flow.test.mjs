import test from 'node:test';
import assert from 'node:assert/strict';
import { safeDestination, submitEmailAuth, verifyAccount, confirmEmail, logout, confirmationMessage } from '../src/modules/membercore/auth-flow.ts';

const user = { id: 'supabase-auth-fixture', email: 'fixture@example.test', user_metadata: { role: 'president' } };
function auth(overrides = {}) {
  return {
    signUp: async () => ({ data: { user, session: null }, error: null }),
    signInWithPassword: async () => ({ data: {}, error: null }),
    getUser: async () => ({ data: { user }, error: null }),
    exchangeCodeForSession: async () => ({ error: null }),
    signOut: async () => ({ error: null }),
    ...overrides
  };
}
const submit = (port, mode = 'login', next = '/account', email = 'fixture@example.test', password = 'fixture-password') => submitEmailAuth(port, mode, email, password, next, 'http://localhost:3000');

test('allows only known relative destinations', () => {
  for (const route of ['/', '/account', '/memberships', '/bookings', '/check-in']) assert.equal(safeDestination(route), route);
});
test('rejects hosts, schemes, encoding, backslashes, control characters and unknown pages', () => {
  for (const route of [undefined, null, {}, ['/bookings'], 'https://evil.test', '//evil.test', '/\\evil.test', '\\evil.test', '/%2f%2fevil.test', '/%5cevil.test', '/account?next=//evil.test', '/account#evil', '/account\r\nLocation:evil', '/auth/callback', '/login', '/unknown', ' /bookings', '/bookings/']) assert.equal(safeDestination(route), '/account');
});
test('missing configuration fails closed for login, signup, account, callback and logout', async () => {
  assert.equal((await submit(null)).ok, false);
  assert.equal((await submit(null, 'signup')).ok, false);
  assert.equal((await verifyAccount(null)).status, 'unavailable');
  assert.equal(await confirmEmail(null, 'fixture-code', '/bookings'), '/login?notice=setup');
  assert.equal((await logout(null)).ok, false);
});
test('invalid credentials are rejected before a network call', async () => {
  const port = auth({ signInWithPassword: () => { throw new Error('must not call'); } });
  assert.match((await submit(port, 'login', '/', 'bad-email')).message, /valid email/);
  assert.match((await submit(port, 'signup', '/', 'fixture@example.test', 'short')).message, /8 characters/);
  assert.match((await submit(port, 'login', '/', 'fixture@example.test', '')).message, /valid email/);
});
test('login passes only email and password and sanitizes the destination', async () => {
  let input;
  const result = await submit(auth({ signInWithPassword: async (value) => { input = value; return { error: null }; } }), 'login', '//evil.test');
  assert.deepEqual(input, { email: 'fixture@example.test', password: 'fixture-password' });
  assert.deepEqual(result, { ok: true, destination: '/account' });
});
test('invalid login does not expose upstream details or identify account existence', async () => {
  const result = await submit(auth({ signInWithPassword: async () => ({ error: { code: 'invalid_credentials', message: 'private upstream detail' } }) }));
  assert.equal(result.ok, false);
  assert.match(result.message, /Check your email and password/);
  assert.doesNotMatch(result.message, /private/);
});
test('unconfirmed email and rate limits give useful messages', async () => {
  for (const [code, expected] of [['email_not_confirmed', /Confirm your email/], ['over_request_rate_limit', /wait/], ['over_email_send_rate_limit', /wait/]]) {
    assert.match((await submit(auth({ signInWithPassword: async () => ({ error: { code } }) }))).message, expected);
  }
});
test('signup sends a PKCE callback URL and no editable role metadata', async () => {
  let input;
  const result = await submit(auth({ signUp: async (value) => { input = value; return { data: { session: null }, error: null }; } }), 'signup', '/bookings');
  assert.equal(input.options.emailRedirectTo, 'http://localhost:3000/auth/callback?next=%2Fbookings');
  assert.equal(input.options.data, undefined);
  assert.deepEqual(result, { ok: true, message: confirmationMessage });
});
test('duplicate signup responses are neutral, including obfuscated users', async () => {
  for (const port of [auth(), auth({ signUp: async () => ({ error: { code: 'user_already_exists' } }) }), auth({ signUp: async () => ({ error: { code: 'email_exists' } }) })]) {
    assert.deepEqual(await submit(port, 'signup'), { ok: true, message: confirmationMessage });
  }
});
test('signup errors and network failures do not expose secrets', async () => {
  assert.match((await submit(auth({ signUp: async () => ({ error: { code: 'weak_password' } }) }), 'signup')).message, /stronger password/);
  assert.equal((await submit(auth({ signUp: async () => ({ error: { code: 'signup_disabled', message: 'sensitive' } }) }), 'signup')).ok, false);
  const result = await submit(auth({ signInWithPassword: async () => { throw new Error('private token'); } }));
  assert.match(result.message, /unavailable/);
  assert.doesNotMatch(result.message, /private token/);
});
test('immediate signup sessions still go to a server-guarded destination', async () => {
  assert.deepEqual(await submit(auth({ signUp: async () => ({ data: { session: {} }, error: null }) }), 'signup', '//evil.test'), { ok: true, destination: '/account' });
});
test('account access uses getUser and never getSession or metadata roles', async () => {
  let calls = 0;
  const result = await verifyAccount(auth({ getUser: async () => { calls++; return { data: { user }, error: null }; }, getSession: () => { throw new Error('unverified'); } }));
  assert.equal(calls, 1);
  assert.deepEqual(result, { status: 'authenticated', authUserId: user.id, email: user.email });
  assert.equal('role' in result, false);
});
test('missing users, expired sessions and forged-cookie errors cannot grant access', async () => {
  for (const result of [{ data: { user: null }, error: null }, { data: { user }, error: { code: 'bad_jwt' } }, { data: { user: null }, error: { code: 'session_not_found' } }]) {
    assert.deepEqual(await verifyAccount(auth({ getUser: async () => result })), { status: 'login', destination: '/login?next=%2Faccount&notice=session' });
  }
});
test('account verification exceptions fail closed without leaking details', async () => {
  const result = await verifyAccount(auth({ getUser: async () => { throw new Error('secret'); } }));
  assert.equal(result.status, 'unavailable');
  assert.doesNotMatch(result.message, /secret/);
});
test('confirmation exchanges the code, verifies identity and redirects safely', async () => {
  const calls = [];
  const port = auth({ exchangeCodeForSession: async (code) => { calls.push(code); return { error: null }; }, getUser: async () => { calls.push('verified'); return { data: { user }, error: null }; } });
  assert.equal(await confirmEmail(port, 'fixture-code', '//evil.test'), '/account');
  assert.deepEqual(calls, ['fixture-code', 'verified']);
});
test('missing, expired, reused or unverifiable confirmation links fail safely', async () => {
  assert.equal(await confirmEmail(auth(), null, '/account'), '/login?notice=confirmation');
  assert.equal(await confirmEmail(auth(), 'x'.repeat(2049), '/account'), '/login?notice=confirmation');
  assert.equal(await confirmEmail(auth({ exchangeCodeForSession: async () => ({ error: { message: 'secret' } }) }), 'fixture', '/'), '/login?notice=confirmation');
  assert.equal(await confirmEmail(auth({ getUser: async () => ({ data: { user: null }, error: null }) }), 'fixture', '/'), '/login?notice=confirmation');
  assert.equal(await confirmEmail(auth({ exchangeCodeForSession: async () => { throw new Error('secret'); } }), 'fixture', '/'), '/login?notice=confirmation');
});
test('logout signs out only this browser and returns a fixed destination', async () => {
  let input;
  const result = await logout(auth({ signOut: async (value) => { input = value; return { error: null }; } }));
  assert.deepEqual(input, { scope: 'local' });
  assert.deepEqual(result, { ok: true, destination: '/login?notice=signed-out' });
});
test('logout errors do not falsely report success or expose upstream details', async () => {
  for (const port of [auth({ signOut: async () => ({ error: { message: 'secret' } }) }), auth({ signOut: async () => { throw new Error('secret'); } })]) {
    const result = await logout(port);
    assert.equal(result.ok, false);
    assert.doesNotMatch(result.message, /secret/);
  }
});
