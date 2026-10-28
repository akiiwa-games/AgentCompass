import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import { bundles, patchedBundle } from './patch-descriptor-v3.mjs';

for (const name of Object.keys(bundles)) {
  const source = readFileSync(new URL(`./node_modules/grok-bot-cli/dist/bin/${name}`, import.meta.url), 'utf8');
  // Execute the installed upstream parser itself, without loading its auth, filesystem or gateway code.
  const start = source.indexOf('class GrokBotGatewaySessionError extends Error {');
  const end = source.indexOf('function safeStorageKey(', start);
  assert.ok(start >= 0 && end > start, `${name}: expected upstream parser`);
  const parse = vm.runInNewContext(`${source.slice(start, end)}; encryptedPayload`, {}, { timeout: 1000 });

  test(`${name}: v3 uses the v2 entry layout; v1/v2 remain compatible`, () => {
    assert.equal(parse({ version: 1, encrypted: 'synthetic-v1' }), 'synthetic-v1');
    for (const version of [2, 3]) {
      assert.equal(parse({ version, entries: { fixture: { savedAtMs: 1, encrypted: 'synthetic-entry' } } }), 'synthetic-entry');
    }
  });

  test(`${name}: ambiguous, empty and future-version descriptors remain rejected`, () => {
    const rows = [
      [{ version: 4, entries: { a: { encrypted: 'synthetic' } } }, 'UNSUPPORTED_VERSION'],
      ...[2, 3].flatMap(version => [
        [{ version, entries: { a: { encrypted: 'a' }, b: { encrypted: 'b' } } }, 'AMBIGUOUS_ENTRIES'],
        [{ version, entries: {} }, 'EMPTY_ENTRIES'],
        [{ version, entries: { a: { encrypted: '' } } }, 'MISSING_ENCRYPTED_PAYLOAD'],
        [{ version, entries: { a: {} } }, 'MISSING_ENCRYPTED_PAYLOAD'],
      ]),
    ];
    for (const [input, code] of rows) assert.throws(() => parse(input), error => error.code === code);
  });

  test(`${name}: patch is repeatable and rejects modified dependency bytes`, () => {
    assert.equal(patchedBundle(name, source), source);
    assert.throws(() => patchedBundle(name, source + '\n// unreviewed drift'), /unexpected content/);
  });
}
