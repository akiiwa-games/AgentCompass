import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import net from 'node:net';
import {randomUUID, createHash} from 'node:crypto';
import {setTimeout as pause} from 'node:timers/promises';
import {execute, project, normalizeProviderModels, notificationStep, notificationEvents, ensureNotification, notificationReceipt,
  creationFailureClass, creationFailureCode, creationRecord, resolvePaseoPlacement, subscriptionLapsed, providerRetry, retryStreak, retryIsStuck,
  workedBeforeRetrying, endedOnReply, turnFailure, RETRY_STUCK_CODE, TURN_FAILED_CODE, fallbackArgv, runFallback,
  attemptsNote, notificationFile, agentResumed, settleArgv, settleBeforeExit,
  finalReplyText, reportSection, composeNotice, notifyEnd, hostFailure, hostFailureClass, CONDITIONS, RECORDED_ENDS,
  BridgeError, createClient, continuationVerdict, REPORT_INLINE_LIMIT, REPORT_SUMMARY_LIMIT} from './paseo_dispatch_bridge.mjs';

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
  // Once bound and finished, the one notice names the worker and carries the reply (or says there was none).
  workerState = {...workerState, status: 'idle', attentionReason: 'finished'};
  const parentMsgs = [];
  const stepClient = {
    fetchAgent: fake.fetchAgent,
    sendMessage: async (...args) => parentMsgs.push(args),
  };
  assert.equal(await notificationStep(stepClient, record, () => {}, 20), true);
  assert.equal(parentMsgs.length, 1);
  assert.match(parentMsgs[0][1], new RegExp(`Worker: ${workerId}`));
  assert.match(parentMsgs[0][1], /The worker finished its turn without a final reply/);
  assert.doesNotMatch(parentMsgs[0][1], /get_agent_activity/);
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
  for (const [agent, ending] of [[{status: 'idle', attentionReason: 'finished'}, /finished its turn without a final reply/],
    [{status: 'error'}, /ended in an error and the provider gave no message/], [{status: 'closed'}, /Paseo closed this worker/],
    [{status: 'running', providerUnavailable: true}, /provider became unavailable/]]) {
    const f = watchFixture();
    f.setAgent({...agent, supportsNativePaseoTools: false});
    assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0][0], 'parent');
    assert.match(f.calls[0][1], /Continuation: (same worker|roll|rotate|unknown)/);
    assert.match(f.calls[0][1], /redo or correction/);
    assert.match(f.calls[0][1], ending);
    assert.doesNotMatch(f.calls[0][1], /get_agent_activity/);
    assert.equal(f.calls[0][2].activeTurnBehavior, 'steer');
    assert.equal(f.saved[0].pending.messageId, f.calls[0][2].messageId);
    await notificationStep(f.client, f.record, f.persist, 20);
    assert.equal(f.calls.length, 1);
  }
});

test('permissions notify once per request and do not consume the final callback', async () => {
  const f = watchFixture();
  f.client.fetchAgentTimeline = async () => ({entries: [{item: {type: 'assistant_message', text: 'SECRET-REPORT'}}]});
  f.setAgent({status: 'idle', pendingPermissions: [{id: 'approval-1'}, {id: 'approval-2'}]});
  for (let i = 0; i < 4; i++) assert.equal(await notificationStep(f.client, f.record, f.persist, 10), false);
  assert.equal(f.calls.length, 2);
  assert.match(f.calls[0][1], /needs permission/);
  assert.match(f.calls[0][1], /A permission is waiting/);
  assert.doesNotMatch(f.calls[0][1], /SECRET-REPORT/);
  assert.doesNotMatch(f.calls[0][1], /Final reply:/);
  assert.match(f.calls[0][1], /Continuation: (same worker|roll|rotate|unknown)/);
  f.setAgent({status: 'idle', attentionReason: 'finished'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 20), true);
  assert.equal(f.calls.length, 3);
  assert.match(f.calls[2][1], /Final reply:\nSECRET-REPORT/);
});

test('a finished worker sends one notice containing the final reply after its last prompt', async () => {
  const f = watchFixture();
  f.client.fetchAgentTimeline = async () => ({entries: [
    {item: {type: 'user_message', text: 'older prompt'}},
    {item: {type: 'assistant_message', text: 'older reply'}},
    {item: {type: 'user_message', text: 'the brief'}},
    {item: {type: 'assistant_message', text: '<the report>'}},
  ]});
  assert.equal(finalReplyText([
    {item: {type: 'assistant_message', text: 'older reply'}},
    {item: {type: 'user_message', text: 'the brief'}},
    {item: {type: 'assistant_message', text: '<the report>'}},
  ]), '<the report>');
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0][1], /^\[AgentCompass worker notification\]/);
  assert.match(f.calls[0][1], /Final reply:\n<the report>/);
  assert.doesNotMatch(f.calls[0][1], /older reply/);
  assert.match(f.calls[0][1], /Continuation: (same worker|roll|rotate|unknown)/);
  assert.match(f.calls[0][1], /redo or correction/);
  assert.doesNotMatch(f.calls[0][1], /get_agent_activity/);
  await notificationStep(f.client, f.record, f.persist, 20);
  assert.equal(f.calls.length, 1);
});

test('a parent reply while the watcher is running is the one end message', async () => {
  const f = watchFixture();
  f.client.fetchAgentTimeline = async () => ({entries: [{item: {type: 'assistant_message', text: 'should not be sent'}}]});
  f.setAgent({status: 'running', lastUserMessageAt: '2026-11-12T00:00:00.000Z'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), false);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.user_message_at, '2026-11-12T00:00:00.000Z');
  f.setAgent({status: 'idle', attentionReason: 'finished', lastUserMessageAt: '2026-11-12T00:00:01.000Z'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 20), true);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.status, 'continued');
});

test('a delivery failure is not delivery evidence and keeps the pending notice', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-undelivered-'));
  const old = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = root;
  try {
    const file = notificationFile('case-undelivered');
    fs.mkdirSync(path.dirname(file), {recursive: true});
    fs.writeFileSync(file, JSON.stringify({
      dispatch_id: 'case-undelivered', parent_id: 'parent', status: 'delivery_failed', delivered: [],
      pending: {key: 'terminal', text: 'final answer'},
    }));
    const calls = [];
    const client = {
      fetchAgent: async () => ({agent: {status: 'idle'}}),
      sendMessage: async () => { throw new Error('still down'); },
    };
    await assert.rejects(notifyEnd(client, {dispatch_id: 'case-undelivered', parent_id: 'parent'}));
    const kept = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(kept.status, 'delivery_failed');
    assert.equal(kept.pending.text, 'final answer');
    assert.equal(typeof kept.pending.messageId, 'string');
    assert.equal(kept.pending.messageId.length > 0, true);
    assert.deepEqual(kept.delivered ?? [], []);
    client.sendMessage = async (...args) => calls.push(args);
    const sent = await notifyEnd(client, {dispatch_id: 'case-undelivered', parent_id: 'parent'});
    assert.equal(sent.sent, true);
    assert.notEqual(sent.already, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][1], 'final answer');
    assert.equal(calls[0][2].messageId, kept.pending.messageId);
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).status, 'delivered');
  } finally {
    if (old === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = old;
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('a timeline read failure does not send an empty report', async () => {
  const f = watchFixture();
  f.client.fetchAgentTimeline = async () => { throw new Error('temporary unavailable'); };
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), false);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.pending, null);
  assert.notEqual(f.record.status, 'delivered');
  const body = await composeNotice(f.client, f.record, {key: 'terminal', reason: 'finished'});
  assert.equal(body, null);
});

test('a continuation does not send an already pending terminal notice', async () => {
  const f = watchFixture();
  f.record.continued = true;
  f.record.pending = {key: 'terminal', messageId: 'old-id', text: 'old final'};
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.calls.length, 0);
  assert.equal(f.record.status, 'continued');
  assert.equal(f.record.pending, null);
});

test('a long final reply is stored beside the receipt and the notice carries the path and a summary', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-notice-'));
  const old = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = root;
  try {
    const text = 'R'.repeat(REPORT_INLINE_LIMIT + 50);
    const section = reportSection('child', text);
    const file = path.join(root, 'paseo-notifications', 'child.report.txt');
    assert.equal(fs.readFileSync(file, 'utf8'), text);
    assert.match(section, /Full text: /);
    assert.ok(section.includes(file));
    assert.ok(section.includes('R'.repeat(REPORT_SUMMARY_LIMIT)));
    assert.equal(section.includes(text), false);
    const f = watchFixture();
    f.client.fetchAgentTimeline = async () => ({entries: [{item: {type: 'assistant_message', text}}]});
    assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
    assert.equal(f.calls.length, 1);
    assert.ok(f.calls[0][1].includes(file));
    assert.match(f.calls[0][1], /Summary:/);
    assert.equal(f.calls[0][1].includes(text), false);
    const composed = await composeNotice(f.client, f.record, {key: 'terminal', reason: 'finished'});
    assert.match(composed, /\[AgentCompass worker notification\]/);
  } finally {
    if (old === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = old;
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('notify_end sends one catch-up notice and a second call sends nothing', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-notify-'));
  const old = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = root;
  try {
    const file = notificationFile('child');
    fs.mkdirSync(path.dirname(file), {recursive: true});
    const calls = [];
    const client = {
      fetchAgent: async id => ({agent: {id, status: 'running'}}),
      fetchAgentTimeline: async () => { throw new Error('pending text is the notice'); },
      sendMessage: async (...args) => calls.push(args),
    };
    fs.writeFileSync(file, JSON.stringify({
      dispatch_id: 'child', parent_id: 'parent', status: 'watching', delivered: [],
      pending: {key: 'terminal', messageId: 'ignored', text: 'PENDING-REPLY'},
      terminal: {reason: 'finished'},
    }));
    const first = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'});
    assert.equal(first.ok, true);
    assert.equal(first.sent, true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].slice(0, 2), ['parent', 'PENDING-REPLY']);
    const again = await execute(client, {operation: 'notify_end', dispatch_id: 'child', parent_id: 'parent'});
    assert.equal(again.already, true);
    assert.equal(again.sent, false);
    assert.equal(calls.length, 1);
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.status, 'delivered');
    assert.equal(saved.pending, null);
    assert.ok(saved.delivered.includes('terminal'));
    const missing = await notifyEnd(client, {dispatch_id: 'no-such-dispatch', parent_id: 'parent'});
    assert.equal(missing.ok, false);
    assert.equal(missing.error, 'notification_unavailable');
  } finally {
    if (old === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = old;
    fs.rmSync(root, {recursive: true, force: true});
  }
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

test('a worker this dispatch saw, now labeled for another dispatch, was superseded, and the watcher leaves it to the registry', async () => {
  const later = {status: 'idle', labels: {'router.dispatch_id': 'later-dispatch'}};
  const resume = resumeFixture();
  resume.record.seen = true;
  resume.target = async () => ({agent: later});
  assert.equal(await notificationStep(resume.client, resume.record, resume.persist, 300000), true);
  assert.equal(resume.calls.length, 0);
  assert.equal(resume.record.status, 'continued');
  assert.equal(resume.record.continued_by, 'later-dispatch');
  assert.equal(resume.record.terminal.reason, 'superseded by a later dispatch');
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

test('a resume never lands in a directory other than the one asked for', async () => {
  let notified = 0, updated = 0;
  const fake = {
    fetchAgent: async id => id === 'parent'
      ? {agent: {cwd: 'E:/fixture'}}
      : {agent: {id, provider: 'claude', model: 'x', status: 'idle', cwd: 'E:\\repo\\Y', labels: {'router.parent_id': 'parent'}}},
    updateAgent: async () => {updated++;}, sendMessage: async () => {},
  };
  const notify = async () => {notified++; return {status: 'watching'};};
  const input = {operation: 'resume', dispatch_id: 'resume-workspace', parent_id: 'parent', agent_id: 'old', request: {provider: 'claude/x', labels: {}, initialPrompt: 'p', title: 't'}};
  const elsewhere = await execute(fake, {...input, request: {...input.request, cwd: 'E:/repo/W'}}, notify);
  assert.deepEqual([elsewhere.ok, elsewhere.sent, elsewhere.error, notified, updated], [false, false, 'worker_workspace_mismatch', 0, 0]);
  // The refusal names both directories, so a caller that named the worker learns exactly why.
  assert.deepEqual(elsewhere.detail, {worker: 'E:\\repo\\Y', requested: 'E:/repo/W'});
  const otherModel = await execute(fake, {...input, request: {...input.request, provider: 'claude/z'}}, notify);
  assert.deepEqual([otherModel.error, otherModel.detail], ['worker_model_mismatch', {worker: 'claude/x', requested: 'claude/z'}]);
  assert.equal((await execute(fake, {...input, request: {...input.request, cwd: 'e:/repo/y/'}}, notify)).ok, true);
  assert.equal((await execute(fake, input, notify)).ok, true);
});

test('workspace_agents probes directory placeability across pages and reports unreadable workspace lists without leaking host IDs', async () => {
  const pages = {
    undefined: {entries: [{id: 'wks_other', projectRootPath: 'E:/other'}], pageInfo: {hasMore: true, nextCursor: 'next'}},
    next: {entries: [{id: 'wks_tree', projectRootPath: 'E:/repo', workspaceDirectory: 'E:\\repo\\tree'}], pageInfo: {hasMore: false, nextCursor: null}},
  };
  const seen = [];
  const fake = {
    fetchAgent: async () => ({agent: {cwd: 'E:/repo', workspaceId: 'wks_root'}}),
    fetchAgents: async () => ({entries: [], pageInfo: {hasMore: false}}),
    fetchWorkspaces: async options => {
      seen.push(options);
      return pages[options.page.cursor];
    },
  };
  const found = await execute(fake, {operation: 'workspace_agents', cwd: 'e:/repo/tree/', parent_id: 'parent'});
  assert.deepEqual([found.ok, found.placeable, found.cwd], [true, true, 'e:/repo/tree/']);
  assert(!JSON.stringify(found).includes('wks_'));
  SessionInboundMessageSchema.parse({type: 'fetch_workspaces_request', requestId: 'fixture-request', ...seen[0]});
  const denied = await execute({...fake, fetchWorkspaces: async () => {throw new Error('boom');}}, {operation: 'workspace_agents', cwd: 'E:/repo/tree', parent_id: 'parent'});
  assert.deepEqual([denied.ok, denied.agents, denied.placeable, denied.error], [true, [], false, 'paseo_workspaces_unreadable']);
});

test('a workspace lookup that hangs still returns running directory agents within seconds', async () => {
  const fake = {
    fetchAgents: async () => ({entries: [{agent: {id: 'a', status: 'running', cwd: 'E:/repo/tree', labels: {'router.dispatch_id': 'd'}}}],
      pageInfo: {hasMore: false}}),
    fetchWorkspaces: () => new Promise(() => {}),
  };
  const started = Date.now();
  const found = await execute(fake, {operation: 'workspace_agents', cwd: 'e:\\repo\\tree\\'});
  assert.deepEqual([found.ok, found.agents, found.cwd], [true, [{id: 'a', status: 'running', dispatch_id: 'd'}], 'e:\\repo\\tree\\']);
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

test('workspace_agents lists only router agents in that canonical directory across pages', async () => {
  const filters = [];
  const fake = {
    fetchWorkspaces: async () => ({entries: [{id: 'wks_w', workspaceDirectory: 'E:/repo/W'}], pageInfo: {hasMore: false, nextCursor: null}}),
    fetchAgents: async ({filter, page}) => {
      filters.push(filter);
      return page.cursor
        ? {entries: [{agent: {id: 'c', status: 'initializing', workspaceId: 'wks_w', labels: {'router.dispatch_id': 'dc'}}}], pageInfo: {hasMore: false, nextCursor: null}}
        : {entries: [{agent: {id: 'a', status: 'running', cwd: 'e:\\repo\\w\\', labels: {'router.dispatch_id': 'da'}}},
            {agent: {id: 'b', status: 'running', cwd: 'E:/repo/other', labels: {'router.dispatch_id': 'db'}}},
            {agent: {id: 'h', status: 'running', cwd: 'E:/repo/W', labels: {}}}], pageInfo: {hasMore: true, nextCursor: 'next'}};
    },
  };
  const result = await execute(fake, {operation: 'workspace_agents', cwd: 'E:/repo/W'});
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
    // The reclaimed lock holds the new owner's pid once, so the next observer reads a live owner.
    assert.equal(fs.readFileSync(recordFile + '.lock', 'utf8'), String(recovered.pid));
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

// Owner requirement 2026-11-10: a failed attempt goes to AgentCompass's fallback step first; the parent hears only the
// final outcome, once, with the attempts listed.
test('a failed attempt that AgentCompass relaunched tells the parent nothing and ends this watcher', async () => {
  for (const agent of [{status: 'error'}, {status: 'running', providerUnavailable: true}]) {
    const f = watchFixture();
    f.setAgent(agent);
    const asked = [];
    const done = await notificationStep(f.client, f.record, f.persist, 10, async record => {
      asked.push(structuredClone(record));
      return {action: 'relaunched', dispatch_id: 'child-2', root_dispatch_id: 'child', lines: ['attempt 1: a', 'attempt 2: b']};
    });
    assert.equal(done, true);
    assert.equal(f.calls.length, 0);
    assert.equal(f.record.status, 'relaunched');
    // The terminal was saved before the step ran, so the registry's observation reads it.
    assert.equal(asked[0].terminal.reason, 'error');
    assert.equal(f.saved.find(s => s.fallback?.status === 'running').terminal.reason, 'error');
    assert.equal(await notificationStep(f.client, f.record, f.persist, 20, async () => assert.fail('ran twice')), true);
    assert.equal(f.calls.length, 0);
  }
});

test('a dispatch with no candidate left sends one notification listing every attempt', async () => {
  const f = watchFixture();
  f.setAgent({status: 'error'});
  const text = 'AgentCompass retried or relaunched this brief on every startable candidate and none is left:\n' +
    'attempt 1: m1 on a1 failed: worker_error; switched to m2 on a2\nattempt 2: m2 on a2 failed: worker_error';
  const outcome = {action: 'final', result: 'blocked', root_dispatch_id: 'root', text,
    lines: ['attempt 1: m1 on a1 failed: worker_error; switched to m2 on a2', 'attempt 2: m2 on a2 failed: worker_error']};
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10, async () => outcome), true);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0][1], /^\[AgentCompass worker notification\]\nDispatch: root\nAttempt dispatch: child\n/);
  assert.match(f.calls[0][1], /State: blocked\n/);
  assert.match(f.calls[0][1], /switched to m2 on a2/);
  assert(!/roll this brief again/.test(f.calls[0][1]));
  await notificationStep(f.client, f.record, f.persist, 20, async () => assert.fail('ran twice'));
  assert.equal(f.calls.length, 1);
});

test('a failure the fallback step declines or cannot read is reported as before', async () => {
  for (const outcome of [null, {action: 'disabled'}, {action: 'final', result: 'declined', lines: ['attempt 1: x']}]) {
    const f = watchFixture();
    f.setAgent({status: 'error'});
    assert.equal(await notificationStep(f.client, f.record, f.persist, 10, async () => outcome), true);
    assert.equal(f.calls.length, 1);
    assert.match(f.calls[0][1], /State: error\n/);
    assert.match(f.calls[0][1], /ended in an error and the provider gave no message; it left no final reply/);
    assert.doesNotMatch(f.calls[0][1], /get_agent_activity/);
  }
  // A finish never goes to the fallback step.
  const f = watchFixture();
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10, async () => assert.fail('finish')), true);
  assert.equal(f.calls.length, 1);
});

test('another observer relaunching the attempt makes this watcher look again later', async () => {
  const f = watchFixture();
  f.setAgent({status: 'error'});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10, async () => ({action: 'pending'})), false);
  assert.equal(f.calls.length, 0);
  assert.equal(await notificationStep(f.client, f.record, f.persist, 20,
    async () => ({action: 'relaunched', dispatch_id: 'child-2'})), true);
  assert.equal(f.calls.length, 0);
});

test('a scheduled same-model retry runs the fallback step again only once it is due, and tells the parent nothing', async () => {
  const f = watchFixture();
  f.setAgent({status: 'error'});
  const retryAt = new Date(100000).toISOString();
  f.record.deadline = 50;   // a retry past the watch keeps the watcher until the retry has been seen
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10,
    async () => ({action: 'scheduled', retry_at: retryAt, root_dispatch_id: 'child'})), false);
  assert.deepEqual([f.record.fallback.status, f.record.fallback.retry_at], ['scheduled', retryAt]);
  assert(f.record.deadline >= 100000 + 30 * 60 * 1000);
  assert.equal(f.saved.at(-1).fallback.status, 'scheduled');   // persisted: a restarted watcher waits too
  assert.equal(await notificationStep(f.client, f.record, f.persist, 99999, async () => assert.fail('ran early')), false);
  const asked = [];
  assert.equal(await notificationStep(f.client, f.record, f.persist, 100001, async record => {
    asked.push(record.dispatch_id);
    return {action: 'relaunched', dispatch_id: 'child-2', root_dispatch_id: 'child'};
  }), true);
  assert.deepEqual(asked, ['child']);
  assert.equal(f.record.status, 'relaunched');
  assert.equal(f.calls.length, 0);
});

test('a scheduled retry the parent canceled ends this watcher silently; one retired past its wait is reported once', async () => {
  const retryAt = new Date(100000).toISOString();
  const canceled = watchFixture();
  canceled.setAgent({status: 'error'});
  await notificationStep(canceled.client, canceled.record, canceled.persist, 10, async () => ({action: 'scheduled', retry_at: retryAt}));
  assert.equal(await notificationStep(canceled.client, canceled.record, canceled.persist, 100001,
    async () => ({action: 'final', result: 'canceled', root_dispatch_id: 'child', text: 'canceled'})), true);
  assert.equal(canceled.record.status, 'canceled');
  assert.equal(canceled.calls.length, 0);
  const expired = watchFixture();
  expired.setAgent({status: 'error'});
  await notificationStep(expired.client, expired.record, expired.persist, 10, async () => ({action: 'scheduled', retry_at: retryAt}));
  const text = 'AgentCompass scheduled a retry of this brief on the same model after a transient provider failure, but ' +
    'nothing started it before the wait the task allows (can_wait_hours) ran out, so the retry is retired.';
  assert.equal(await notificationStep(expired.client, expired.record, expired.persist, 100001,
    async () => ({action: 'final', result: 'retry_expired', root_dispatch_id: 'child', text})), true);
  assert.equal(expired.calls.length, 1);
  assert.match(expired.calls[0][1], /State: retry_expired\n/);
  assert(expired.calls[0][1].includes('can_wait_hours'));
});

test('a worker the parent resumed ends a scheduled retry or error step silently with no switched-to notice', async () => {
  const retryAt = new Date(100000).toISOString();
  const scheduled = watchFixture();
  scheduled.setAgent({status: 'error'});
  await notificationStep(scheduled.client, scheduled.record, scheduled.persist, 10,
    async () => ({action: 'scheduled', retry_at: retryAt}));
  assert.equal(agentResumed({status: 'error'}, scheduled.record), false);
  assert.equal(agentResumed({status: 'running'}, {terminal: {cancelled: false}}), false);
  scheduled.setAgent({status: 'running'});
  assert.equal(agentResumed({status: 'running'}, scheduled.record), true);
  // Before retry_at, seeing the worker running again wakes the fallback step to retire the retry and stays silent.
  let asked = 0;
  assert.equal(await notificationStep(scheduled.client, scheduled.record, scheduled.persist, 500, async () => {
    asked++;
    return {action: 'final', result: 'resumed', root_dispatch_id: 'child', text: 'resumed'};
  }), true);
  assert.equal(asked, 1);
  assert.equal(scheduled.record.status, 'resumed');
  assert.equal(scheduled.calls.length, 0);
  // Direct `resumed` on an error sighting sends neither an error nor a switched-to notice.
  const direct = watchFixture();
  direct.setAgent({status: 'error'});
  assert.equal(await notificationStep(direct.client, direct.record, direct.persist, 10,
    async () => ({action: 'final', result: 'resumed', root_dispatch_id: 'child', text: 'resumed'})), true);
  assert.equal(direct.record.status, 'resumed');
  assert.equal(direct.calls.length, 0);
});

test('the relaunched attempt reports its finish under the parent dispatch with the attempts', async () => {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-attempts-'));
  const previous = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = state;
  try {
    const file = notificationFile('child');
    fs.mkdirSync(path.dirname(file), {recursive: true});
    fs.writeFileSync(`${file}.attempts`, JSON.stringify({root_dispatch_id: 'root',
      lines: ['attempt 1: m1 on a1 failed: create_failed:unknown:INVALID_MODEL; switched to m2 on a2', 'attempt 2: m2 on a2 (running)']}));
    assert.equal(attemptsNote(file).root_dispatch_id, 'root');
    const f = watchFixture();
    assert.equal(await notificationStep(f.client, f.record, f.persist, 10, null), true);
    assert.match(f.calls[0][1], /Dispatch: root\nAttempt dispatch: child\nWorker: child\nState: finished\nContinuation: [^\n]+\n[^\n]+\nAttempts:\n- attempt 1: m1 on a1 failed: create_failed:unknown:INVALID_MODEL; switched to m2 on a2\n/);
  } finally {
    if (previous === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = previous;
    fs.rmSync(state, {recursive: true, force: true});
  }
});

test('the fallback command comes only from AGENTCOMPASS_FALLBACK_ARGV and its JSON answer is read', async () => {
  assert.equal(fallbackArgv({}), null);
  assert.equal(fallbackArgv({AGENTCOMPASS_FALLBACK_ARGV: 'not json'}), null);
  assert.equal(fallbackArgv({AGENTCOMPASS_FALLBACK_ARGV: '[]'}), null);
  assert.deepEqual(fallbackArgv({AGENTCOMPASS_FALLBACK_ARGV: '["agentcompass","dispatch"]'}), ['agentcompass', 'dispatch']);
  assert.equal(await runFallback('d1', null), null);
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-fallback-'));
  try {
    const answer = path.join(folder, 'answer.mjs'), silent = path.join(folder, 'silent.mjs');
    fs.writeFileSync(answer, 'const i = process.argv.indexOf("--dispatch-id"); ' +
      'console.log(JSON.stringify({action: "relaunched", dispatch_id: process.argv[i + 1] + "-2"}, null, 2));');
    fs.writeFileSync(silent, 'console.log("no json");');
    assert.deepEqual(await runFallback('d1', [process.execPath, answer]), {action: 'relaunched', dispatch_id: 'd1-2'});
    assert.equal(await runFallback('d1', [process.execPath, silent]), null);
  } finally { fs.rmSync(folder, {recursive: true, force: true}); }
  assert.equal(await runFallback('d1', [path.join(os.tmpdir(), 'no-such-agentcompass-binary')]), null);
});

test('the watcher settles the registry from AGENTCOMPASS_SETTLE_ARGV before it unlocks', async () => {
  assert.equal(settleArgv({}), null);
  assert.equal(settleArgv({AGENTCOMPASS_SETTLE_ARGV: 'not json'}), null);
  assert.equal(settleArgv({AGENTCOMPASS_SETTLE_ARGV: '[]'}), null);
  assert.deepEqual(settleArgv({AGENTCOMPASS_SETTLE_ARGV: '["agentcompass","dispatch"]'}), ['agentcompass', 'dispatch']);
  assert.equal(await settleBeforeExit(null), null);
  assert.equal(await settleBeforeExit({}), null);
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-settle-'));
  try {
    const answer = path.join(folder, 'answer.mjs');
    fs.writeFileSync(answer, 'const i = process.argv.indexOf("--dispatch-id"); ' +
      'console.log(JSON.stringify({state: "completed", dispatch_id: process.argv[i + 1]}));');
    assert.deepEqual(await settleBeforeExit({dispatch_id: 'd5706c25'}, [process.execPath, answer]),
      {state: 'completed', dispatch_id: 'd5706c25'});
  } finally { fs.rmSync(folder, {recursive: true, force: true}); }
  const source = fs.readFileSync(fileURLToPath(new URL('./paseo_dispatch_bridge.mjs', import.meta.url)), 'utf8');
  const body = source.slice(source.indexOf('async function watch('), source.indexOf('export function canonicalPath'));
  const exit = body.slice(body.lastIndexOf('finally {'));
  assert.match(exit, /await settleBeforeExit\(record\)/);
  assert.ok(exit.indexOf('await settleBeforeExit(record)') < exit.indexOf('unlinkSync(lock)'));
});

test('Paseo placement sends workspaceId only when verified for that exact cwd and refuses ancestor-only directories before create', async () => {
  const seenOptions = [];
  const id = '22334455-6677-4888-9999-aabbccddeeff';
  const baseRequest = {provider: 'claude/claude-sonnet-4-6', title: 'fixture', initialPrompt: 'p', labels: {'router.dispatch_id': id}};

  // 1. Exact workspace match (across slash/case/trailing-separator differences) -> sends matching workspaceId + cwd.
  const exactClient = {
    fetchAgent: async () => ({agent: {cwd: 'D:\\work\\games', workspaceId: 'wks_parent'}}),
    fetchWorkspaces: async () => ({
      entries: [
        {id: 'wks_parent', workspaceDirectory: 'D:\\work\\games'},
        {id: 'wks_exact', workspaceDirectory: 'D:\\work\\games\\space_trader'},
      ],
      pageInfo: {hasMore: false, nextCursor: null},
    }),
    createAgent: async options => {seenOptions.push(options); return {id, provider: 'claude', model: 'claude-sonnet-4-6', status: 'running', labels: options.labels};},
  };
  const exact = await execute(exactClient, {operation: 'create', dispatch_id: id, parent_id: 'parent',
    request: {...baseRequest, cwd: 'd:/work/games/space_trader/'}}, notifier);
  assert.equal(exact.ok, true);
  assert.equal(seenOptions.length, 1);
  assert.equal(seenOptions[0].workspaceId, 'wks_exact');
  assert.equal(seenOptions[0].cwd, 'd:/work/games/space_trader/');

  // 2. Only enclosing workspace exists -> refuses before createAgent; never sends ancestor workspaceId.
  let ancestorCreates = 0;
  const ancestorOnlyClient = {
    fetchAgent: async () => ({agent: {cwd: 'D:\\work\\games', workspaceId: 'wks_parent'}}),
    fetchWorkspaces: async () => ({
      entries: [{id: 'wks_parent', workspaceDirectory: 'D:\\work\\games'}],
      pageInfo: {hasMore: false, nextCursor: null},
    }),
    createAgent: async () => {ancestorCreates++; return {};},
  };
  const refused = await execute(ancestorOnlyClient, {operation: 'create', dispatch_id: id, parent_id: 'parent',
    request: {...baseRequest, cwd: 'D:/work/games/space_trader'}}, notifier);
  assert.deepEqual([refused.ok, refused.sent, refused.error, ancestorCreates],
    [false, false, 'paseo_workspace_not_found_for_directory', 0]);
  assert.deepEqual(refused.enclosing_directories, ['D:\\work\\games']);
  assert(!JSON.stringify(refused).includes('wks_'));

  // 3. Parent has no workspaceId and parent.cwd === target cwd -> omits workspaceId and sends cwd.
  const noWksOptions = [];
  const cwdOnlyClient = {
    fetchAgent: async () => ({agent: {cwd: 'D:\\work\\standalone'}}),
    fetchWorkspaces: async () => ({entries: [], pageInfo: {hasMore: false, nextCursor: null}}),
    createAgent: async options => {noWksOptions.push(options); return {id, provider: 'claude', model: 'claude-sonnet-4-6', status: 'running', labels: options.labels};},
  };
  const cwdOnly = await execute(cwdOnlyClient, {operation: 'create', dispatch_id: id, parent_id: 'parent',
    request: {...baseRequest, cwd: 'd:/work/standalone'}}, notifier);
  assert.equal(cwdOnly.ok, true);
  assert.equal(noWksOptions.length, 1);
  assert.equal('workspaceId' in noWksOptions[0], false);
  assert.equal(noWksOptions[0].cwd, 'd:/work/standalone');

  // 4. Unreadable workspace list refuses before createAgent instead of failing open.
  let unreadableCreates = 0;
  const unreadableClient = {
    fetchAgent: async () => ({agent: {cwd: 'E:/other', workspaceId: 'wks_other'}}),
    fetchWorkspaces: async () => {throw new Error('socket closed');},
    createAgent: async () => {unreadableCreates++; return {};},
  };
  const unreadable = await execute(unreadableClient, {operation: 'create', dispatch_id: id, parent_id: 'parent',
    request: {...baseRequest, cwd: 'E:/target'}}, notifier);
  assert.deepEqual([unreadable.ok, unreadable.sent, unreadable.error, unreadableCreates],
    [false, false, 'paseo_workspaces_unreadable', 0]);
});

test('creationFailureCode strips generic unknown wire code and creationFailureClass separates request from candidate refusals without leaking text', () => {
  assert.deepEqual(creationFailureCode({errorCode: 'unknown'}), {});
  assert.deepEqual(creationFailureCode({errorCode: 'INVALID_MODEL'}), {failure_code: 'INVALID_MODEL'});

  // Paseo workspace/request refusals map to request_invalid, never candidate failure classes.
  assert.equal(creationFailureClass({failedStage: 'workspace', errorCode: 'unknown', error: 'secret path canary'}), 'request_invalid');
  assert.equal(creationFailureClass({errorCode: 'branch_already_checked_out', error: 'secret branch canary'}), 'request_invalid');
  assert.equal(creationFailureClass({errorCode: 'unknown', error: 'Workspace D:/work/games/space_trader not found'}), 'request_invalid');
  const snap = creationRecord;
  assert.equal(typeof snap, 'function');
  // Structured candidate codes map to candidate classes; unclassified errors stay unknown.
  assert.equal(creationFailureClass({errorCode: 'INVALID_MODEL'}), 'model_unavailable');
  assert.equal(creationFailureClass({errorCode: 'QUOTA_EXCEEDED'}), 'quota');
  assert.equal(creationFailureClass({errorCode: 'PROVIDER_UNAVAILABLE'}), 'provider_unavailable');
  assert.equal(creationFailureClass({errorCode: 'unknown', error: 'opaque internal failure'}), 'unknown');
});

// ---- refusal conditions and what a completion notice says ended the worker (audit 2026-11-21, findings 2, 7b, 10) ----

const resumeInput = {operation: 'resume', dispatch_id: 'cond-new', parent_id: 'parent', agent_id: 'worker',
  request: {provider: 'claude/m', title: 't', initialPrompt: 'p', labels: {'router.dispatch_id': 'cond-new'}}};
const conditionFor = async worker => {
  const client = {fetchAgent: async id => ({agent: id === 'parent' ? {cwd: '/fixture'} : worker})};
  return execute(client, resumeInput, notifier);
};
const resumable = {id: 'worker', provider: 'claude', model: 'm', status: 'idle', labels: {'router.parent_id': 'parent', 'router.dispatch_id': 'old'}};

test('a refused resume says which state refused, in the fixed order, beside the unchanged code', async () => {
  const cases = [
    [{status: 'running'}, 'running'],
    [{status: 'initializing'}, 'initializing'],
    [{status: 'error'}, 'error'],
    [{attentionReason: 'error'}, 'error'],
    [{providerUnavailable: true}, 'provider_unavailable'],
    [{pendingPermissions: [{id: 'p'}]}, 'permission_pending'],
    // several hold: the first of the order wins
    [{status: 'running', providerUnavailable: true, pendingPermissions: [{id: 'p'}]}, 'running'],
    [{status: 'error', providerUnavailable: true, pendingPermissions: [{id: 'p'}]}, 'error'],
    [{providerUnavailable: true, pendingPermissions: [{id: 'p'}]}, 'provider_unavailable'],
  ];
  for (const [state, condition] of cases) {
    const result = await conditionFor({...resumable, ...state});
    assert.deepEqual([result.ok, result.sent, result.error, result.condition], [false, false, 'worker_not_idle', condition], JSON.stringify(state));
  }
});

test('a refused resume of a missing worker says whether it is absent or archived', async () => {
  const absent = await execute({fetchAgent: async id => ({agent: id === 'parent' ? {cwd: '/fixture'} : null})}, resumeInput, notifier);
  assert.deepEqual([absent.error, absent.condition], ['worker_not_found', 'absent']);
  const archived = await conditionFor({...resumable, archivedAt: '2026-11-07T00:00:00Z'});
  assert.deepEqual([archived.error, archived.condition], ['worker_not_found', 'archived']);
});

test('a refused send after the relabel carries the class of the failure and no text of it', async () => {
  const canary = 'secret-host-text-canary C:/secret/path token-123';
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-condition-'));
  const previous = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = stateDir;
  try {
    for (const [thrown, expected] of [
      [Object.assign(new Error(canary), {name: 'DaemonConnectionError', code: 'DAEMON_REQUEST_TIMEOUT'}), 'timeout'],
      [Object.assign(new Error(canary), {name: 'DaemonRpcError'}), 'daemon_error'],
      [new TypeError(canary), 'bridge_error'],
    ]) {
      const client = {
        fetchAgent: async id => ({agent: id === 'parent' ? {cwd: '/fixture'} : {...resumable, labels: {'router.parent_id': 'parent'}}}),
        updateAgent: async () => {},
        sendMessage: async () => { throw thrown; },
      };
      const result = await execute(client, resumeInput, notifier);
      assert.deepEqual([result.error, result.condition], ['host_resume_failed', expected]);
      assert.equal(JSON.stringify(result).includes('canary'), false);
      assert.equal(JSON.stringify(result).includes('secret'), false);
    }
  } finally {
    if (previous === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = previous;
    fs.rmSync(stateDir, {recursive: true, force: true});
  }
});

// A class is read from what the real SDK raises, so these use its own classes and clients (the daemon is never needed).
const sdkModule = await import(pathToFileURL(require.resolve('@getpaseo/client/internal/daemon-client')).href);
const {WebSocket: SdkWebSocket, WebSocketServer: SdkWebSocketServer} = require('ws');
const sdkClient = (url, extra = {}) => new sdkModule.DaemonClient({url, clientId: randomUUID(), clientType: 'cli',
  appVersion: '0.11.2', localCredential: () => 'x', reconnect: {enabled: false}, ...extra,
  webSocketFactory: (target, config) => new SdkWebSocket(target, {headers: config?.headers, protocols: config?.protocols})});
const closedPort = async () => {
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const {port} = probe.address();
  await new Promise(resolve => probe.close(resolve));
  return port;
};
const connectFailure = async client => {
  let failure;
  try { await client.connect(); } catch (error) { failure = error; } finally { await client.close().catch(() => {}); }
  assert.ok(failure, 'the connect did not fail');
  return failure;
};

test('a connect failure is classed from the shape the real SDK gives, a missing credential apart from a daemon that is down', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-connect-'));
  const old = process.env.PASEO_HOME;
  process.env.PASEO_HOME = home;
  const servers = [];
  const serve = async handler => {
    const server = net.createServer(handler);
    servers.push(server);
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    return server.address().port;
  };
  const input = {operation: 'create'};
  try {
    fs.writeFileSync(path.join(home, 'config.json'), JSON.stringify({daemon: {listen: `127.0.0.1:${await closedPort()}`}}));
    // the bridge's own client with no local credential file: the daemon may be up, this is not "unreachable"
    const credential = {};
    const unread = await connectFailure(await createClient(credential));
    assert.equal(hostFailure(unread, false, input, credential).condition, 'credential_unreadable');
    // the credential read, the port closed: the daemon is down
    fs.writeFileSync(path.join(home, 'local-credential'), 'x');
    const read = {};
    const refused = await connectFailure(await createClient(read));
    assert.equal(hostFailure(refused, false, input, read).condition, 'refused');
    assert.equal(JSON.stringify(hostFailure(refused, false, input, read)).includes('127.0.0.1'), false);
    // a server that never answers
    const silent = await connectFailure(sdkClient(`ws://127.0.0.1:${await serve(() => {})}/ws`, {connectTimeoutMs: 300}));
    assert.equal(hostFailure(silent, false, input, {}).condition, 'timeout');
    // a server that drops the socket: reachable, but not answering
    const dropped = await connectFailure(sdkClient(`ws://127.0.0.1:${await serve(socket => socket.destroy())}/ws`, {connectTimeoutMs: 2000}));
    assert.equal(hostFailure(dropped, false, input, {}).condition, 'unreachable');
    assert.equal(hostFailure(new sdkModule.DaemonAuthenticationError('password_required'), false, input, {}).condition, 'authentication');
    // a daemon that accepts the connection and closes the handshake with a reason (Paseo does: version, replaced
    // connection, output limit): reached, so not "unreachable", and its words are not in the answer
    const reachedServer = new SdkWebSocketServer({host: '127.0.0.1', port: 0});
    servers.push({close: () => reachedServer.close()});
    await new Promise(resolve => reachedServer.once('listening', resolve));
    reachedServer.on('connection', socket => socket.close(1008, 'Unsupported client version secret-daemon-words'));
    fs.writeFileSync(path.join(home, 'config.json'), JSON.stringify({daemon: {listen: `127.0.0.1:${reachedServer.address().port}`}}));
    const reached = {};
    const closedByDaemon = await connectFailure(await createClient(reached));
    assert.equal(reached.opened, true);
    assert.deepEqual(hostFailure(closedByDaemon, false, input, reached),
      {ok: false, sent: false, error: 'host_connection_failed', condition: 'rejected'});
    // the same refusal without the daemon having answered stays what it was
    assert.equal(hostFailure(closedByDaemon, false, input, {}).condition, 'unreachable');
  } finally {
    for (const server of servers) server.close();
    if (old === undefined) delete process.env.PASEO_HOME; else process.env.PASEO_HOME = old;
    fs.rmSync(home, {recursive: true, force: true});
  }
});

test('a request failure is classed from what the real SDK raises; the daemon\'s own text never decides the class', async () => {
  const canary = 'secret-host-text-canary C:/secret/path';
  const idle = sdkClient('ws://127.0.0.1:1/ws', {webSocketFactory: () => { throw new Error('no socket'); }});
  const raised = async call => { try { await call(); } catch (error) { return error; } assert.fail('nothing was raised'); };
  const classOf = error => hostFailure(error, true, {operation: 'resume'}, {}).condition;
  try {
    // what the SDK raises on its own: a request it refuses to send, a client that is not connected
    const invalid = await raised(() => idle.sendMessage('w', undefined, {messageId: 'd'}));
    assert.equal(invalid.name, 'ZodError');
    assert.equal(classOf(invalid), 'bridge_error');
    const labels = await raised(() => idle.updateAgent('w', {name: 't', labels: {a: 1}}));
    assert.equal(labels.name, 'ZodError');
    assert.equal(classOf(labels), 'bridge_error');
    const notConnected = await raised(() => idle.sendMessage('w', 'p', {messageId: 'd'}));
    assert.match(notConnected.message, /^Transport not connected/);
    assert.equal(classOf(notConnected), 'connection_lost');
    assert.equal(classOf(new Error('Daemon client closed')), 'connection_lost');
    // the WebSocket transport's own text, raised when the socket is closing while the client still reads connected
    for (const state of [0, 2, 3]) assert.equal(classOf(new Error(`WebSocket not open (readyState=${state})`)), 'connection_lost');
    assert.equal(classOf(new Error('The provider said WebSocket not open (readyState=2)')), 'daemon_error');
    assert.equal(classOf(new sdkModule.DaemonConnectionError('Connection lost')), 'connection_lost');
    assert.equal(classOf(new sdkModule.DaemonConnectionError('Timed out waiting', 'DAEMON_REQUEST_TIMEOUT')), 'timeout');
    // the daemon's answer reaches the bridge as `new Error(payload.error)`: whatever it says, the connection is up
    for (const text of ['codex session is closed', 'claude process is closed', 'Codex app-server request timed out for turn/start',
      'ACP initialize timed out after 30000ms', 'Transport not connected to the provider', 'Daemon client closed by the provider', canary])
      assert.equal(classOf(new Error(text)), 'daemon_error', text);
    // the SDK's own typed errors, in the shape each has
    assert.equal(classOf(Object.assign(new Error('x'), {name: 'DaemonRpcError', code: 'whatever'})), 'daemon_error');
    assert.equal(classOf(Object.assign(new Error('x'), {name: 'DaemonProtocolError', code: 'invalid_response'})), 'invalid_response');
    assert.equal(classOf(new sdkModule.DaemonAuthenticationError('incorrect_password')), 'authentication');
    // the bridge's own code threw
    assert.equal(classOf(new TypeError(canary)), 'bridge_error');
    // the bridge's own thrown errors, as its code really throws them (marked by type, not read from words)
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-own-'));
    const oldState = process.env.ROUTER_STATE_DIR;
    process.env.ROUTER_STATE_DIR = root;
    try {
      const own = [
        await raised(() => execute({}, {operation: 'no-such-operation'})),
        await raised(() => notificationFile('not a valid id!')),
        await raised(() => { const f = notificationFile('mismatch'); fs.mkdirSync(path.dirname(f), {recursive: true});
          fs.writeFileSync(f, JSON.stringify({dispatch_id: 'mismatch', parent_id: 'someone-else', status: 'watching'}));
          return ensureNotification({dispatch_id: 'mismatch', parent_id: 'parent'}); }),
        await raised(() => { const f = notificationFile('isdir'); fs.mkdirSync(f, {recursive: true}); return notificationReceipt(f); }),
      ];
      for (const error of own) assert.equal(classOf(error), 'bridge_error', error.message);
      assert.equal(own[3].cause.code, 'EISDIR');
    } finally {
      if (oldState === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = oldState;
      fs.rmSync(root, {recursive: true, force: true});
    }
    for (const error of [invalid, notConnected, new Error(canary), new TypeError(canary)])
      assert.equal(JSON.stringify(hostFailure(error, true, {operation: 'create'}, {})).includes('canary'), false);
    assert.equal(hostFailure(invalid, true, {operation: 'create'}, {}).sent, true);
    assert.equal(hostFailure(invalid, true, {operation: 'observe'}, {}).sent, false);
  } finally { await idle.close().catch(() => {}); }
});

// The notice, split at the continuation verdict into the header the parent parses and the body this change says.
const noticeParts = async (client, record, event) => {
  const text = await composeNotice(client, record, event);
  const verdict = await continuationVerdict(record.agent_id ?? record.dispatch_id);
  const at = text.indexOf(verdict);
  assert.ok(at > 0, 'the notice has no continuation verdict');
  return {header: text.slice(0, at), body: text.slice(at + verdict.length + 1)};
};
const noticeBase = {dispatch_id: 'd1', parent_id: 'p1', agent_id: 'a1', created_at: Date.now() - 1000, creation_deadline: Date.now() + 1e6};
const timeline = entries => ({fetchAgentTimeline: async () => ({entries: entries.map(([type, text, ids]) => ({item: {type, text, ...(ids ?? {})}}))})});
const SUPERSEDED_TEXT = 'superseded by a later dispatch';
const NOT_LANDED_STATE = 'ended; no later turn of this parent landed on this worker';
const NOT_LANDED = "A later dispatch took this worker's labels, but AgentCompass recorded no turn of it from this " +
  "parent that landed on this worker, so this is this dispatch's own end.\n";

test('each ending of a worker has its own sentence in the body, under the unchanged State header', async () => {
  const none = timeline([]);
  const expected = [
    ['worker unavailable', {}, 'Paseo no longer lists this worker (it was removed or archived); it left no final reply.\n'],
    ['closed', {}, 'Paseo closed this worker; it left no final reply.\n'],
    ['finished', {}, 'The worker finished its turn without a final reply.\n'],
    ['error', {terminal: {reason: 'error', status: 'error', attentionReason: null}},
      "The worker's turn ended in an error and the provider gave no message; it left no final reply.\n"],
    ['error', {terminal: {reason: 'error', status: 'idle', attentionReason: 'error'}},
      "The worker's turn ended in an error and the provider gave no message; it left no final reply.\n"],
    ['error', {terminal: {reason: 'error', provider_unavailable: true}},
      "The worker's provider became unavailable; the worker left no final reply.\n"],
    ['notification watch expired; check worker status', {},
      'The notification watch expired before this worker was seen to end; check it with get_agent_status. It left no final reply that was read.\n'],
    ['unknown', {}, 'This dispatch ended, but the notification watch did not see how: whether it finished, failed or was canceled is not known here. ' +
      'Check the worker with get_agent_status (agentId: a1).\n'],
  ];
  for (const [reason, extra, sentence] of expected) {
    const {header, body} = await noticeParts(none, {...noticeBase, ...extra}, {key: 'terminal', reason});
    assert.ok(header.includes(`State: ${reason}\n`), reason);
    assert.equal(body, sentence, `${reason} ${JSON.stringify(extra)}`);
  }
  // an error with the provider's message and no reply: the message is printed, so it is not said to be missing
  const message = {terminal: {reason: 'error', failure_code: TURN_FAILED_CODE, provider_message: 'model gpt-x is not available on this plan',
    status: 'error', attentionReason: null}};
  const {body} = await noticeParts(none, {...noticeBase, ...message}, {key: 'terminal', reason: 'error'});
  assert.match(body, /^The worker's turn ended with this error: model gpt-x is not available on this plan\n/);
  assert.ok(body.endsWith('holds nothing.\nThe worker left no final reply.\n'), body);
  assert.doesNotMatch(body, /gave no message/);
  // a worker that ended with a reply still carries it, whatever ended it
  const reply = timeline([['assistant_message', 'the report']]);
  assert.equal((await noticeParts(reply, noticeBase, {key: 'terminal', reason: 'closed'})).body, 'Final reply:\nthe report\n');
});

test("a superseded notice, sent once the registry found no landed later turn, is this dispatch's own end in its header too", async () => {
  // The watcher only claims a superseded record; only the catch-up composes it, once the registry found no landed turn.
  for (const [entries, body] of [
    [[{item: {type: 'user_message', text: 'brief'}}, {item: {type: 'assistant_message', text: 'this dispatch reply'}}],
      `${NOT_LANDED}Final reply:\nthis dispatch reply\n`],
    [[{item: {type: 'user_message', text: 'brief'}}], `${NOT_LANDED}This dispatch's turn left no final reply.\n`]]) {
    await withNoticeState(async () => {
      writeRecord('child', {dispatch_id: 'child', parent_id: 'parent', status: 'continued', continued_by: 'later-dispatch',
        delivered: [], seen: true, pending: null, terminal: {reason: SUPERSEDED_TEXT}});
      const sends = [];
      const client = {fetchAgent: async () => ({agent: {status: 'idle'}}), fetchAgentTimeline: async () => ({entries}),
        sendMessage: async (...args) => sends.push(args)};
      assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'})).sent, true);
      const text = sends[0][1];
      assert.ok(text.includes(`State: ${NOT_LANDED_STATE}\n`), text);
      assert.ok(!text.includes(`State: ${SUPERSEDED_TEXT}`), text);
      assert.ok(text.endsWith(body), text);
    });
  }
});

test('a catch-up for a receipt with no terminal invents no finish', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-notify-'));
  const old = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = root;
  try {
    const file = notificationFile('child');
    fs.mkdirSync(path.dirname(file), {recursive: true});
    const sent = [];
    const client = {
      fetchAgent: async id => ({agent: {id, status: 'error'}}),
      fetchAgentTimeline: async () => ({entries: []}),
      sendMessage: async (...args) => sent.push(args),
    };
    fs.writeFileSync(file, JSON.stringify({dispatch_id: 'child', parent_id: 'parent', status: 'watching', delivered: [], pending: null}));
    assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'})).sent, true);
    assert.equal(sent.length, 1);
    assert.match(sent[0][1], /\nState: unknown\n/);
    assert.doesNotMatch(sent[0][1], /State: finished|finished its turn/);
    assert.match(sent[0][1], /whether it finished, failed or was canceled is not known here/);
    // a receipt that has a terminal says what it carries
    fs.writeFileSync(file, JSON.stringify({dispatch_id: 'child', parent_id: 'parent', status: 'watching', delivered: [], pending: null,
      terminal: {reason: 'closed'}}));
    assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'})).sent, true);
    assert.match(sent[1][1], /\nState: closed\n/);
    // no terminal, and the registry says what it ended the dispatch as: the notice says that, as the registry's record
    const bare = JSON.stringify({dispatch_id: 'child', parent_id: 'parent', status: 'watching', delivered: [], pending: null});
    for (const state of RECORDED_ENDS) {
      fs.writeFileSync(file, bare);
      assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent', ended_as: state})).sent, true);
      const text = sent.at(-1)[1];
      assert.ok(text.includes(`\nState: recorded as ${state}; the notification watch did not see the end\n`), text);
      assert.ok(text.includes(`The dispatch registry recorded this dispatch as ${state}, but the notification watch did not see the end`), text);
      assert.doesNotMatch(text, /State: unknown|State: finished|finished its turn/);
    }
    // a value that is no state of the registry is not printed: the notice stays `unknown`
    for (const other of ['finished', 'launched', 'completed\nState: finished', 7, null]) {
      fs.writeFileSync(file, bare);
      assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent', ended_as: other})).sent, true);
      assert.match(sent.at(-1)[1], /\nState: unknown\n/);
      assert.equal(sent.at(-1)[1].split('\nState: ').length, 2);
    }
  } finally {
    if (old === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = old;
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('the watcher records that a provider became unavailable, and a released watcher\'s terminal reads the same', async () => {
  const f = watchFixture();
  f.setAgent({status: 'running', providerUnavailable: true});
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.record.terminal.provider_unavailable, true);
  assert.match(f.calls[0][1], /provider became unavailable/);
  // a terminal written before the flag existed: an `error` end of a worker that was neither in status error nor in
  // attention error can only have come from providerUnavailable (notificationEvents)
  const provider = /provider became unavailable/, plain = /the provider gave no message/;
  for (const [terminal, expect] of [
    [{reason: 'error', status: 'running', attentionReason: null}, provider],
    [{reason: 'error', status: 'idle', attentionReason: null}, provider],
    [{reason: 'error', status: 'error', attentionReason: null}, plain],
    [{reason: 'error', status: 'running', attentionReason: 'error'}, plain],
    [{reason: 'error', status: null, attentionReason: null}, plain],
  ]) {
    const {body} = await noticeParts(timeline([]), {...noticeBase, terminal}, {key: 'terminal', reason: 'error'});
    assert.match(body, expect, JSON.stringify(terminal));
  }
});

test('a poll\'s late save over the stop of a refused resume does not keep the create from retiring the old watcher', async () => {
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
  const file = path.join(root, 'paseo-notifications', 'raced.json');
  const record = () => JSON.parse(fs.readFileSync(file, 'utf8'));
  try {
    process.env.ROUTER_PASEO_SDK_ROOT = fakeNotifierSdk(root);
    process.env.ROUTER_STATE_DIR = root;
    process.env.PASEO_HOME = root;
    fs.writeFileSync(path.join(root, 'agent.json'), JSON.stringify({status: 'idle', labels: {'router.dispatch_id': 'before'}}));
    const request = {provider: 'claude/m', title: 't', initialPrompt: 'p', labels: {'router.dispatch_id': 'raced'}};
    const old = {id: 'old-worker', provider: 'claude', model: 'm', status: 'idle', labels: {'router.parent_id': 'parent'}};
    const refused = await execute({
      fetchAgent: async id => ({agent: id === 'parent' ? {cwd: '/fixture'} : old}),
      updateAgent: async () => {},
      sendMessage: async () => {throw new Error('fixture refusal');},
    }, {operation: 'resume', dispatch_id: 'raced', parent_id: 'parent', agent_id: 'old-worker', request});
    assert.equal(refused.error, 'host_resume_failed');
    const firstPid = record().pid;
    pids.add(firstPid);
    // The old watcher stops at its next poll. Its last save was made before it saw the stop marker, so it can land
    // after the stop's own save: the interleaving, made on purpose. The marker stays; the record says `watching`.
    await until(() => !running(firstPid));
    assert.equal(fs.existsSync(`${file}.creation-failed`), true);
    fs.writeFileSync(file, JSON.stringify({...record(), status: 'watching'}));
    assert.equal(record().status, 'watching');

    const created = await execute({
      fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
      createAgent: async options => ({id: options.agentId, provider: 'claude', model: 'm', status: 'running', labels: options.labels}),
    }, {operation: 'create', dispatch_id: 'raced', parent_id: 'parent', request,
      create_deadline: new Date(Date.now() + 60000).toISOString()});
    assert.equal(created.ok, true);
    pids.add(created.completion_notification.pid);
    assert.equal(created.completion_notification.status, 'watching');
    assert.notEqual(created.completion_notification.pid, firstPid);
    assert.equal(record().agent_id, undefined);
    assert.equal(fs.existsSync(`${file}.creation-failed`), false);
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

test('a finished record that has a stop marker is returned to a create, not retired', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-notify-'));
  const oldEnv = {...process.env};
  try {
    process.env.ROUTER_PASEO_SDK_ROOT = fakeNotifierSdk(root);
    process.env.ROUTER_STATE_DIR = root;
    process.env.PASEO_HOME = root;
    for (const status of ['delivered', 'continued']) {
      const id = `ended-${status}`;
      const file = path.join(root, 'paseo-notifications', `${id}.json`);
      fs.mkdirSync(path.dirname(file), {recursive: true});
      const ended = {dispatch_id: id, parent_id: 'parent', agent_id: 'old-worker', status, delivered: ['terminal'], pid: 2147483647};
      fs.writeFileSync(file, JSON.stringify(ended));
      fs.writeFileSync(`${file}.creation-failed`, 'host_resume_failed');
      const request = {provider: 'claude/m', title: 't', initialPrompt: 'p', labels: {'router.dispatch_id': id}};
      const created = await execute({
        fetchAgent: async () => ({agent: {cwd: '/fixture'}}),
        createAgent: async options => ({id: options.agentId, provider: 'claude', model: 'm', status: 'running', labels: options.labels}),
      }, {operation: 'create', dispatch_id: id, parent_id: 'parent', request, create_deadline: new Date(Date.now() + 60000).toISOString()});
      assert.equal(created.completion_notification.status, status);
      assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), ended);
      assert.equal(fs.existsSync(`${file}.creation-failed`), true);
    }
  } finally {
    for (const name of ['ROUTER_STATE_DIR', 'ROUTER_PASEO_SDK_ROOT', 'PASEO_HOME']) {
      if (oldEnv[name] === undefined) delete process.env[name]; else process.env[name] = oldEnv[name];
    }
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('a host failure\'s condition is a token of its code\'s vocabulary, and the vocabulary lists no token that is never produced', () => {
  const named = (name, message = 'x', extra = {}) => Object.assign(new Error(message), {name}, extra);
  const errors = [named('DaemonAuthenticationError'), new BridgeError('refused input'),
    named('DaemonConnectionError', 'x', {code: 'DAEMON_REQUEST_TIMEOUT'}), named('DaemonConnectionError'),
    named('DaemonProtocolError'), Object.assign(new Error('x'), {code: 'invalid_response'}), named('DaemonRpcError'),
    named('ZodError'), new TypeError('x'), new Error('Connection timed out'), new Error('connect ECONNREFUSED 127.0.0.1:1'),
    new Error('Daemon client closed'), new Error('session is closed'), new Error(''), null];
  const produced = {host_connection_failed: new Set(), host_request_failed: new Set()};
  for (const error of errors) for (const connected of [false, true])
    for (const credential of [{}, {unreadable: true}, {opened: true}]) {
      const answer = hostFailure(error, connected, {operation: 'create'}, credential);
      assert.equal(answer.error, connected ? 'host_request_failed' : 'host_connection_failed');
      assert.equal(answer.condition, hostFailureClass(error, connected, credential));
      assert.ok(CONDITIONS[answer.error].includes(answer.condition), `${answer.error}: ${answer.condition}`);
      produced[answer.error].add(answer.condition);
    }
  for (const code of Object.keys(produced)) assert.deepEqual([...produced[code]].sort(), [...CONDITIONS[code]].sort());
  assert.deepEqual(CONDITIONS.host_resume_failed, CONDITIONS.host_request_failed);
});

// --- A send whose outcome Paseo does not know, a later dispatch on the worker, a late close after the reply ---

const UNKNOWN = 'agent_request_outcome_unknown';
const idOf = (dispatchId, attempt = 0) => createHash('sha256')
  .update(`router-finish:${dispatchId}:terminal${attempt ? `:after-unknown:${attempt}` : ''}`).digest('hex');
// A parent timeline row as Paseo writes it: an accepted prompt carries the entry's turnId, a rejected one none.
const accepted = (clientMessageId, extra = {}) => ({item: {type: 'user_message', text: 'x', clientMessageId, messageId: clientMessageId},
  turnId: 'turn-1', ...extra});
const rejected = clientMessageId => ({item: {type: 'user_message', text: 'x', clientMessageId, messageId: clientMessageId}});
// The row Paseo writes right after a prompt whose turn failed to start (agent-manager.ts appendSystemErrorTimelineMessage).
const systemError = () => ({item: {type: 'assistant_message', text: '[System Error] Failed to start turn'}});
// A row replayed from provider history after a daemon restart: no turnId, and no error rows anywhere.
const replayed = clientMessageId => ({item: {type: 'user_message', text: 'x', clientMessageId}, timestamp: '2026-11-21T08:00:01.000Z'});

// A timeline read back in pages of two from the newest, as the daemon's `before` cursor does.
function pagedTimeline(entries, reads = []) {
  return async (agentId, options) => {
    reads.push({agentId, direction: options.direction, cursor: options.cursor ?? null});
    const end = options.cursor ? Number(options.cursor) : entries.length;
    const start = Math.max(0, end - 2);
    return {entries: entries.slice(start, end).map(entry => (entry.item ? entry : {item: entry})), hasOlder: start > 0,
      startCursor: start > 0 ? String(start) : null};
  };
}

async function withNoticeState(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'router-unknown-'));
  const old = process.env.ROUTER_STATE_DIR;
  process.env.ROUTER_STATE_DIR = root;
  try { return await run(); }
  finally {
    if (old === undefined) delete process.env.ROUTER_STATE_DIR; else process.env.ROUTER_STATE_DIR = old;
    fs.rmSync(root, {recursive: true, force: true});
  }
}

function writeRecord(dispatchId, record) {
  const file = notificationFile(dispatchId);
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, JSON.stringify(record));
  return file;
}

const SAVED_AT = '2026-11-21T08:00:00.000Z';
const pendingRecord = (extra = {}) => ({dispatch_id: 'child', parent_id: 'parent', status: 'watching', delivered: [],
  seen: true, pending: {key: 'terminal', messageId: idOf('child'), text: 'THE NOTICE', saved_at: SAVED_AT, sent_at: SAVED_AT},
  terminal: {reason: 'finished'}, ...extra});
// Past the first resend interval since the record's first send (RESEND_AFTER_MS): a next id is due.
const LATER = Date.parse(SAVED_AT) + 16 * MINUTE;
const unknownClient = (timelineEntries, sends, reads = []) => ({
  fetchAgent: async id => ({agent: {id, status: 'running'}}),
  fetchAgentTimeline: pagedTimeline(timelineEntries, reads),
  sendMessage: async (...args) => {sends.push(args); throw new Error(UNKNOWN);},
});

test('an unknown send outcome whose notice Paseo accepted in the parent timeline is delivered and is not sent again', async () => {
  await withNoticeState(async () => {
    const file = writeRecord('child', pendingRecord());
    const sends = [], reads = [];
    const client = unknownClient([accepted(idOf('child')), {type: 'assistant_message', text: 'y'},
      accepted('other'), {type: 'assistant_message', text: 'w'}, {type: 'tool_call', callId: 'c'}], sends, reads);
    const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'});
    assert.deepEqual(answer, {ok: true, sent: false, already: true, evidence: 'delivered', outcome: 'found', message_id: idOf('child')});
    assert.equal(sends.length, 1);
    assert.equal(sends[0][2].messageId, idOf('child'));
    // the parent's timeline is the one read, back to the page that holds the notice
    assert.ok(reads.length >= 3 && reads.every(read => read.agentId === 'parent'));
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.status, 'delivered');
    assert.equal(saved.delivery_outcome, 'found');
    assert.equal(saved.delivery_message_id, idOf('child'));
    assert.equal(saved.pending, null);
    assert.equal(JSON.stringify(saved).includes('THE NOTICE'), false);
    // a later catch-up reads how the receipt ended, and sends nothing
    assert.deepEqual(await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}),
      {ok: true, sent: false, already: true, evidence: 'delivered', delivery_outcome: 'found', message_id: idOf('child')});
    assert.equal(sends.length, 1);
  });
});

test('a notice Paseo rejected (its turn failed to start) is not a delivery: it is sent once under the next id', async () => {
  await withNoticeState(async () => {
    writeRecord('child', pendingRecord());
    const sends = [];
    const client = unknownClient([rejected(idOf('child')), systemError()], sends);
    client.sendMessage = async (...args) => {sends.push(args); if (args[2].messageId === idOf('child')) throw new Error(UNKNOWN);};
    const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, LATER);
    assert.deepEqual(answer, {ok: true, sent: true, message_id: idOf('child', 1), outcome: 'resent'});
    assert.deepEqual(sends.map(call => call[2].messageId), [idOf('child'), idOf('child', 1)]);
  });
});

test('an unknown send outcome whose notice is not in the parent timeline is sent once under the next derived id', async () => {
  await withNoticeState(async () => {
    const file = writeRecord('child', pendingRecord());
    const sends = [];
    // another dispatch's notice and this dispatch's own resume prompt are not this notice
    const parentTimeline = [accepted(idOf('other-dispatch')), accepted('child'),
      {type: 'assistant_message', text: 'z', messageId: idOf('child')}];
    let savedBeforeResend = null;
    const client = unknownClient(parentTimeline, sends);
    client.sendMessage = async (...args) => {
      sends.push(args);
      if (args[2].messageId === idOf('child')) throw new Error(UNKNOWN);
      savedBeforeResend = JSON.parse(fs.readFileSync(file, 'utf8'));
    };
    const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, LATER);
    assert.deepEqual(answer, {ok: true, sent: true, message_id: idOf('child', 1), outcome: 'resent'});
    assert.deepEqual(sends.map(call => call[2].messageId), [idOf('child'), idOf('child', 1)]);
    assert.deepEqual(sends.map(call => call[1]), ['THE NOTICE', 'THE NOTICE']);
    assert.equal(savedBeforeResend.pending.attempt, 1);
    assert.equal(savedBeforeResend.pending.messageId, idOf('child', 1));
    assert.equal(savedBeforeResend.pending.saved_at, SAVED_AT, 'the first save time stays');
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.status, 'delivered');
    assert.equal(saved.delivery_outcome, 'resent');
    assert.equal(saved.delivery_message_id, idOf('child', 1));
  });
});

test('two observers of one unknown outcome send under one derived id, and a resend whose outcome is unknown looks for every earlier id', async () => {
  await withNoticeState(async () => {
    writeRecord('child', pendingRecord());
    const resent = [];
    const client = unknownClient([], []);
    client.sendMessage = async (to, text, options) => {
      if (options.messageId === idOf('child')) throw new Error(UNKNOWN);
      resent.push(options.messageId);
    };
    await Promise.all([notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, LATER),
      notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, LATER)]);
    assert.deepEqual(resent, [idOf('child', 1), idOf('child', 1)]);
  });
  await withNoticeState(async () => {
    const file = writeRecord('child', pendingRecord({pending: {key: 'terminal', text: 'THE NOTICE', attempt: 1, saved_at: SAVED_AT}}));
    const sends = [];
    // the first send landed after all: its accepted row carries the attempt-0 id
    const answer = await notifyEnd(unknownClient([accepted(idOf('child'))], sends), {dispatch_id: 'child', parent_id: 'parent'});
    assert.equal(answer.outcome, 'found');
    assert.equal(answer.message_id, idOf('child'));
    assert.deepEqual(sends.map(call => call[2].messageId), [idOf('child', 1)]);
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).status, 'delivered');
  });
});

test('a next id waits its interval after the previous send, and the notice is undelivered only after the third', async () => {
  await withNoticeState(async () => {
    const file = writeRecord('child', pendingRecord({pending: {key: 'terminal', text: 'THE NOTICE', saved_at: SAVED_AT}}));
    const sends = [], reads = [];
    // Every send fails in the daemon after its receipt was written (a parent whose turns are rejected for a while): the
    // first call of an id throws a restart, a later call of the same id answers unknown, and no row is ever written.
    const tried = new Set();
    const client = unknownClient([], sends, reads);
    client.sendMessage = async (...args) => {
      sends.push(args[2].messageId);
      if (tried.has(args[2].messageId)) throw new Error(UNKNOWN);
      tried.add(args[2].messageId);
      throw new Error('daemon restarted');
    };
    const t0 = Date.parse(SAVED_AT);
    const read = async minutes => {
      try { return await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, t0 + minutes * MINUTE); }
      catch (error) { return error.message; }
    };
    const wait = id => ({ok: false, error: 'notice_resend_wait', message_id: id});
    assert.equal(await read(0), 'daemon restarted');
    assert.deepEqual(await read(1), wait(idOf('child')), 'not in the timeline, but the next id is not due');
    const quiet = [sends.length, reads.length];
    assert.deepEqual(await read(14), wait(idOf('child')));
    assert.deepEqual([sends.length, reads.length], quiet, 'nothing is sent or read before it is due');
    assert.equal(await read(15), 'daemon restarted', 'the second id at 15 minutes');
    assert.deepEqual(await read(16), wait(idOf('child', 1)));
    assert.deepEqual(await read(74), wait(idOf('child', 1)));
    assert.equal(await read(75), 'daemon restarted', 'the third id 60 minutes after the second');
    const undelivered = {ok: true, sent: false, error: 'notice_undelivered', message_id: idOf('child', 2)};
    assert.deepEqual(await read(76), {...undelivered, outcome: 'undelivered'});
    const before = sends.length;
    assert.deepEqual(await read(500), undelivered, 'nothing more is sent');
    assert.equal(sends.length, before);
    assert.deepEqual([...new Set(sends)], [idOf('child'), idOf('child', 1), idOf('child', 2)]);
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.status, 'undelivered');
    assert.equal(saved.pending, null);
  });
  // The watcher's step keeps polling while it waits, and its pending notice stays.
  await withNoticeState(async () => {
    const record = pendingRecord({pending: {key: 'terminal', text: 'THE NOTICE', saved_at: SAVED_AT, sent_at: SAVED_AT,
      messageId: idOf('child')}});
    const client = unknownClient([], []);
    client.fetchAgent = async id => ({agent: id === 'parent' ? {status: 'running'} : {status: 'idle', attentionReason: 'finished'}});
    assert.equal(await notificationStep(client, record, () => {}, Date.parse(SAVED_AT) + MINUTE, null), false);
    assert.equal(record.pending.unknown, true);
    assert.equal(record.status, 'watching');
  });
});

test('a notice row counts unless Paseo rejected it: accepted live, rejected live, and replayed from history', async () => {
  for (const [name, entries, outcome] of [
    ['accepted live', [accepted(idOf('child')), {type: 'assistant_message', text: 'ok'}], 'found'],
    ['rejected live', [rejected(idOf('child')), systemError()], 'resent'],
    // the error row on the newer page: the rejected row ends the older one
    ['rejected live across pages', [{type: 'assistant_message', text: 'a'}, rejected(idOf('child')), systemError(),
      {type: 'assistant_message', text: 'b'}], 'resent'],
    ['replayed from history', [replayed(idOf('child')), {item: {type: 'assistant_message', text: 'reply'}}], 'found'],
    ['replayed, the newest row', [{item: {type: 'assistant_message', text: 'a'}}, replayed(idOf('child'))], 'found']]) {
    await withNoticeState(async () => {
      writeRecord('child', pendingRecord());
      const sends = [];
      const client = unknownClient(entries, sends);
      client.sendMessage = async (...args) => {sends.push(args); if (args[2].messageId === idOf('child')) throw new Error(UNKNOWN);};
      const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, LATER);
      assert.equal(answer.outcome, outcome, name);
      assert.equal(sends.length, outcome === 'found' ? 1 : 2, name);
    });
  }
});

test('a pending notice a v0.7.4 install wrote searches back to its own terminal sighting', async () => {
  await withNoticeState(async () => {
    const terminalAt = '2026-11-21T06:00:00.000Z';
    const at = minutes => new Date(Date.parse(terminalAt) + minutes * MINUTE).toISOString();
    // v0.7.4's receipt: no saved_at, no sent_at, the terminal sighting dated
    writeRecord('child', {dispatch_id: 'child', parent_id: 'parent', agent_id: 'worker', status: 'watching', delivered: [],
      seen: true, created_at: Date.parse(terminalAt) - 30 * MINUTE, deadline: Date.parse(terminalAt) + 7 * 24 * 60 * MINUTE,
      creation_deadline: Date.parse(terminalAt) - 28 * MINUTE,
      pending: {key: 'terminal', reason: 'finished', messageId: idOf('child'), text: 'THE NOTICE'},
      terminal: {reason: 'finished', status: 'idle', attentionReason: 'finished', at: terminalAt}});
    // the notice lands a minute after the sighting; the parent then works on, many pages
    const rows = [accepted(idOf('child'), {timestamp: at(1)})];
    for (let n = 2; n < 14; n++) rows.push({item: {type: 'assistant_message', text: `work ${n}`}, timestamp: at(n)});
    const sends = [], reads = [];
    const answer = await notifyEnd(unknownClient(rows, sends, reads), {dispatch_id: 'child', parent_id: 'parent'},
      Date.parse(terminalAt) + 300 * MINUTE);
    assert.equal(answer.outcome, 'found');
    assert.equal(sends.length, 1, 'the one send of its own id, answered unknown; nothing new is sent');
    assert.ok(reads.length >= 7, String(reads.length));
  });
});

test("a catch-up does not write over a receipt whose watcher is still running the attempt's fallback", async () => {
  for (const status of ['running', 'scheduled', 'waiting']) {
    await withNoticeState(async () => {
      const record = {dispatch_id: 'child', parent_id: 'parent', status: 'watching', delivered: [], seen: true,
        pending: null, pid: process.pid, terminal: {reason: 'error', status: 'error', at: SAVED_AT},
        fallback: {status, at: SAVED_AT}};
      const file = writeRecord('child', record);
      const before = fs.readFileSync(file, 'utf8');
      const sends = [];
      const client = {fetchAgent: async () => ({agent: {status: 'idle'}}), fetchAgentTimeline: async () => ({entries: []}),
        sendMessage: async (...args) => sends.push(args)};
      assert.deepEqual(await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}), {ok: false, error: 'fallback_pending'});
      assert.equal(sends.length, 0, status);
      assert.equal(fs.readFileSync(file, 'utf8'), before, status);
      // a watcher that died with its fallback open holds nothing: the registry asked, so the notice goes
      writeRecord('child', {...record, pid: 2 ** 30});
      assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'})).sent, true, status);
    });
  }
});

test('the search for a notice stops at rows older than its first save, and a pending with no time of its own reads one page', async () => {
  const at = offset => new Date(Date.parse(SAVED_AT) + offset * 1000).toISOString();
  // newest first in reading order: two pages after the save, then a page that reaches before it, then older ones
  const rows = [accepted(idOf('child'), {timestamp: at(-300)}), {item: {type: 'assistant_message', text: 'a'}, timestamp: at(-200)},
    {item: {type: 'assistant_message', text: 'b'}, timestamp: at(-100)}, {item: {type: 'assistant_message', text: 'c'}, timestamp: at(-10)},
    {item: {type: 'assistant_message', text: 'd'}, timestamp: at(10)}, {item: {type: 'assistant_message', text: 'e'}, timestamp: at(20)},
    {item: {type: 'assistant_message', text: 'f'}, timestamp: at(30)}, {item: {type: 'assistant_message', text: 'g'}, timestamp: at(40)}];
  for (const [pending, pages] of [[{saved_at: SAVED_AT}, 3], [{}, 1]]) {
    await withNoticeState(async () => {
      writeRecord('child', pendingRecord({pending: {key: 'terminal', text: 'THE NOTICE', sent_at: SAVED_AT, ...pending}}));
      const sends = [], reads = [];
      const client = unknownClient(rows, sends, reads);
      client.sendMessage = async (...args) => {sends.push(args); if (args[2].messageId === idOf('child')) throw new Error(UNKNOWN);};
      const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}, LATER);
      assert.equal(answer.outcome, 'resent', JSON.stringify(pending));
      assert.equal(reads.length, pages, JSON.stringify(pending));
    });
  }
});

test('an unknown outcome with a parent timeline that cannot be read decides nothing and keeps the notice pending', async () => {
  await withNoticeState(async () => {
    const file = writeRecord('child', pendingRecord());
    const sends = [];
    const client = unknownClient([], sends);
    client.fetchAgentTimeline = async () => { throw new Error('daemon busy'); };
    await assert.rejects(notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'}));
    assert.equal(sends.length, 1);
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.status, 'watching');
    assert.equal(saved.pending.text, 'THE NOTICE');
    assert.equal(saved.pending.attempt ?? 0, 0);
  });
});

test("the watcher's send follows the same rule: an unknown outcome found accepted in the parent timeline is delivered", async () => {
  const f = watchFixture();
  f.client.fetchAgentTimeline = async id => id === 'parent'
    ? {entries: [accepted(idOf('child'))], hasOlder: false}
    : {entries: [{item: {type: 'assistant_message', text: 'the report'}}]};
  f.client.sendMessage = async (...args) => {f.calls.push(args); throw new Error(UNKNOWN);};
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.record.status, 'delivered');
  assert.equal(f.record.delivery_outcome, 'found');
  assert.equal(f.record.delivery_message_id, idOf('child'));
  assert.equal(typeof f.saved.find(saved => saved.pending)?.pending.saved_at, 'string');
  assert.deepEqual(f.record.delivered, ['terminal']);
});

test('the watcher that sees a later dispatch on its worker sends nothing and leaves the pending notice as a claim', async () => {
  const resume = resumeFixture();
  resume.record.seen = true;
  resume.record.pending = {key: 'terminal', messageId: idOf('new-dispatch'), text: 'THE NOTICE', saved_at: SAVED_AT};
  resume.target = async () => ({agent: {status: 'idle', labels: {'router.dispatch_id': 'later-dispatch', 'router.parent_id': 'parent'}}});
  assert.equal(await notificationStep(resume.client, resume.record, resume.persist, 300000), true);
  assert.equal(resume.calls.length, 0);
  assert.equal(resume.record.status, 'continued');
  assert.equal(resume.record.continued_by, 'later-dispatch');
  assert.equal(resume.record.pending.text, 'THE NOTICE', 'kept for a catch-up if the later turn never lands');
  // a resume never seen bound: its other label is the previous dispatch's, and claims nothing
  const unbound = resumeFixture();
  unbound.target = async () => ({agent: {status: 'idle', attentionReason: 'finished', labels: {'router.dispatch_id': 'previous'}}});
  assert.equal(await notificationStep(unbound.client, unbound.record, unbound.persist, 300000), false);
  assert.notEqual(unbound.record.status, 'continued');
});

test('a catch-up the registry asks for sends a claimed notice with its own reply, and later labels decide nothing there', async () => {
  const later = {status: 'idle', lastUserMessageAt: '2026-11-21T08:34:05.000Z',
    labels: {'router.dispatch_id': 'later-dispatch', 'router.parent_id': 'parent'}};
  // the watcher claimed the later dispatch with a notice pending
  await withNoticeState(async () => {
    const file = writeRecord('child', pendingRecord({status: 'continued', continued_by: 'later-dispatch', continued: true}));
    const sends = [];
    const client = {fetchAgent: async id => ({agent: id === 'parent' ? {status: 'running'} : later}),
      sendMessage: async (...args) => sends.push(args)};
    const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'});
    assert.deepEqual(answer, {ok: true, sent: true, message_id: idOf('child')});
    assert.deepEqual(sends.map(call => call[1]), ['THE NOTICE']);
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.status, 'delivered');
    assert.equal(saved.continued_by, undefined);
  });
  // the watcher claimed it before composing (superseded, nothing pending): the notice is composed with its own reply
  await withNoticeState(async () => {
    writeRecord('child', {dispatch_id: 'child', parent_id: 'parent', status: 'continued', continued_by: 'later-dispatch',
      delivered: [], seen: true, pending: null, terminal: {reason: SUPERSEDED_TEXT}});
    const sends = [];
    const client = {fetchAgent: async id => ({agent: id === 'parent' ? {status: 'running'} : later}),
      fetchAgentTimeline: async () => ({entries: [{item: {type: 'user_message', text: 'brief'}}, {item: {type: 'assistant_message', text: 'report of child'}}]}),
      sendMessage: async (...args) => sends.push(args)};
    assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'})).sent, true);
    assert.ok(sends[0][1].includes(`${NOT_LANDED}Final reply:\nreport of child\n`), sends[0][1]);
  });
  // a pending notice with no claim and a late observer: the labels are not read at the catch-up
  await withNoticeState(async () => {
    writeRecord('child', pendingRecord({created_at: 0}));
    const sends = [];
    const client = {fetchAgent: async id => ({agent: id === 'parent' ? {status: 'running'} : later}),
      sendMessage: async (...args) => sends.push(args)};
    assert.equal((await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent'})).sent, true);
    assert.equal(sends.length, 1);
  });
});

test('a catch-up for a worker Paseo no longer lists says it is unavailable and ends sent', async () => {
  await withNoticeState(async () => {
    // the watcher died before it saw anything: no terminal, nothing pending
    const file = writeRecord('child', {dispatch_id: 'child', parent_id: 'parent', agent_id: 'worker', status: 'watching',
      delivered: [], pending: null, seen: true});
    const sends = [];
    const client = {fetchAgent: async id => ({agent: id === 'parent' ? {status: 'running'} : null}),
      fetchAgentTimeline: async () => ({entries: []}), sendMessage: async (...args) => sends.push(args)};
    const answer = await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent', ended_as: 'canceled'});
    assert.equal(answer.sent, true);
    assert.match(sends[0][1], /State: worker unavailable\n/);
    assert.match(sends[0][1], /Paseo no longer lists this worker \(it was removed or archived\); it left no final reply\./);
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).status, 'delivered');
  });
  await withNoticeState(async () => {
    writeRecord('child', {dispatch_id: 'child', parent_id: 'parent', agent_id: 'worker', status: 'watching', delivered: [], pending: null});
    const sends = [];
    const client = {fetchAgent: async id => { if (id === 'parent') return {agent: {status: 'running'}}; throw new Error('timeout'); },
      fetchAgentTimeline: async () => ({entries: []}), sendMessage: async (...args) => sends.push(args)};
    await notifyEnd(client, {dispatch_id: 'child', parent_id: 'parent', ended_as: 'canceled'});
    assert.match(sends[0][1], /State: recorded as canceled; the notification watch did not see the end\n/, 'an unread worker says nothing');
  });
});

test('a reply followed only by the late close of a call the turn started before it is a reply', () => {
  const user = {item: {type: 'user_message', text: 'brief'}};
  const reply = {item: {type: 'assistant_message', text: 'report'}};
  const call = (id, status) => ({item: {type: 'tool_call', callId: id, status}});
  assert.equal(endedOnReply([user, call('bg', 'running'), reply, call('bg', 'canceled')]), true);
  assert.equal(endedOnReply([user, reply, call('new', 'canceled')]), false, 'a call started after the reply');
  assert.equal(endedOnReply([user, call('bg', 'running'), reply, call('bg', 'canceled'), call('new', 'running')]), false);
  assert.equal(endedOnReply([user, call('bg', 'running'), {item: {type: 'error', message: 'x'}}, reply, call('bg', 'canceled')]), false);
  assert.equal(endedOnReply([call('bg', 'running'), user, reply, call('bg', 'canceled')]), false, 'started in an earlier turn');
});

test("a worker observed late whose turn ended on its reply records replied, reading back to the closed call's start", async () => {
  const f = watchFixture();
  const turn = [
    {type: 'user_message', text: 'brief'}, {type: 'tool_call', callId: 'bg', status: 'running'},
    {type: 'reasoning', text: 'r'}, {type: 'tool_call', callId: 'other', status: 'completed'},
    {type: 'assistant_message', text: 'the report'}, {type: 'tool_call', callId: 'bg', status: 'canceled'},
  ];
  f.client.fetchAgentTimeline = pagedTimeline(turn);
  assert.equal(await notificationStep(f.client, f.record, f.persist, 10), true);
  assert.equal(f.record.terminal.reason, 'finished');
  assert.equal(f.record.terminal.replied, true);
  assert.match(f.calls[0][1], /Final reply:\nthe report/);
});
