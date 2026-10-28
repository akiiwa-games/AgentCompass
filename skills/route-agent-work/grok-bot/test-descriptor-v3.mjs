import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import vm from 'node:vm';
import { bundles, patchedBundle } from './patch-descriptor-v3.mjs';

test('--check verifies without installing a missing patch', () => {
  const root = mkdtempSync(join(tmpdir(), 'grok-bot-patch-check-'));
  try {
    const dependency = join(root, 'node_modules', 'grok-bot-cli');
    mkdirSync(join(dependency, 'dist', 'bin'), { recursive: true });
    writeFileSync(join(dependency, 'package.json'), JSON.stringify({ version: '0.12.4' }));
    const script = join(root, 'patch-descriptor-v3.mjs');
    writeFileSync(script, readFileSync(new URL('./patch-descriptor-v3.mjs', import.meta.url)));
    for (const name of Object.keys(bundles)) {
      writeFileSync(join(dependency, 'dist', 'bin', name),
        readFileSync(new URL(`./node_modules/grok-bot-cli/dist/bin/${name}`, import.meta.url)));
    }
    assert.equal(spawnSync(process.execPath, [script, '--check']).status, 0);
    const target = join(dependency, 'dist', 'bin', 'gbot.mjs');
    const unpatched = readFileSync(target, 'utf8')
      .replace('wrapped.version !== 1 && wrapped.version !== 2 && wrapped.version !== 3)',
               'wrapped.version !== 1 && wrapped.version !== 2)')
      .replace('if (wrapped.version === 2 || wrapped.version === 3) {', 'if (wrapped.version === 2) {');
    writeFileSync(target, unpatched);
    assert.notEqual(spawnSync(process.execPath, [script, '--check']).status, 0);
    assert.equal(readFileSync(target, 'utf8'), unpatched);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

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
