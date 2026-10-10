import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validatePublicSupabaseConfiguration as validate } from '../src/lib/supabase/config.ts';

const fixtureKey = 'sb_publishable_unit_test_fixture';
test('missing variables return a setup message without throwing', () => {
  for (const args of [[undefined, undefined], ['https://example.test', undefined], ['', fixtureKey]]) {
    const result = validate(...args);
    assert.equal(result.status, 'missing');
    assert.match(result.message, /setup required/i);
  }
});
test('public configuration is accepted without claiming connectivity', () => {
  assert.equal(validate('https://example.test', fixtureKey).status, 'ready');
});
test('localhost HTTP is allowed for development', () => {
  assert.equal(validate('http://127.0.0.1:54321', fixtureKey).status, 'ready');
});
test('unsafe or malformed project URLs are rejected', () => {
  for (const url of ['bad-url', 'http://example.test', 'https://user:password@example.test', 'https://example.test/path', 'https://example.test?key=fixture']) assert.equal(validate(url, fixtureKey).status, 'invalid');
});
test('secret-looking, legacy and malformed keys are rejected', () => {
  for (const key of ['sb_secret_unit_test_fixture', 'eyJ_fake_jwt_fixture', 'sb_publishable_', 'sb_publishable_bad value']) assert.equal(validate('https://example.test', key).status, 'invalid');
});
test('configuration errors do not echo supplied values', () => {
  const result = validate('invalid-url-private-fixture', fixtureKey);
  assert.equal(result.status, 'invalid');
  assert.ok(!result.message.includes('invalid-url-private-fixture'));
  assert.ok(!result.message.includes(fixtureKey));
});
