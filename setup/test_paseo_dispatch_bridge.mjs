import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import {setTimeout as pause} from 'node:timers/promises';
import {execute, project, notificationStep, notificationEvents, ensureNotification} from './paseo_dispatch_bridge.mjs';

const notifier = async () => ({status: 'watching', pid: 123, state_file: 'fixture.json'});

const require = createRequire(path.resolve(process.env.ROUTER_PASEO_SDK_ROOT ?? fileURLToPath(new URL('./paseo-bridge/node_modules/', import.meta.url)), '_test.cjs'));
const {SessionInboundMessageSchema} = await import(pathToFileURL(require.resolve('@getpaseo/protocol/messages')).href);

test('SDK creation uses its documented fields, parentage and idempotency', async () => {
  let calls = 0;
  const id = '00112233-4455-4677-8899-aabbccddeeff';
  const request = {provider: 'pi/openai-codex/gpt-6-astra', title: 'fixture', initialPrompt: 'secret-prompt-canary',
    labels: {'router.dispatch_id': id}, settings: {thinkingOptionId: 'high'}};
  const fake = {
    fetchAgent: async parent => {assert.equal(parent, 'parent'); return {agent: {cwd: 'E:/fixture', workspaceId: 'workspace'}};},
    createAgent: async options => {
      calls++;
      assert.equal(options.callerAgentId, 'parent');
      assert.equal(options.idempotencyKey, id);
      assert.equal(options.clientMessageId, id);
      assert.equal(options.agentId, id);
      assert.equal(options.model, 'openai-codex/gpt-6-astra');
      const {provider, model, cwd, title, modeId, thinkingOptionId, featureValues, ...rest} = options;
      // Validate against the installed protocol, without opening a host connection.
      SessionInboundMessageSchema.parse({type: 'agent.create.request', requestId: 'fixture-request', ...rest,
        config: {provider, model, cwd, title, modeId, thinkingOptionId, featureValues}});
      return {id, provider, model, status: 'running', labels: options.labels, lastError: 'secret-error-canary'};
    },
  };
  const result = await execute(fake, {operation: 'create', dispatch_id: id, parent_id: 'parent', request}, notifier);
  assert.equal(calls, 1);
  assert.equal(result.ok, true);
  assert(!JSON.stringify(result).includes('canary'));
});

test('missing parent refuses creation even with a cwd', async () => {
  const request = {provider: 'claude-2/claude-sonnet-5-5', cwd: 'E:/fixture'};
  const fake = {createAgent: async () => {throw new Error('must not create');}};
  for (const parent_id of [null, '', ' ', 'top-level']) {
    assert.deepEqual(await execute(fake, {operation: 'create', parent_id, request}),
      {ok: false, sent: false, error: 'addressable_parent_required'});
  }
});

test('observation resolves actual directory entries then fetches full state', async () => {
  const fake = {
    fetchAgents: async options => {
      assert.equal(options.filter.labels['router.dispatch_id'], 'dispatch');
      assert.equal(options.filter.includeArchived, true);
      return {entries: [{agent: {id: 'child'}, project: {}}]};
    },
    fetchAgent: async id => ({agent: {id, status: 'idle', attentionReason: 'finished'}}),
  };
  assert.equal((await execute(fake, {operation: 'observe', dispatch_id: 'dispatch'})).agent.attentionReason, 'finished');
});

test('missing parent is a definite no-send failure; lost create reply is uncertain', async () => {
  const input = {operation: 'create', parent_id: 'parent', dispatch_id: 'dispatch',
    request: {provider: 'codex/gpt-6-astra', settings: {modeId: 'full-access'}}};
  assert.equal((await execute({fetchAgent: async () => null}, input)).sent, false);
  const result = await execute({fetchAgent: async () => ({agent: {cwd: 'E:/fixture'}}),
    createAgent: async () => {throw new Error('secret-canary');}}, input, notifier);
  assert.equal(result.sent, true);
  assert.equal(result.ok, false);
  assert(!JSON.stringify(result).includes('secret-canary'));
  assert.deepEqual(project({id: 'child', initialPrompt: 'secret-canary'}), {id: 'child'});
});

test('notification readiness precedes creation; unavailable notifier prevents launch', async () => {
  const calls = [];
  const input = {operation: 'create', parent_id: 'parent', dispatch_id: 'dispatch',
    request: {provider: 'codex/model'}};
  const client = {fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
    createAgent: async () => {calls.push('create'); return {id: 'dispatch'};}};
  const result = await execute(client, input, async () => {calls.push('notify'); return notifier();});
  assert.deepEqual(calls, ['notify', 'create']);
  assert.equal(result.completion_notification.status, 'watching');
  calls.length = 0;
  const failed = await execute(client, input, async () => {throw new Error('cannot spawn');});
  assert.equal(failed.sent, false);
  assert.equal(failed.error, 'notification_start_failed');
  assert.equal(calls.length, 0);
});

function watchFixture() {
  const record = {dispatch_id: 'child', parent_id: 'parent', status: 'watching',
    creation_deadline: 100, deadline: 1000, delivered: [], pending: null};
  const calls = [], saved = [];
  let agent = {id: 'child', status: 'idle', attentionReason: 'finished'};
  const client = {
    fetchAgent: async id => ({agent: id === 'parent' ? {status: 'running'} : agent}),
    sendMessage: async (...args) => calls.push(args),
  };
  return {record, client, calls, saved, persist: () => saved.push(structuredClone(record)),
    setAgent: value => {agent = value;}};
}

test('finished and failed workers notify even without native messaging tools', async () => {
  for (const agent of [{status: 'idle', attentionReason: 'finished'}, {status: 'error'},
    {status: 'closed'}, {status: 'running', providerUnavailable: true}]) {
    const f = watchFixture();
    f.setAgent({...agent, supportsNativePaseoTools: false});
    assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0][0], 'parent');
    assert.match(f.calls[0][1], /get_agent_activity.*child/);
    assert.equal(f.calls[0][2].activeTurnBehavior, 'steer');
    assert.equal(f.saved[0].pending.messageId, f.calls[0][2].messageId);
    await notificationStep(f.client, f.record, f.persist, 20);
    assert.equal(f.calls.length, 1);
  }
});

test('permissions notify once per request and do not consume the final callback', async () => {
  const f = watchFixture();
  f.setAgent({status: 'idle', pendingPermissions: [{id: 'approval-1'}, {id: 'approval-2'}]});
  for (let i = 0; i < 4; i++) assert.equal(await notificationStep(f.client, f.record, f.persist, 10), false);
  assert.equal(f.calls.length, 2);
  assert.match(f.calls[0][1], /needs permission/);
  f.setAgent({status: 'idle', attentionReason: 'finished'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 20), true);
  assert.equal(f.calls.length, 3);
});

test('lost acknowledgment reuses the persisted message id after restart', async () => {
  const f = watchFixture();
  f.client.sendMessage = async (...args) => {f.calls.push(args); throw new Error('connection lost');};
  await assert.rejects(notificationStep(f.client, f.record, f.persist, 10));
  const recovered = structuredClone(f.saved.at(-1));
  f.client.sendMessage = async (...args) => {f.calls.push(args);};
  await notificationStep(f.client, recovered, () => {}, 20);
  assert.deepEqual(f.calls[1], f.calls[0]);
  assert.equal(recovered.status, 'delivered');
});

test('initial idle and delayed creation do not falsely finish; vanished workers do', () => {
  const {record} = watchFixture();
  assert.deepEqual(notificationEvents(null, record, 10), []);
  assert.deepEqual(notificationEvents({status: 'idle'}, record, 10), []);
  assert.deepEqual(notificationEvents({status: 'running'}, record, 10), []);
  assert.equal(notificationEvents({status: 'idle'}, record, 20)[0].reason, 'finished');
  assert.equal(notificationEvents(null, record, 30)[0].reason, 'worker unavailable');
});

test('expired observation reports its limit and missing parents terminate without a send', async () => {
  const f = watchFixture();
  f.setAgent({status: 'running'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 1001), true);
  assert.match(f.calls[0][1], /watch expired/);
  const absent = watchFixture();
  absent.client.fetchAgent = async () => null;
  assert.equal(await notificationStep(absent.client, absent.record, absent.persist, 10), true);
  assert.equal(absent.calls.length, 0);
  assert.equal(absent.record.status, 'parent_unavailable');
});

test('detached notifier has one owner, recovers a killed owner, and exits after delivery', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-notify-'));
  const oldEnv = {...process.env};
  const pids = new Set();
  const recordFile = path.join(root, 'paseo-notifications', 'lifecycle.json');
  const running = pid => {try {process.kill(pid, 0); return true;} catch {return false;}};
  const until = async predicate => {
    for (let i = 0; i < 200; i++) {
      if (predicate()) return;
      await pause(50);
    }
    assert.fail('notifier lifecycle deadline exceeded');
  };
  try {
    const sdk = path.join(root, 'node_modules');
    fs.mkdirSync(path.join(sdk, '@getpaseo/client'), {recursive: true});
    fs.writeFileSync(path.join(sdk, '@getpaseo/client/package.json'), JSON.stringify({
      name: '@getpaseo/client', exports: {'./internal/daemon-client': './client.cjs'}}));
    fs.writeFileSync(path.join(sdk, '@getpaseo/client/client.cjs'), `
      const fs = require('node:fs');
      const path = require('node:path');
      const root = process.env.ROUTER_STATE_DIR;
      exports.DaemonClient = class {
        isConnected = false;
        async connect() {this.isConnected = true;}
        async close() {this.isConnected = false;}
        async fetchAgent(id) {
          return {agent: id === 'parent' ? {status: 'running'} :
            JSON.parse(fs.readFileSync(path.join(root, 'agent.json'), 'utf8'))};
        }
        async sendMessage(agent, text, options) {
          fs.appendFileSync(path.join(root, 'messages.jsonl'), JSON.stringify({agent, text, options}) + '\\n');
        }
      };
    `);
    fs.mkdirSync(path.join(sdk, 'ws'));
    fs.writeFileSync(path.join(sdk, 'ws/index.js'), 'exports.WebSocket = class {};');
    process.env.ROUTER_STATE_DIR = root;
    process.env.ROUTER_PASEO_SDK_ROOT = sdk;
    process.env.PASEO_HOME = root;
    fs.writeFileSync(path.join(root, 'agent.json'), JSON.stringify({status: 'running'}));
    const input = {dispatch_id: 'lifecycle', parent_id: 'parent'};
    const initial = await Promise.all([ensureNotification(input), ensureNotification(input)]);
    const first = initial[0];
    pids.add(first.pid);
    for (const receipt of initial) pids.add(receipt.pid);
    assert(initial.every(receipt => receipt.pid === first.pid));
    const concurrent = await Promise.all([ensureNotification(input), ensureNotification(input)]);
    assert(concurrent.every(receipt => receipt.pid === first.pid));
    process.kill(first.pid);
    await until(() => !running(first.pid));
    const recovered = await ensureNotification(input);
    pids.add(recovered.pid);
    assert.notEqual(recovered.pid, first.pid);
    fs.writeFileSync(path.join(root, 'agent.json'), JSON.stringify({status: 'idle', attentionReason: 'finished'}));
    await until(() => JSON.parse(fs.readFileSync(recordFile, 'utf8')).status === 'delivered');
    await until(() => !running(recovered.pid));
    assert.equal(fs.existsSync(recordFile + '.lock'), false);
    assert.equal((await ensureNotification(input)).status, 'delivered');
    assert.equal(fs.readFileSync(path.join(root, 'messages.jsonl'), 'utf8').trim().split('\n').length, 1);
  } finally {
    if (fs.existsSync(recordFile)) pids.add(JSON.parse(fs.readFileSync(recordFile, 'utf8')).pid);
    for (const pid of pids) if (running(pid)) process.kill(pid);
    for (const name of ['ROUTER_STATE_DIR', 'ROUTER_PASEO_SDK_ROOT', 'PASEO_HOME']) {
      if (oldEnv[name] === undefined) delete process.env[name]; else process.env[name] = oldEnv[name];
    }
    await until(() => [...pids].every(pid => !running(pid)));
    fs.rmSync(root, {recursive: true, force: true});
  }
});
