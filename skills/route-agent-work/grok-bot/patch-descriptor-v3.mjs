// Backport ONLY encryptedPayload's two conditions from upstream PR #143:
// https://github.com/ScriptedAlchemy/grok-bot-cli/commit/e9ac66339d422733a6054ff61795d9841ffe43da
// No auth/decryption/account-selection changes. npm ci restores the original files.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export const bundles = {
  'gbot.mjs': {
    before: '70ed28198daa2a204a649760e01fb60688d27f9aaac66de372e39ea0351a0cdd',
    after: 'e93a44b9731475a70d298955544334e2eb6965f2db6f85d9bb38354364b2c224',
  },
  'gbot-flight.mjs': {
    before: '5e2211f62cd7501ba36f45ef1fc6577f91804f2daf77054d7308759261ff5441',
    after: 'abcb1400c553cff6a96b294e66b0207c7947a16f7f0891658f8fda1dc57ce2f3',
  },
};

export function patchedBundle(name, source) {
  const hash = value => createHash('sha256').update(value).digest('hex');
  const expected = bundles[name];
  if (hash(source) === expected.after) return source;
  if (hash(source) !== expected.before) {
    throw Error(`${name}: unexpected content; restore with npm ci, then reapply this pinned patch.`);
  }
  const patched = source
    .replace('wrapped.version !== 1 && wrapped.version !== 2)',
             'wrapped.version !== 1 && wrapped.version !== 2 && wrapped.version !== 3)')
    .replace('if (wrapped.version === 2) {',
             'if (wrapped.version === 2 || wrapped.version === 3) {');
  if (hash(patched) !== expected.after) throw Error(`${name}: patch result differs from the reviewed bytes.`);
  return patched;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const root = new URL('./node_modules/grok-bot-cli/', import.meta.url);
  if (JSON.parse(readFileSync(new URL('package.json', root), 'utf8')).version !== '0.12.4') {
    throw Error('This patch supports only grok-bot-cli 0.12.4; use the checked-in package-lock.json.');
  }
  // Validate BOTH bundles before writing either, so dependency drift cannot half-apply a patch.
  const updates = Object.keys(bundles).map(name => {
    const path = new URL(`dist/bin/${name}`, root);
    return [path, patchedBundle(name, readFileSync(path, 'utf8'))];
  });
  for (const [path, text] of updates) writeFileSync(path, text, 'utf8');
  console.log('grok-bot-cli 0.12.4: descriptor v3 compatibility verified in both CLI bundles.');
}
