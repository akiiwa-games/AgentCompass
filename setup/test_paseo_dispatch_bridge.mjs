import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import {setTimeout as pause} from 'node:timers/promises';
import {execute, project, notificationStep, notificationEvents, ensureNotification, notificationReceipt,
  creationFailureClass, creationRecord} from './paseo_dispatch_bridge.mjs';

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
      const {provider, model, cwd, title, modeId, thinkingOptionId, featureValues, onEvent, ...rest} = options;
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

test('SDK resume sends message, updates labels, and watches target agent id', async () => {
  const sent = [], updated = [];
  const dispatchId = '11223344-5566-4777-8899-aabbccddeeff';
  const workerId = 'existing-worker-id';
  const request = {
    provider: 'claude-lab/claude-sonnet-5-5',
    title: 'Step 1A-Rev',
    initialPrompt: 'secret-resume-canary',
    labels: {'router.dispatch_id': dispatchId, 'router.parent_id': 'parent', 'router.task_id': 'task-rev'},
    settings: {modeId: 'bypassPermissions', thinkingOptionId: 'high'},
  };
  let workerState = {
    id: workerId,
    provider: 'claude-lab',
    model: 'claude-sonnet-5-5',
    currentModeId: 'bypassPermissions',
    thinkingOptionId: 'high',
    status: 'idle',
    attentionReason: 'finished',
    updatedAt: '2026-11-03T00:00:00.000Z',
    labels: {'router.dispatch_id': 'old-dispatch', 'router.parent_id': 'parent'},
  };
  const fake = {
    fetchAgent: async id => ({agent: id === 'parent' ? {cwd: 'E:/fixture', status: 'running'} : workerState}),
    sendMessage: async (id, text, options) => {
      sent.push({id, text, options});
      workerState = {...workerState, status: 'running', attentionReason: undefined,
        lastUserMessageAt: '2026-11-03T00:01:00.000Z'};
    },
    updateAgent: async (id, patch) => {
      updated.push({id, patch});
      workerState = {...workerState, name: patch.name, labels: patch.labels};
    },
  };
  const result = await execute(fake, {
    operation: 'resume',
    dispatch_id: dispatchId,
    parent_id: 'parent',
    agent_id: workerId,
    request,
  }, notifier);
  assert.equal(result.ok, true);
  assert.equal(result.sent, true);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].id, workerId);
  assert.equal(sent[0].options.messageId, dispatchId);
  assert.equal(updated.length, 1);
  assert.equal(updated[0].id, workerId);
  assert.equal(updated[0].patch.labels['router.dispatch_id'], dispatchId);
  assert.equal(result.agent.id, workerId);
  assert.equal(result.agent.status, 'running');
  assert(!JSON.stringify(result).includes('canary'));

  // Before the resumed dispatch label is bound, stale idle/finished state from the previous turn must not fire
  const record = {dispatch_id: dispatchId, parent_id: 'parent', agent_id: workerId,
    status: 'watching', creation_deadline: 100, deadline: 1000, delivered: [], pending: null};
  assert.deepEqual(notificationEvents({
    id: workerId, status: 'idle', attentionReason: 'finished',
    labels: {'router.dispatch_id': 'old-dispatch'},
  }, record, 10), []);
  // Bound but the new turn not yet delivered: the previous turn's finish must not fire either
  assert.deepEqual(notificationEvents({
    id: workerId, status: 'idle', attentionReason: 'finished', lastUserMessageAt: '1969-12-31T23:00:00.000Z',
    labels: {'router.dispatch_id': dispatchId},
  }, {...record, creation_deadline: 0}, 10), []);
  // Once bound and finished, notificationStep queries workerId and includes workerId in the message
  workerState = {...workerState, status: 'idle', attentionReason: 'finished'};
  const parentMsgs = [];
  const stepClient = {
    fetchAgent: fake.fetchAgent,
    sendMessage: async (...args) => parentMsgs.push(args),
  };
  assert.equal(await notificationStep(stepClient, record, () => {}, 20), true);
  assert.equal(parentMsgs.length, 1);
  assert.match(parentMsgs[0][1], new RegExp(`Worker: ${workerId}`));
  assert.match(parentMsgs[0][1], new RegExp(`agentId: ${workerId}`));
});

test('missing parent refuses creation even with a cwd', async () => {
  const request = {provider: 'claude-lab/claude-sonnet-5-5', cwd: 'E:/fixture'};
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

test('only a matching definitive creation failure terminates the notification and claim', async () => {
  const old = process.env.ROUTER_STATE_DIR;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-creation-failure-'));
  process.env.ROUTER_STATE_DIR = root;
  try {
    const folder = path.join(root, 'paseo-notifications'); fs.mkdirSync(folder);
    for (const [id, unknown, key, expected] of [
      ['definite', false, 'definite', false], ['unknown', true, 'unknown', true],
      ['missing', undefined, 'missing', true], ['mismatch', false, 'another', true],
    ]) {
      const file = path.join(folder, `${id}.json`);
      fs.writeFileSync(file, JSON.stringify({parent_id: 'parent', status: 'watching'}));
      const fake = {fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
        createAgent: async options => {
          options.onEvent({kind: 'agent', idempotencyKey: key, phase: 'failed', outcomeUnknown: unknown});
          throw new Error('secret-failure-canary');
        }};
      const result = await execute(fake, {operation: 'create', dispatch_id: id, parent_id: 'parent',
        request: {provider: 'pi/openai-codex/gpt-6-astra'}}, notifier);
      assert.equal(result.sent, expected);
      assert.equal(fs.existsSync(`${file}.creation-failed`), !expected);
      assert.equal(JSON.parse(fs.readFileSync(file)).status, expected ? 'watching' : 'creation_failed');
      assert(!JSON.stringify(result).includes('canary'));
    }
  } finally {
    if (old === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = old;
    fs.rmSync(root, {recursive: true, force: true});
  }
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

test('a create only notifies once it has ended: never for slowness, whatever the deadline', () => {
  const record = {dispatch_id: 'd', creation_deadline: 100};
  for (const phase of ['accepted', 'workspace_ready', 'agent_ready', 'prompt_started'])
    assert.deepEqual(notificationEvents(null, record, 10_000, {phase}), [], phase);
  assert.deepEqual(notificationEvents(null, record, 99, null), [], 'not admitted, inside the deadline');
  assert.equal(notificationEvents(null, record, 100, null)[0].reason, 'creation failed');
  assert.equal(notificationEvents(null, record, 10, {phase: 'failed'})[0].reason, 'creation failed');
  assert.deepEqual(notificationEvents(null, record, 10_000), [], 'a record not read decides nothing');
});

test('a failed create tells the parent the brief can be rolled again', async () => {
  const f = watchFixture();
  const fetchAgent = f.client.fetchAgent;
  f.client.fetchAgent = async id => id === f.record.parent_id ? fetchAgent(id) : null;
  f.client.sendCorrelatedSessionRequest = async () => ({snapshot: {phase: 'failed', error: 'secret-sdk-canary'}});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.match(f.calls[0][1], /safe to roll this brief again/);
  assert(!f.calls[0][1].includes('get_agent_activity'));
  assert(!JSON.stringify(f.record).includes('canary'));
});

test('the creation record is read through a schema-valid one-shot request, keeping only fixed fields', async () => {
  const seen = [];
  const client = {sendCorrelatedSessionRequest: async request => {
    seen.push(request);
    SessionInboundMessageSchema.parse({...request.message, requestId: 'fixture-request'});
    return {snapshot: seen.length === 1 ? null : {phase: 'failed', outcomeUnknown: false, agentId: 'd', error: 'Timed out refreshing x secret-canary'}};
  }};
  assert.equal(await creationRecord(client, 'd'), null);
  const failed = await creationRecord(client, 'd');
  assert.deepEqual(failed, {phase: 'failed', outcome_unknown: false, agent_id: 'd', failure_class: 'provider_refresh_timeout'});
  assert.equal(seen[0].message.subscribe, false);
  await assert.rejects(creationRecord({sendCorrelatedSessionRequest: async () => ({snapshot: null, error: 'boom'})}, 'd'), /creation_unobservable/);
});

test('observe reports the creation record when no agent exists, and finds an agent created meanwhile', async () => {
  const none = {fetchAgents: async () => ({entries: []}), fetchAgent: async () => null,
    sendCorrelatedSessionRequest: async () => ({snapshot: {phase: 'accepted', agentId: 'd'}})};
  const pending = await execute(none, {operation: 'observe', dispatch_id: 'observe-creation', agent_id: null});
  assert.deepEqual([pending.ok, pending.agent, pending.creation.phase], [true, null, 'accepted']);
  const raced = {...none, fetchAgent: async id => ({agent: {id, status: 'running', labels: {}}}),
    sendCorrelatedSessionRequest: async () => ({snapshot: {phase: 'completed', agentId: 'd'}})};
  assert.equal((await execute(raced, {operation: 'observe', dispatch_id: 'observe-creation', agent_id: null})).agent.id, 'd');
  const bound = {...none, sendCorrelatedSessionRequest: async () => assert.fail('a bound agent has no create to read')};
  assert.equal('creation' in await execute(bound, {operation: 'observe', dispatch_id: 'observe-creation', agent_id: 'gone'}), false);
});

// What the daemon answers for a completed record whose agent is gone (real run against a real record, 2026-11-07).
const agentRemoved = async () => ({snapshot: null, error: 'Previously created agent no longer exists'});

test('a worker removed before it was observed reads as a completed record with no agent, not an unreadable host', async () => {
  assert.deepEqual(await creationRecord({sendCorrelatedSessionRequest: agentRemoved}, 'd'),
    {phase: 'completed', outcome_unknown: false, agent_id: null});
  const gone = {fetchAgents: async () => ({entries: []}), fetchAgent: async () => null, sendCorrelatedSessionRequest: agentRemoved};
  const observed = await execute(gone, {operation: 'observe', dispatch_id: 'observe-removed', agent_id: null});
  assert.deepEqual([observed.ok, observed.agent, observed.creation.phase, observed.creation.agent_id], [true, null, 'completed', null]);
});

test('the watcher reports a removed worker once and stops', async () => {
  const f = watchFixture();
  const fetchAgent = f.client.fetchAgent;
  f.client.fetchAgent = async id => id === f.record.parent_id ? fetchAgent(id) : null;
  f.client.sendCorrelatedSessionRequest = agentRemoved;
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0][1], /removed before this dispatch observed it/);
  assert(!f.calls[0][1].includes('get_agent_activity'));
  assert.equal(f.record.status, 'delivered');
});

test('a record that cannot be read waits inside the watch and never holds back its limit', async () => {
  const f = watchFixture();
  const fetchAgent = f.client.fetchAgent;
  f.client.fetchAgent = async id => id === f.record.parent_id ? fetchAgent(id) : null;
  f.client.sendCorrelatedSessionRequest = async () => ({snapshot: null, error: 'boom'});
  await assert.rejects(notificationStep(f.client, f.record, f.persist, 10), /creation_unobservable/);
  assert.equal(f.calls.length, 0);
  assert.equal(await notificationStep(f.client, f.record, f.persist, f.record.deadline), true);
  assert.match(f.calls[0][1], /watch expired/);
});

test('project carries where the worker runs', () => {
  assert.equal(project({id: 'a', cwd: 'E:/fixture'}).cwd, 'E:/fixture');
});

test('a create past its send window is never sent', async () => {
  let created = 0;
  const fake = {fetchAgent: async () => ({agent: {cwd: 'E:/fixture'}}), createAgent: async () => {created++; return {};}};
  const request = {provider: 'claude/x', labels: {}};
  const input = {operation: 'create', dispatch_id: 'send-window', parent_id: 'parent', request};
  const late = await execute(fake, {...input, send_by: new Date(Date.now() - 1000).toISOString()}, notifier);
  assert.deepEqual([late.ok, late.sent, late.error, created], [false, false, 'create_send_window_elapsed', 0]);
  await execute(fake, {...input, send_by: new Date(Date.now() + 60000).toISOString()}, notifier);
  assert.equal(created, 1);
});

test('workspace_agents lists only router agents of that workspace, across pages', async () => {
  const filters = [];
  const fake = {fetchAgents: async ({filter, page}) => {
    filters.push(filter);
    return page.cursor
      ? {entries: [{agent: {id: 'c', status: 'initializing', workspaceId: 'W', labels: {'router.dispatch_id': 'dc'}}}], pageInfo: {hasMore: false, nextCursor: null}}
      : {entries: [{agent: {id: 'a', status: 'running', workspaceId: 'W', labels: {'router.dispatch_id': 'da'}}},
          {agent: {id: 'b', status: 'running', workspaceId: 'other', labels: {'router.dispatch_id': 'db'}}},
          {agent: {id: 'h', status: 'running', workspaceId: 'W', labels: {}}}], pageInfo: {hasMore: true, nextCursor: 'next'}};
  }};
  const result = await execute(fake, {operation: 'workspace_agents', workspace_id: 'W'});
  assert.deepEqual(result.agents.map(a => [a.id, a.dispatch_id]), [['a', 'da'], ['c', 'dc']]);
  assert.deepEqual(filters[0].statuses, ['initializing', 'running']);
  SessionInboundMessageSchema.parse({type: 'fetch_agents_request', requestId: 'fixture-request', filter: filters[0], page: {limit: 200}});
});

test('expired observation reports its limit and missing parents terminate without a send', async () => {
  const f = watchFixture();
  f.setAgent({status: 'running'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 1001), true);
  assert.match(f.calls[0][1], /watch expired/);
  const absent = watchFixture();
  absent.client.fetchAgent = async () => null;
  absent.client.sendCorrelatedSessionRequest = async () => ({snapshot: null});
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
    // A definitive creation rejection must also stop its real watcher process.
    fs.writeFileSync(path.join(root, 'agent.json'), 'null');
    const rejected = await ensureNotification({dispatch_id: 'rejected', parent_id: 'parent'});
    pids.add(rejected.pid);
    const rejectedFile = path.join(root, 'paseo-notifications', 'rejected.json');
    await execute({fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
      createAgent: async options => {
        options.onEvent({kind: 'agent', idempotencyKey: 'rejected', phase: 'failed', outcomeUnknown: false});
        throw new Error('fixture rejection');
      }}, {operation: 'create', dispatch_id: 'rejected', parent_id: 'parent',
        request: {provider: 'pi/model'}}, async () => rejected);
    await until(() => !running(rejected.pid));
    assert.equal(JSON.parse(fs.readFileSync(rejectedFile)).status, 'creation_failed');
    assert.equal(fs.existsSync(rejectedFile + '.lock'), false);
    assert.equal((await ensureNotification({dispatch_id: 'rejected', parent_id: 'parent'})).status, 'creation_failed');
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

test('resume binds the dispatch labels before sending, and a refused send gives the old labels back', async () => {
  const dispatchId = '22334455-6677-4888-9999-aabbccddeeff';
  const request = {provider: 'antigravity-resilient/gemini-4-argon-high', title: 'Drift round',
    initialPrompt: 'secret-resume-canary', labels: {'router.dispatch_id': dispatchId, 'router.parent_id': 'parent'},
    settings: {modeId: 'full-access'}};
  const oldLabels = {'router.dispatch_id': 'old-dispatch', 'router.parent_id': 'parent'};
  const make = (sendMessage) => {
    const calls = [];
    let worker = {id: 'worker', title: 'Old title', provider: 'antigravity-resilient', model: 'gemini-4-argon-high',
      currentModeId: 'full-access', status: 'idle', attentionReason: 'finished', labels: oldLabels};
    return {calls, client: {
      fetchAgent: async id => ({agent: id === 'parent' ? {status: 'running'} : worker}),
      updateAgent: async (id, patch) => { calls.push(['update', patch.labels['router.dispatch_id'], patch.name]);
        worker = {...worker, labels: patch.labels}; },
      sendMessage: async (...args) => { calls.push(['send', worker.labels['router.dispatch_id']]); return sendMessage(...args); },
    }};
  };
  const input = {operation: 'resume', dispatch_id: dispatchId, parent_id: 'parent', agent_id: 'worker', request};

  // 11-06: the send outlived the caller's 25 s timeout; the worker must already carry this dispatch's labels.
  const ok = make(async () => {});
  assert.equal((await execute(ok.client, input, notifier)).ok, true);
  assert.deepEqual(ok.calls, [['update', dispatchId, 'Drift round'], ['send', dispatchId]]);

  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-resume-'));
  const previous = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = stateDir;
  try {
    const refused = make(async () => { throw new Error('send refused'); });
    const result = await execute(refused.client, input, notifier);
    assert.deepEqual([result.ok, result.sent, result.error], [false, false, 'host_resume_failed']);
    assert.deepEqual(refused.calls, [['update', dispatchId, 'Drift round'], ['send', dispatchId],
      ['update', 'old-dispatch', 'Old title']]);
  } finally {
    if (previous === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = previous;
    fs.rmSync(stateDir, {recursive: true, force: true});
  }
});

test('the notifier keeps its first terminal sighting and the receipt reports it', async () => {
  const dispatchId = '33445566-7788-4999-aaaa-bbccddeeff00';
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-terminal-'));
  const previous = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = stateDir;
  try {
    let worker = {id: dispatchId, status: 'idle', attentionReason: 'finished', labels: {'router.dispatch_id': dispatchId}};
    let parent = {status: 'closed'};
    const client = {fetchAgent: async id => ({agent: id === 'parent' ? parent : worker}), sendMessage: async () => {}};
    const record = {dispatch_id: dispatchId, parent_id: 'parent', status: 'watching', creation_deadline: 0,
      deadline: 10_000, delivered: [], pending: null, started: true, pid: process.pid};
    const file = path.join(stateDir, 'paseo-notifications', `${dispatchId}.json`);
    fs.mkdirSync(path.dirname(file), {recursive: true});
    const persist = () => fs.writeFileSync(file, JSON.stringify(record));
    // Even when the parent is already gone, the finish is recorded for the registry.
    assert.equal(await notificationStep(client, record, persist, 5_000), true);
    assert.equal(record.status, 'parent_unavailable');
    assert.deepEqual(record.terminal, {reason: 'finished', status: 'idle', attentionReason: 'finished',
      at: new Date(5_000).toISOString()});
    assert.deepEqual(notificationReceipt(file).terminal, record.terminal);

    // A live parent: the sighting is kept once, later lifecycle (closed after idle collection) does not replace it.
    parent = {status: 'running'};
    const second = {...record, status: 'watching', terminal: undefined, delivered: []};
    assert.equal(await notificationStep(client, second, () => {}, 6_000), true);
    worker = {...worker, status: 'closed', attentionReason: null};
    second.status = 'watching'; second.delivered = [];
    await notificationStep(client, second, () => {}, 9_000);
    assert.equal(second.terminal.reason, 'finished');
    assert.equal(second.terminal.at, new Date(6_000).toISOString());
  } finally {
    if (previous === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = previous;
    fs.rmSync(stateDir, {recursive: true, force: true});
  }
});

test('a definitive create failure carries a fixed failure class, never the SDK text', () => {
  assert.equal(creationFailureClass({error: 'Timed out refreshing Claude 2 after 120000ms; pending: models'}),
    'provider_refresh_timeout');
  assert.equal(creationFailureClass({error: "Agent x references unavailable provider 'antigravity-resilient'"}),
    'provider_unavailable');
  assert.equal(creationFailureClass({error: 'Unknown model gpt-x'}), 'model_unavailable');
  assert.equal(creationFailureClass({error: 'something with secret-prompt-canary'}), 'unknown');
  assert.equal(creationFailureClass(undefined), 'unknown');
});
