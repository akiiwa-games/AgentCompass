import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import {setTimeout as pause} from 'node:timers/promises';
import {execute, project, normalizeProviderModels, notificationStep, notificationEvents, ensureNotification, notificationReceipt,
  creationFailureClass, creationRecord, subscriptionLapsed, providerRetry, retryStreak, retryIsStuck,
  workedBeforeRetrying, endedOnReply, turnFailure, RETRY_STUCK_CODE, TURN_FAILED_CODE} from './paseo_dispatch_bridge.mjs';

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
  assert.deepEqual(failed, {phase: 'failed', outcome_unknown: false, agent_id: 'd', agent_created: false, failure_class: 'provider_refresh_timeout'});
  assert.equal(seen[0].message.subscribe, false);
  await assert.rejects(creationRecord({sendCorrelatedSessionRequest: async () => ({snapshot: null, error: 'boom'})}, 'd'), /creation_unobservable/);
});

test("a definitive create failure forwards the provider's own error code, never its text", async () => {
  // 2026-11-09: the Antigravity plugin's ProviderActionError("INVALID_MODEL") reaches the daemon's creation record as errorCode.
  const refused = {kind: 'agent', idempotencyKey: 'd', phase: 'failed', outcomeUnknown: false, agentId: 'd',
    error: 'Unknown Antigravity model: gemini-4-argon-high secret-canary', errorCode: 'INVALID_MODEL'};
  const recorded = await creationRecord({sendCorrelatedSessionRequest: async () => ({snapshot: refused})}, 'd');
  assert.equal(recorded.failure_code, 'INVALID_MODEL');
  const fake = {fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
    createAgent: async options => { options.onEvent(refused); throw new Error('secret-canary'); }};
  const created = await execute(fake, {operation: 'create', dispatch_id: 'd', parent_id: 'parent',
    request: {provider: 'antigravity-resilient/gemini-4-argon-high'}}, async () => ({status: 'watching'}));
  assert.equal(created.error, 'host_creation_failed');
  assert.equal(created.failure_code, 'INVALID_MODEL');
  assert(!JSON.stringify([recorded, created]).includes('canary'));
  // Something that is not an identifier is text, and stays private.
  const prose = await creationRecord({sendCorrelatedSessionRequest: async () => ({snapshot: {...refused, errorCode: 'model is gone'}})}, 'd');
  assert.equal('failure_code' in prose, false);
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
    {phase: 'completed', outcome_unknown: false, agent_id: null, agent_created: true});
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

test('a worker that failed at its prompt and was removed is reported as removed, not as never created', async () => {
  const f = watchFixture();
  const fetchAgent = f.client.fetchAgent;
  f.client.fetchAgent = async id => id === f.record.parent_id ? fetchAgent(id) : null;
  f.client.sendCorrelatedSessionRequest = async () => ({snapshot: {phase: 'failed', outcomeUnknown: true, agentId: 'd',
    agent: {id: 'd'}, failedStage: 'prompt', error: 'secret-canary'}});
  assert.equal((await creationRecord(f.client, 'd')).agent_created, true);
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.match(f.calls[0][1], /removed before this dispatch observed it/);
  assert(!/safe to roll/.test(f.calls[0][1]));
});

test('a worker that appears between the watcher reading it and reading its record is followed, not called removed', async () => {
  const f = watchFixture();
  const alive = {id: 'child', status: 'running'};
  const reads = [];
  const parent = f.client.fetchAgent;
  f.client.fetchAgent = async id => {
    if (id === 'parent') return parent(id);
    reads.push(id);
    return reads.length === 1 ? null : {agent: alive};
  };
  f.client.sendCorrelatedSessionRequest = async () => ({snapshot: {phase: 'failed', outcomeUnknown: true,
    agentId: 'child', agent: {id: 'child'}, error: 'prompt failed'}});
  f.record.creation_deadline = 10 ** 6;
  assert.equal(await notificationStep(f.client, f.record, f.persist, 500), false);
  assert.deepEqual(reads, ['child', 'child']);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.seen, true);
  assert.equal(f.record.started, true);
});

test('a restart before any agent existed still reads as never created', () => {
  const record = {dispatch_id: 'd', delivered: [], creation_deadline: 1000};
  const restarted = {phase: 'failed', outcome_unknown: true, agent_id: 'd', agent_created: false};
  assert.deepEqual(notificationEvents(null, record, 10, restarted), [{key: 'terminal', reason: 'creation failed'}]);
});

// Dispatch 49fe279b, 2026-11-07. Claimed 11:52:15.9, watcher up 11:52:17.7 with a fixed 2 minute limit, Paseo creating it
// from 11:52:25, terminal "creation outcome unavailable" at 11:54:17.7, agent created 11:54:34, running 11:54:42.
test('a create still running when an old watcher limit passes sends nothing, and the worker is then followed to its end', async () => {
  const up = Date.parse('2026-11-07T11:52:17.693Z');
  const at = text => Date.parse(`2026-11-07T${text}Z`);
  const f = watchFixture();
  // The record as the older CLI wrote it to disk: no created_at, and creation_deadline 2 minutes after the watcher started.
  delete f.record.created_at;
  Object.assign(f.record, {deadline: up + 7 * 24 * 3600 * 1000, creation_deadline: up + 120000});
  let phase = {phase: 'accepted', agentId: 'd'};
  f.client.sendCorrelatedSessionRequest = async () => ({snapshot: phase});
  const fetchAgent = f.client.fetchAgent;
  let worker = null;
  f.client.fetchAgent = async id => id === f.record.parent_id ? fetchAgent(id) : (worker ? {agent: worker} : null);
  for (const moment of ['11:53:00.000', '11:54:17.735', '11:54:30.000'])
    assert.equal(await notificationStep(f.client, f.record, f.persist, at(moment)), false, moment);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.terminal, undefined);
  worker = {id: 'd', status: 'running', labels: {'router.dispatch_id': f.record.dispatch_id}};
  phase = {phase: 'completed', agentId: 'd', agent: worker};
  assert.equal(await notificationStep(f.client, f.record, f.persist, at('11:54:44.000')), false);
  assert.equal(f.calls.length, 0);
  worker = {...worker, status: 'idle', attentionReason: 'finished'};
  assert.equal(await notificationStep(f.client, f.record, f.persist, at('12:30:00.000')), true);
  assert.match(f.calls[0][1], /State: finished/);
});

function resumeFixture() {
  const f = watchFixture();
  // creation_deadline is what the fixed 2 minute limit was read from.
  Object.assign(f.record, {dispatch_id: 'new-dispatch', agent_id: 'worker', created_at: 0, creation_deadline: 120000, deadline: 7 * 24 * 3600 * 1000});
  const parent = f.client.fetchAgent;
  f.target = async () => null;
  f.client.fetchAgent = async id => id === f.record.parent_id ? parent(id) : f.target(id);
  return f;
}

test('a resume target that answers late, past the old 2 minute limit, sends nothing until it is read', async () => {
  const f = resumeFixture();
  const step = now => notificationStep(f.client, f.record, f.persist, now);
  f.target = async () => {throw new Error('timeout');};
  for (const now of [60000, 120000, 121000, 200000]) await assert.rejects(step(now), /timeout/);
  // Readable, and still carrying the previous dispatch: the relabel has not landed yet.
  f.target = async id => ({agent: {id, status: 'idle', attentionReason: 'finished', labels: {'router.dispatch_id': 'old-dispatch'}}});
  for (const now of [240000, 600000]) assert.equal(await step(now), false, `unbound at ${now}`);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.terminal, undefined);
  f.target = async id => ({agent: {id, status: 'running', labels: {'router.dispatch_id': 'new-dispatch'}}});
  assert.equal(await step(610000), false);
  assert.equal(f.calls.length, 0);
  f.target = async id => ({agent: {id, status: 'idle', attentionReason: 'finished', labels: {'router.dispatch_id': 'new-dispatch'}}});
  assert.equal(await step(900000), true);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0][1], /State: finished/);
});

test('a resume target the host reports gone or archived ends the watch at once, whatever the clock says', async () => {
  const record = {dispatch_id: 'new-dispatch', agent_id: 'worker', creation_deadline: 120000};
  const unavailable = [{key: 'terminal', reason: 'worker unavailable'}];
  for (const now of [10, 120000, 500000]) {
    assert.deepEqual(notificationEvents(null, record, now), unavailable, `gone at ${now}`);
    assert.deepEqual(notificationEvents({id: 'worker', archivedAt: '2026-11-07T00:00:00Z', labels: {'router.dispatch_id': 'old-dispatch'}}, record, now), unavailable, `archived at ${now}`);
  }
  const f = resumeFixture();
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.match(f.calls[0][1], /State: worker unavailable/);
});

test('an unbound resume target that is merely closed is no evidence it is gone', () => {
  const record = {dispatch_id: 'new-dispatch', agent_id: 'worker', creation_deadline: 100};
  assert.deepEqual(notificationEvents({id: 'worker', status: 'closed', labels: {'router.dispatch_id': 'old-dispatch'}}, record, 500), []);
});

test('a worker this dispatch saw, now labeled for another dispatch, was superseded', async () => {
  const later = {status: 'idle', labels: {'router.dispatch_id': 'later-dispatch'}};
  const resume = resumeFixture();
  resume.record.seen = true;
  resume.target = async () => ({agent: later});
  assert.equal(await notificationStep(resume.client, resume.record, resume.persist, 300000), true);
  assert.equal(resume.record.status, 'delivered');
  assert.equal(resume.record.terminal.reason, 'superseded by a later dispatch');
  assert.match(resume.calls[0][1], /State: superseded by a later dispatch/);
  const created = {dispatch_id: 'child', creation_deadline: 100, seen: true};
  assert.deepEqual(notificationEvents(later, created, 500), [{key: 'terminal', reason: 'superseded by a later dispatch'}]);
});

test('labels of another dispatch end nothing before this dispatch saw its own, nor do missing labels after', () => {
  const bound = {id: 'worker', status: 'idle', labels: {'router.dispatch_id': 'new-dispatch'}};
  assert.deepEqual(notificationEvents({...bound, labels: {'router.dispatch_id': 'later-dispatch'}},
    {dispatch_id: 'new-dispatch', agent_id: 'worker', created_at: 0}, 500), []);
  assert.deepEqual(notificationEvents({id: 'child', status: 'running'}, {dispatch_id: 'child', creation_deadline: 100, seen: true}, 500), []);
});

test('a bound resume whose turn never lands gets one notice after the lease, keeps watching, and still reports a finish', async () => {
  const f = resumeFixture();
  const labels = {'router.dispatch_id': 'new-dispatch'};
  const before = {status: 'idle', attentionReason: 'finished', labels, lastUserMessageAt: new Date(-1000).toISOString()};
  f.target = async () => ({agent: before});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 599000), false);
  assert.equal(f.calls.length, 0);
  assert.equal(await notificationStep(f.client, f.record, f.persist, 600000), false);
  assert.equal(await notificationStep(f.client, f.record, f.persist, 700000), false);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0][1], /State: turn not delivered/);
  assert.match(f.calls[0][1], /release/);
  assert.equal(f.record.status, 'watching');
  assert.equal(f.record.terminal, undefined);
  f.target = async () => ({agent: {...before, lastUserMessageAt: new Date(650000).toISOString()}});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 800000), true);
  assert.match(f.calls[1][1], /State: finished/);
  assert.equal(f.record.terminal.reason, 'finished');
});

test('a resume that is running, or restoring within the lease, is not called undelivered', () => {
  const record = {dispatch_id: 'new-dispatch', agent_id: 'worker', created_at: 0};
  const labels = {'router.dispatch_id': 'new-dispatch'};
  assert.deepEqual(notificationEvents({status: 'running', labels}, {...record}, 900000), []);
  assert.deepEqual(notificationEvents({status: 'initializing', labels}, {...record}, 1000), []);
  assert.deepEqual(notificationEvents({status: 'initializing', labels}, {...record}, 600000),
    [{key: 'undelivered', reason: 'turn not delivered'}]);
});

test('a resume target that never becomes readable still gets its watch-limit notice', async () => {
  const f = resumeFixture();
  f.target = async () => {throw new Error('timeout');};
  await assert.rejects(notificationStep(f.client, f.record, f.persist, f.record.deadline - 1), /timeout/);
  assert.equal(await notificationStep(f.client, f.record, f.persist, f.record.deadline), true);
  assert.match(f.calls[0][1], /watch expired/);
});

test('a resume never lands in a workspace other than the one asked for', async () => {
  let notified = 0, updated = 0;
  const fake = {
    fetchAgent: async id => id === 'parent'
      ? {agent: {cwd: 'E:/fixture'}}
      : {agent: {id, provider: 'claude', model: 'x', status: 'idle', workspaceId: 'Y', labels: {'router.parent_id': 'parent'}}},
    updateAgent: async () => {updated++;}, sendMessage: async () => {},
  };
  const notify = async () => {notified++; return {status: 'watching'};};
  const input = {operation: 'resume', dispatch_id: 'resume-workspace', parent_id: 'parent', agent_id: 'old', request: {provider: 'claude/x', labels: {}, initialPrompt: 'p', title: 't'}};
  const elsewhere = await execute(fake, {...input, request: {...input.request, workspaceId: 'W'}}, notify);
  assert.deepEqual([elsewhere.ok, elsewhere.sent, elsewhere.error, notified, updated], [false, false, 'worker_workspace_mismatch', 0, 0]);
  assert.equal((await execute(fake, {...input, request: {...input.request, workspaceId: 'Y'}}, notify)).ok, true);
  assert.equal((await execute(fake, input, notify)).ok, true);
});

test('workspace_agents says where the workspace is, across pages, and an unreadable list costs only that', async () => {
  const pages = {
    undefined: {entries: [{id: 'other', projectRootPath: 'E:/other'}], pageInfo: {hasMore: true, nextCursor: 'next'}},
    next: {entries: [{id: 'W', projectRootPath: 'E:/repo', workspaceDirectory: 'E:/repo/tree'}], pageInfo: {hasMore: false, nextCursor: null}},
  };
  const seen = [];
  const fake = {fetchAgents: async () => ({entries: [], pageInfo: {hasMore: false}}),
    fetchWorkspaces: async options => {
      seen.push(options);
      return pages[options.page.cursor];
    }};
  const found = await execute(fake, {operation: 'workspace_agents', workspace_id: 'W'});
  assert.deepEqual([found.ok, found.cwd], [true, 'E:/repo/tree']);
  SessionInboundMessageSchema.parse({type: 'fetch_workspaces_request', requestId: 'fixture-request', ...seen[0]});
  const denied = await execute({...fake, fetchWorkspaces: async () => {throw new Error('boom');}}, {operation: 'workspace_agents', workspace_id: 'W'});
  assert.deepEqual([denied.ok, denied.agents, denied.cwd], [true, [], null]);
});

test('a workspace lookup that hangs costs only the directory, within seconds', async () => {
  const fake = {
    fetchAgents: async () => ({entries: [{agent: {id: 'a', status: 'running', workspaceId: 'W', labels: {'router.dispatch_id': 'd'}}}],
      pageInfo: {hasMore: false}}),
    fetchWorkspaces: () => new Promise(() => {}),
  };
  const started = Date.now();
  const found = await execute(fake, {operation: 'workspace_agents', workspace_id: 'W'});
  assert.deepEqual([found.ok, found.agents, found.cwd], [true, [{id: 'a', status: 'running', dispatch_id: 'd'}], null]);
  assert(Date.now() - started < 10000);
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

function fakeNotifierSdk(root) {
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
  return sdk;
}

test('a create that falls back from a refused resume gets its own watcher, following the new worker', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-notify-'));
  const oldEnv = {...process.env};
  const pids = new Set();
  const running = pid => {try {process.kill(pid, 0); return true;} catch {return false;}};
  const until = async predicate => {
    for (let i = 0; i < 200; i++) {
      if (predicate()) return;
      await pause(50);
    }
    assert.fail('notifier lifecycle deadline exceeded');
  };
  const file = path.join(root, 'paseo-notifications', 'fallback.json');
  const record = () => JSON.parse(fs.readFileSync(file, 'utf8'));
  try {
    process.env.ROUTER_PASEO_SDK_ROOT = fakeNotifierSdk(root);
    process.env.ROUTER_STATE_DIR = root;
    process.env.PASEO_HOME = root;
    fs.writeFileSync(path.join(root, 'agent.json'), JSON.stringify({status: 'idle', labels: {'router.dispatch_id': 'before'}}));
    const request = {provider: 'claude/m', title: 't', initialPrompt: 'p', labels: {'router.dispatch_id': 'fallback'}};
    const old = {id: 'old-worker', provider: 'claude', model: 'm', status: 'idle', labels: {'router.parent_id': 'parent'}};
    const refused = await execute({
      fetchAgent: async id => ({agent: id === 'parent' ? {cwd: '/fixture'} : old}),
      updateAgent: async () => {},
      sendMessage: async () => {throw new Error('fixture refusal');},
    }, {operation: 'resume', dispatch_id: 'fallback', parent_id: 'parent', agent_id: 'old-worker', request});
    assert.equal(refused.error, 'host_resume_failed');
    const firstPid = record().pid;
    pids.add(firstPid);
    assert.equal(record().agent_id, 'old-worker');

    const created = await execute({
      fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
      createAgent: async options => ({id: options.agentId, provider: 'claude', model: 'm', status: 'running', labels: options.labels}),
    }, {operation: 'create', dispatch_id: 'fallback', parent_id: 'parent', request,
      create_deadline: new Date(Date.now() + 60000).toISOString()});
    assert.equal(created.ok, true);
    assert.equal(created.completion_notification.status, 'watching');
    pids.add(created.completion_notification.pid);
    assert.notEqual(created.completion_notification.pid, firstPid);
    assert.equal(running(firstPid), false);
    assert.equal(running(created.completion_notification.pid), true);
    assert.equal(record().agent_id, undefined);
    assert.equal(fs.existsSync(`${file}.creation-failed`), false);

    fs.writeFileSync(path.join(root, 'agent.json'), JSON.stringify({status: 'idle', attentionReason: 'finished'}));
    await until(() => record().status === 'delivered');
    const [message] = fs.readFileSync(path.join(root, 'messages.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.match(message.text, /Worker: fallback\nState: finished/);
    await until(() => !running(created.completion_notification.pid));
  } finally {
    if (fs.existsSync(file)) pids.add(record().pid);
    for (const pid of pids) if (running(pid)) process.kill(pid);
    for (const name of ['ROUTER_STATE_DIR', 'ROUTER_PASEO_SDK_ROOT', 'PASEO_HOME']) {
      if (oldEnv[name] === undefined) delete process.env[name]; else process.env[name] = oldEnv[name];
    }
    await until(() => [...pids].every(pid => !running(pid)));
    fs.rmSync(root, {recursive: true, force: true});
  }
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
    process.env.ROUTER_STATE_DIR = root;
    process.env.ROUTER_PASEO_SDK_ROOT = fakeNotifierSdk(root);
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
      at: new Date(5_000).toISOString(), replied: false});   // no timeline read: not shown to have ended on a reply
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

test('a lapsed subscription gets its own class: structured fields decide, a model refusal does not lapse the account', () => {
  assert.equal(creationFailureClass({error: 'Your subscription has expired'}), 'subscription_inactive');
  assert.equal(creationFailureClass({error: 'whatever', errorCode: 'payment_required'}), 'subscription_inactive');
  assert.equal(creationFailureClass({error: 'whatever', statusCode: 402}), 'subscription_inactive');
  assert.notEqual(creationFailureClass({error: 'Your subscription has expired', errorCode: 'quota_exceeded'}), 'subscription_inactive');
  assert.notEqual(creationFailureClass({error: 'Your plan does not include this model'}), 'subscription_inactive');
  assert.equal(subscriptionLapsed(undefined, undefined, 'The model is not available on your plan'), false);
  const projected = project({id: 'x', lastError: 'Your subscription has expired'});
  assert.equal(projected.lastErrorClass, 'subscription_inactive');
  assert.equal(JSON.stringify(projected).includes('expired'), false);
  assert.equal(project({id: 'x', lastError: 'Your plan does not include this model'}).lastErrorClass, undefined);
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

// 2026-11-09: an opencode free-model worker sat `running` on "Provider retry (attempt 1): Free usage exceeded, subscribe
// to Go"; opencode never gives up, so the watcher must stop such a turn and say why, with the provider's own message.
const MINUTE = 60 * 1000;
const T0 = Date.parse('2026-11-09T08:00:00.000Z');
const retryEntry = (attempt, at, message = 'Free usage exceeded, subscribe to Go') =>
  ({provider: 'opencode', timestamp: new Date(at).toISOString(),
    item: {type: 'error', message: `Provider retry (attempt ${attempt}): ${message}`}});
const progress = at => ({provider: 'opencode', timestamp: new Date(at).toISOString(),
  item: {type: 'assistant_message', text: 'working'}});

test("provider retry notices are read from Paseo's own template and omp's structured status line", () => {
  assert.deepEqual(providerRetry(retryEntry(3, T0)),
    {attempt: 3, message: 'Free usage exceeded, subscribe to Go', next: null, at: T0});
  assert.deepEqual(providerRetry({timestamp: new Date(T0).toISOString(),
    item: {type: 'error', message: 'Provider retry (attempt 1)'}}), {attempt: 1, message: null, next: null, at: T0});
  assert.equal(providerRetry({timestamp: new Date(T0).toISOString(),
    item: {type: 'error', message: 'Free usage exceeded, subscribe to Go'}}), null);   // not a retry notice
  assert.equal(providerRetry(progress(T0)), null);
  assert.deepEqual(providerRetry({timestamp: new Date(T0).toISOString(), item: {type: 'tool_call', name: 'omp_auto_retry',
    detail: {type: 'plain_text', text: 'Retrying in 8s: 429 Too Many Requests'},
    metadata: {source: 'omp_auto_retry_start', attempt: 2, maxAttempts: 3, delayMs: 8000}}}),
  {attempt: 2, message: 'Retrying in 8s: 429 Too Many Requests', next: new Date(T0 + 8000).toISOString(), at: T0});
});

test('a retry streak is stuck only after the whole window without progress, and any progress resets it', () => {
  const one = retryStreak([progress(T0 - MINUTE), retryEntry(1, T0)]);
  assert.equal(one.count, 1);
  assert.equal(retryIsStuck(one, T0 + 4 * MINUTE), false);   // one long retry-after does not end the window early
  assert.equal(retryIsStuck(one, T0 + 5 * MINUTE), true);
  // Review F4: a minute of overload with fast retries (opencode backs off 2 s, 4 s, 8 s ...) is not stuck.
  const overload = retryStreak([0, 2, 6, 14, 30, 60].map((s, n) => retryEntry(n + 1, T0 + s * 1000)));
  assert.equal(retryIsStuck(overload, T0 + 61 * 1000), false);
  assert.equal(retryIsStuck(overload, T0 + 4 * MINUTE), false);
  assert.equal(retryIsStuck(overload, T0 + 5 * MINUTE), true);
  assert.equal(retryStreak([retryEntry(1, T0), retryEntry(2, T0 + MINUTE), progress(T0 + 2 * MINUTE)]), null);
  assert.equal(retryStreak([]), null);
});

function stuckFixture({entries, labels = {'router.dispatch_id': 'child'}, timelineError} = {}) {
  const f = watchFixture();
  f.record.deadline = T0 + 24 * 60 * MINUTE;
  const order = [];
  f.setAgent({id: 'child', status: 'running', labels});
  f.client.fetchAgentTimeline = async (id, options) => {
    order.push(['timeline', id, options.direction, options.projection]);
    if (timelineError) throw new Error('secret-prompt-canary');
    return {entries: entries ?? [progress(T0 - MINUTE), retryEntry(1, T0), retryEntry(2, T0 + 2 * MINUTE)]};
  };
  f.client.cancelAgent = async id => order.push(['cancel', id, f.saved.at(-1)?.terminal?.failure_code ?? null]);
  return {...f, order};
}

test('a running worker stuck in provider retry is stopped, its terminal saved first, and the parent told why', async () => {
  const f = stuckFixture();
  assert.equal(await notificationStep(f.client, f.record, f.persist, T0 + 2 * MINUTE), false);   // not past its bounds yet
  assert.equal(f.order.filter(([kind]) => kind === 'cancel').length, 0);
  assert.equal(await notificationStep(f.client, f.record, f.persist, T0 + 6 * MINUTE), true);
  // Saved before the cancel, so the stop's idle state can never read as a finish.
  assert.deepEqual(f.order.at(-1), ['cancel', 'child', RETRY_STUCK_CODE]);
  assert.deepEqual(f.order[0], ['timeline', 'child', 'tail', 'canonical']);
  assert.equal(f.record.terminal.failure_code, RETRY_STUCK_CODE);
  assert.equal(f.record.terminal.provider_message, 'Free usage exceeded, subscribe to Go');
  assert.equal(f.record.terminal.attempts, 2);
  assert.equal(f.record.terminal.retrying_since, new Date(T0).toISOString());
  assert.equal(f.record.terminal.next, null);
  assert.equal(f.record.terminal.cancelled, true);
  assert.equal(f.record.terminal.partial_work, true);   // it said "working" before it started retrying
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0][1], /State: provider retry stuck/);
  assert.match(f.calls[0][1], /Provider message: Free usage exceeded, subscribe to Go/);
  assert.match(f.calls[0][1], /already done part of the work \(it is stopped, not removed\)/);
  assert.doesNotMatch(f.calls[0][1], /before the worker did any work/);
  assert.equal(f.record.status, 'delivered');
  // After the cancel the host says idle: the outcome stays the stuck retry.
  f.setAgent({id: 'child', status: 'idle', attentionReason: 'finished', labels: {'router.dispatch_id': 'child'}});
  assert.deepEqual(notificationEvents({status: 'idle', attentionReason: 'finished',
    labels: {'router.dispatch_id': 'child'}}, f.record, T0 + 7 * MINUTE), [{key: 'terminal', reason: 'provider retry stuck'}]);
});

test('an unreadable timeline, progress after retrying, or a worker bound to another dispatch is left alone', async () => {
  for (const options of [{timelineError: true}, {entries: [retryEntry(1, T0), progress(T0 + MINUTE)]}]) {
    const f = stuckFixture(options);
    assert.equal(await notificationStep(f.client, f.record, f.persist, T0 + 60 * MINUTE), false);
    assert.equal(f.record.terminal, undefined);
    assert.equal(f.calls.length, 0);
  }
  const f = stuckFixture({labels: {'router.dispatch_id': 'previous'}});
  f.record.agent_id = 'worker';   // a resumed worker not relabelled for this dispatch yet
  assert.equal(await notificationStep(f.client, f.record, f.persist, T0 + 60 * MINUTE), false);
  assert.equal(f.order.length, 0);
});

test('a turn that retried from its prompt on is reported as having done no work', async () => {
  const prompt = {timestamp: new Date(T0 - MINUTE).toISOString(), item: {type: 'user_message', text: 'brief'}};
  const f = stuckFixture({entries: [progress(T0 - 2 * MINUTE), prompt, retryEntry(1, T0)]});
  assert.equal(await notificationStep(f.client, f.record, f.persist, T0 + 6 * MINUTE), true);
  assert.equal(f.record.terminal.partial_work, false);
  assert.match(f.calls[0][1], /before the worker did any work; roll this brief again/);
  const streak = retryStreak([retryEntry(1, T0)]);
  assert.equal(workedBeforeRetrying([retryEntry(1, T0)], streak, true), true);   // the prompt is beyond the page
  assert.equal(workedBeforeRetrying([retryEntry(1, T0)], streak, false), false);
});

test('a stop that fails is told to the parent, never reported as stopped', async () => {
  const f = stuckFixture();
  let tries = 0;
  f.client.cancelAgent = async () => {tries++; throw new Error('secret-prompt-canary');};
  assert.equal(await notificationStep(f.client, f.record, f.persist, T0 + 6 * MINUTE), true);
  assert.equal(tries, 2);   // retried once
  assert.equal(f.record.terminal.cancelled, false);
  assert.match(f.calls[0][1], /Stopping the worker failed, so it may still be running/);
  assert.match(f.calls[0][1], /cancel_agent \(agentId: child\)/);
  assert.doesNotMatch(f.calls[0][1], /secret-prompt-canary/);
});

test("a worker whose turn failed carries the provider's error text into its terminal", async () => {
  // opencode 2.2.6 (2026-11-09) fails a free model at once instead of retrying.
  const message = 'Free usage limit reached. Connect a provider or subscribe to OpenCode Go to keep going, or switch models.';
  const f = watchFixture();
  f.setAgent({id: 'child', status: 'error', attentionReason: 'error', lastError: message});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.record.terminal.failure_code, TURN_FAILED_CODE);
  assert.equal(f.record.terminal.provider_message, message);
  assert.match(f.calls[0][1], new RegExp(`The worker's turn ended with this error: ${message.replace(/[.]/g, '\\.')}`));
  // Review 2 R3: the bridge does not know yet whether this is the provider's doing, so it does not say so.
  assert.doesNotMatch(f.calls[0][1], /Provider error/);
  const plain = watchFixture();
  plain.setAgent({id: 'child', status: 'error'});
  await notificationStep(plain.client, plain.record, plain.persist, 10);
  assert.equal(plain.record.terminal.failure_code, undefined);   // no provider text, nothing to classify
});

test('a finished turn records whether it ended on the model reply; a cancel mid-retry or mid-tool does not', async () => {
  // Review 2 R1: Paseo's idle/finished reads the same after a cancel; only a turn ending on the reply may lift a hold.
  const prompt = {timestamp: new Date(T0).toISOString(), item: {type: 'user_message', text: 'brief'}};
  const tool = {timestamp: new Date(T0).toISOString(), item: {type: 'tool_call', name: 'shell'}};
  assert.equal(endedOnReply([prompt, tool, progress(T0 + 1000)]), true);
  assert.equal(endedOnReply([prompt, retryEntry(1, T0 + 1000)]), false);   // cancelled while retrying
  assert.equal(endedOnReply([prompt, progress(T0), retryEntry(1, T0 + 1000), progress(T0 + 2000)]), false);
  assert.equal(endedOnReply([prompt, progress(T0), tool]), false);           // cancelled in a tool call
  assert.equal(endedOnReply([prompt]), false);                                // cancelled before any reply
  assert.equal(endedOnReply([]), false);
  for (const [entries, replied] of [[[prompt, progress(T0 + 1000)], true], [[prompt, retryEntry(1, T0 + 1000)], false]]) {
    const f = watchFixture();
    f.client.fetchAgentTimeline = async () => ({entries});
    assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
    assert.equal(f.record.terminal.reason, 'finished');
    assert.equal(f.record.terminal.replied, replied);
  }
});

test("a resumed worker's error from its previous turn is not this dispatch's provider failure", async () => {
  // Review 2 R4: Paseo clears lastError when a turn starts; before this turn started it is the previous turn's.
  const message = 'Free usage limit reached.';
  const record = {dispatch_id: 'd2', agent_id: 'child', created_at: 5000};
  assert.deepEqual(turnFailure({lastError: message, lastUserMessageAt: new Date(4000).toISOString()}, record), {});
  assert.equal(turnFailure({lastError: message, lastUserMessageAt: new Date(6000).toISOString()}, record).failure_code,
    TURN_FAILED_CODE);
  assert.equal(turnFailure({lastError: message}, {...record, started: true}).failure_code, TURN_FAILED_CODE);
  assert.equal(turnFailure({lastError: message}, {dispatch_id: 'child', agent_id: 'child'}).failure_code, TURN_FAILED_CODE);
  const f = watchFixture();
  Object.assign(f.record, {dispatch_id: 'd2', agent_id: 'child', created_at: 5000, deadline: 60_000});
  f.setAgent({id: 'child', status: 'error', lastError: message, labels: {'router.dispatch_id': 'd2'},
    lastUserMessageAt: new Date(4000).toISOString()});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 6000), true);
  assert.equal(f.record.terminal.reason, 'error');
  assert.equal(f.record.terminal.failure_code, undefined);
  assert.doesNotMatch(f.calls[0][1], /ended with this error/);
});

test('provider_models reads live models per provider with structural ids and aliases, and reports disabled or unreadable providers', async () => {
  const fake = {
    getProvidersSnapshot: async () => ({
      providers: [
        {provider: 'antigravity-resilient', enabled: true, status: 'ready'},
        {provider: 'claude', enabled: true, status: 'ready'},
        {provider: 'codex-disabled', enabled: false, status: 'disabled'},
        {provider: 'opencode-broken', enabled: true, status: 'error'},
      ],
    }),
    listProviderModels: async provider => {
      if (provider === 'antigravity-resilient') {
        return {
          models: [
            {id: 'gemini-3.8-flash-high', label: 'Gemini 3.8 Flash (High)'},
            {id: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6 (Thinking)'},
          ],
        };
      }
      if (provider === 'claude') {
        return {
          models: [
            {id: 'claude-fable-5', aliases: ['claude-fable-5[1m]'], label: 'Fable 5'},
            {id: 'claude-opus-5-5', aliases: ['claude-opus-5-5[1m]'], label: 'Opus 5.5'},
          ],
        };
      }
      if (provider === 'opencode-broken') {
        return {models: [], error: 'secret-provider-error-canary'};
      }
      throw new Error('secret-throw-canary');
    },
  };
  const res = await execute(fake, {
    operation: 'provider_models',
    providers: ['antigravity-resilient', 'claude', 'codex-disabled', 'opencode-broken', 'missing-provider'],
  });
  assert.equal(res.ok, true);
  assert.deepEqual(res.providers['antigravity-resilient'], {
    ok: true,
    status: 'ready',
    models: [
      {id: 'gemini-3.8-flash-high', label: 'Gemini 3.8 Flash (High)'},
      {id: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6 (Thinking)'},
    ],
  });
  assert.deepEqual(res.providers.claude, {
    ok: true,
    status: 'ready',
    models: [
      {id: 'claude-fable-5', aliases: ['claude-fable-5[1m]'], label: 'Fable 5'},
      {id: 'claude-opus-5-5', aliases: ['claude-opus-5-5[1m]'], label: 'Opus 5.5'},
    ],
  });
  assert.deepEqual(res.providers['codex-disabled'], {
    ok: false,
    status: 'disabled',
    error: 'provider_disabled',
    models: [],
  });
  assert.deepEqual(res.providers['opencode-broken'], {
    ok: false,
    status: 'error',
    error: 'model_list_unavailable',
    models: [],
  });
  // A provider Paseo's snapshot does not list (e.g. a LiteLLM proxy account) is not Paseo's: no models call at all.
  assert.deepEqual(res.providers['missing-provider'], {
    ok: false,
    status: 'unknown_provider',
    error: 'unknown_provider',
    models: [],
  });
  assert(!JSON.stringify(res).includes('canary'));
  assert.deepEqual(normalizeProviderModels([{id: ' m1 ', aliases: [' a1 ', '', null], label: ' L1 '}, null, {}]),
    [{id: 'm1', aliases: ['a1'], label: 'L1'}]);
});

test('provider_models cannot tell a Paseo provider apart without the snapshot, so an unread snapshot reports unavailable', async () => {
  const asked = [];
  const fake = {
    getProvidersSnapshot: async () => { throw new Error('secret-snapshot-canary'); },
    listProviderModels: async provider => {
      asked.push(provider);
      if (provider === 'claude') return {models: [{id: 'claude-opus-5-5'}]};
      throw new Error('secret-throw-canary');
    },
  };
  const res = await execute(fake, {operation: 'provider_models', providers: ['claude', 'llmproxy']});
  assert.deepEqual(asked.sort(), ['claude', 'llmproxy']);
  assert.deepEqual(res.providers.claude, {ok: true, status: 'ready', models: [{id: 'claude-opus-5-5'}]});
  assert.deepEqual(res.providers.llmproxy,
    {ok: false, status: 'unavailable', error: 'model_list_unavailable', models: []});
  assert(!JSON.stringify(res).includes('canary'));
});
