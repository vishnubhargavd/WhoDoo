import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveApplicationIdentity } from '../src/modules/membercore/application-identity.ts';

const authId = '3fc7cd1d-2b77-411f-a792-58f06efb4401';
const appId = '15d37155-26ef-4e71-b08b-a52cc99db5d7';
const clubId = 'd65c9626-4450-4f72-b923-24d2b3f7056d';
const user = { id: authId, email: 'member@example.test', user_metadata: { role: 'president', club_id: clubId } };
const auth = (candidate = user, error = null) => ({ getUser: async () => ({ data: { user: candidate }, error }) });
const row = { id: appId, auth_user_id: authId, club_id: clubId, role: 'member', is_active_member: false };
const repository = (result) => ({ findBySupabaseAuthUserId: async (id) => {
  assert.equal(id, authId);
  return typeof result === 'function' ? result() : result;
} });

test('maps verified Supabase subject to existing application ID and database role', async () => {
  const result = await resolveApplicationIdentity(auth(), repository(row));
  assert.deepEqual(result, { status: 'mapped', identity: {
    supabaseAuthUserId: authId, applicationUserId: appId, clubId, role: 'member'
  } });
  assert.notEqual(result.identity.role, user.user_metadata.role);
});

test('authenticated signup without an approved mapping stays unmapped', async () => {
  assert.deepEqual(await resolveApplicationIdentity(auth(), repository(null)), { status: 'authenticated-unmapped' });
});

test('signed out, rejected identity and missing auth configuration fail closed', async () => {
  assert.equal((await resolveApplicationIdentity(auth(null), repository(row))).status, 'unauthenticated');
  assert.equal((await resolveApplicationIdentity(auth(user, { code: 'bad_jwt' }), repository(row))).status, 'unauthenticated');
  assert.equal((await resolveApplicationIdentity(null, repository(row))).status, 'unavailable');
});

test('mapping must match the verified Auth UUID and valid application identifiers', async () => {
  for (const invalid of [
    { ...row, auth_user_id: appId },
    { ...row, id: 'member-1' },
    { ...row, club_id: '' },
    { ...row, role: 'owner' },
    { ...row, role: 'president\n' },
  ]) {
    assert.equal((await resolveApplicationIdentity(auth(), repository(invalid))).status, 'unavailable');
  }
});

test('repository exceptions never turn into an unmapped account or leak details', async () => {
  const result = await resolveApplicationIdentity(auth(), repository(() => { throw new Error('connection-secret'); }));
  assert.deepEqual(result, { status: 'unavailable' });
  assert.doesNotMatch(JSON.stringify(result), /connection-secret/);
});

test('membership compatibility flag is not returned as proof of eligibility or payment', async () => {
  const result = await resolveApplicationIdentity(auth(), repository({ ...row, is_active_member: true, dues_paid: true }));
  assert.equal(result.status, 'mapped');
  assert.equal('is_active_member' in result.identity, false);
  assert.equal('dues_paid' in result.identity, false);
});
