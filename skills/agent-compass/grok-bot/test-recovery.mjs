// Execute both installed upstream gateway modules, with synthetic storage and mocked HTTP only.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import vm from 'node:vm';
import { test } from 'node:test';

const account = 'a'.repeat(64), other = 'b'.repeat(64), key = Buffer.alloc(32, 7);
function encrypted(text) {
  const iv = Buffer.alloc(12, 8), cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  return Buffer.concat([Buffer.from('v10'), iv, cipher.update(text), cipher.final(), cipher.getAuthTag()]).toString('base64');
}

function fixture(bundle) {
  const source = fs.readFileSync(new URL(`./runtime/bin/${bundle}`, import.meta.url), 'utf8');
  const state = { active: account, team: '41', missing: false, legacy: false, calls: [], onRequest: null };
  const modules = {};
  const env = { GROK_BOT_TEST: '1', CURSOR_API_BASE_URL: 'http://127.0.0.1:1' };
  function stored() {
    return { version: 1, 'cursor-accounts': JSON.stringify({ active: state.active, accounts: {
      [state.active]: { 'cursor-access-token': encrypted('synthetic-access'),
        ...(state.team == null ? {} : { 'cursor-selected-team-id': state.unreadable ? 'invalid-encrypted-fixture' : encrypted(state.team) }) }
    }}), ...(state.legacy ? { 'cursor-selected-team-id': encrypted('42') } : {}) };
  }
  const fakeFs = {
    existsSync: p => !state.missing && ['sand-secrets.json', 'Local State', 'desktop-status.json'].includes(path.basename(p)),
    readFileSync: p => {
      if (path.basename(p) === 'sand-secrets.json') return JSON.stringify(stored());
      if (path.basename(p) === 'Local State') return JSON.stringify({ os_crypt: { encrypted_key: Buffer.from('DPAPIfixture').toString('base64') } });
      if (path.basename(p) === 'desktop-status.json') return JSON.stringify({ appVersion: '0.77.0' });
      throw Error('Unexpected filesystem read: ' + path.basename(p));
    }
  };
  const builtin = { 'node:crypto': { ...crypto, default: crypto }, 'node:fs': fakeFs, 'node:path': path, 'node:os': os,
    'node:child_process': { execFileSync() { throw Error('Native auth/process must not run in tests'); } } };
  function load(id) {
    if (builtin[id]) return builtin[id];
    if (modules[id]) return modules[id];
    if (id.endsWith('/store.js') || id.endsWith('/grok-approvals.js')) return {};
    const marker = `    "${id}" (`;
    const start = source.indexOf(marker);
    assert.ok(start >= 0, id);
    const body = source.indexOf('{\n', start) + 1;
    const end = source.indexOf('\n    },\n    "', body);
    assert.ok(end > body, id);
    const output = {};
    modules[id] = output;
    const require = Object.assign(load, { d: (out, getters) => {
      for (const [name, get] of Object.entries(getters)) Object.defineProperty(out, name, { get, enumerable: true });
    }});
    vm.runInNewContext(source.slice(body, end), {
      __webpack_require__: require, __webpack_exports__: output,
      process: { env, platform: 'win32' }, Buffer, AbortSignal, URL, console,
      fetch: async (url, options) => {
        const operation = url.split('/').at(-1);
        state.calls.push({ operation, authorization: options.headers.authorization, team: options.headers['x-cursor-team-id'], version: options.headers['x-cursor-client-version'] });
        assert.equal(options.redirect, 'error');
        const custom = await state.onRequest?.(operation, state.calls.length);
        if (custom) return custom;
        if (operation === 'EnsureSandBox') return Response.json({ gatewayUrl: 'http://127.0.0.1:2', gatewayToken: 'synthetic-gateway' });
        if (operation === 'listAgents') return Response.json([{ id: 'fixture-bot', name: 'Fixture' }]);
        if (operation === 'sendPrompt') return Response.json({ messageId: 'fixture-message' });
        throw Error('Unexpected operation');
      }
    }, { timeout: 1000 });
    return output;
  }
  const gateway = Object.fromEntries(Object.values(load('./src/core/gateway.js')).map(fn => [fn.name, fn]));
  const options = { appSessionOptions: { platform: 'win32', appData: 'fixture', unprotectData: () => key } };
  return { state, gateway, options, env };
}
const hibernated = () => new Response('', { status: 417, headers: {
  'x-anyrun-error-source': 'anyrun-proxy', 'x-anyrun-failure-reason': 'pod_hibernated'
}});

for (const bundle of ['gbot.mjs', 'gbot-flight.mjs']) {
  test(`${bundle}: missing descriptor bootstraps same scope; hibernated read recovers, send exactly once`, async () => {
    const { state, gateway, options } = fixture(bundle);
    let reads = 0;
    state.onRequest = operation => operation === 'listAgents' && ++reads === 1 ? hibernated() : null;
    const session = await gateway.connectGateway(options);
    await gateway.sendPrompt(session, 'fixture-bot', 'synthetic prompt');
    assert.deepEqual(state.calls.map(x => x.operation), ['EnsureSandBox', 'listAgents', 'EnsureSandBox', 'listAgents', 'sendPrompt']);
    for (const call of state.calls) {
      assert.equal(call.authorization, 'Bearer ' + (call.operation === 'EnsureSandBox' ? 'synthetic-access' : 'synthetic-gateway'));
      if (call.operation === 'EnsureSandBox') {
        assert.equal(call.team, '41');
        assert.equal(call.version, '0.77.0');
      }
    }
  });
  test(`${bundle}: write failure is never replayed`, async () => {
    const { state, gateway, options } = fixture(bundle);
    state.onRequest = operation => operation === 'sendPrompt' ? hibernated() : null;
    const session = await gateway.connectGateway(options);
    await assert.rejects(gateway.sendPrompt(session, 'fixture-bot', 'synthetic prompt'), /sendPrompt failed: 417/);
    assert.deepEqual(state.calls.map(x => x.operation), ['EnsureSandBox', 'listAgents', 'sendPrompt']);
  });
  test(`${bundle}: account/team drift before or during Ensure, or before send, aborts`, async () => {
    for (const kind of ['account', 'team']) {
      for (const phase of ['during-ensure', 'before-send', 'during-refresh']) {
        const { state, gateway, options } = fixture(bundle);
        const change = () => { if (kind === 'account') state.active = other; else state.team = '42'; };
        if (phase === 'during-ensure') {
          state.onRequest = operation => { if (operation === 'EnsureSandBox') change(); };
          await assert.rejects(gateway.connectGateway(options), /account|team/);
        } else {
          const session = await gateway.connectGateway(options);
          if (phase === 'before-send') {
            state.onRequest = operation => { if (operation === 'listAgents') change(); };
          } else {
            state.onRequest = operation => {
              if (operation === 'listAgents') return hibernated();
              if (operation === 'EnsureSandBox') change();
            };
          }
          await assert.rejects(gateway.sendPrompt(session, 'fixture-bot', 'synthetic prompt'), /account|team/);
        }
        assert.equal(state.calls.filter(x => x.operation === 'sendPrompt').length, 0);
      }
    }
  });
  test(`${bundle}: missing credentials, unsupported scope, legacy or corrupt team fail before HTTP`, async () => {
    for (const change of [s => s.missing = true, s => s.active = 'unknown-scope',
      s => { s.team = null; s.legacy = true; }, s => s.team = 'not-a-team', s => s.unreadable = true]) {
      const { state, gateway, options } = fixture(bundle);
      change(state);
      await assert.rejects(gateway.connectGateway(options));
      assert.equal(state.calls.length, 0);
    }
  });
  test(`${bundle}: absent team is personal, expired auth fails without token refresh or send`, async () => {
    const { state, gateway, options } = fixture(bundle);
    state.team = null;
    await gateway.connectGateway(options);
    assert.equal(state.calls[0].team, undefined);
    state.calls.length = 0;
    state.onRequest = () => new Response('', { status: 401 });
    await assert.rejects(gateway.connectGateway(options), /EnsureSandBox failed: 401/);
    assert.deepEqual(state.calls.map(x => x.operation), ['EnsureSandBox']);
  });
  test(`${bundle}: explicit gateway overrides never fall back to native credentials`, async () => {
    const { state, gateway, options, env } = fixture(bundle);
    state.missing = true;
    env.GROK_BOT_GATEWAY_URL = 'http://127.0.0.1:2';
    env.GROK_BOT_GATEWAY_TOKEN = 'synthetic-override';
    state.onRequest = () => hibernated();
    const session = await gateway.connectGateway(options);
    await assert.rejects(gateway.listAgents(session), /listAgents failed: 417/);
    assert.deepEqual(state.calls.map(x => x.operation), ['listAgents']);
    assert.equal(state.calls[0].authorization, 'Bearer synthetic-override');
  });
}
