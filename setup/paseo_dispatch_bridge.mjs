// Narrow local SDK adapter. stdin JSON only; stdout contains metadata, never prompts or credentials.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { setTimeout as pause } from 'node:timers/promises';

const WATCH_LIMIT_MS = 7 * 24 * 60 * 60 * 1000;
const CREATE_LIMIT_MS = 2 * 60 * 1000;
const POLL_MS = 5000;
const DONE = new Set(['delivered', 'parent_unavailable', 'delivery_failed', 'creation_failed']);

export function notificationFile(dispatchId) {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(dispatchId)) throw new Error('invalid_dispatch_id');
  return path.join(process.env.ROUTER_STATE_DIR ?? path.join(os.homedir(), '.route-manager'),
    'paseo-notifications', `${dispatchId}.json`);
}

function readRecord(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function saveRecord(file, record) {
  const temp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(record), {mode: 0o600});
  fs.renameSync(temp, file);
}

function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
}

export function notificationReceipt(file) {
  const record = readRecord(file);
  if (!record) return {status: 'unavailable', state_file: file};
  return {status: !DONE.has(record.status) && !alive(record.pid) ? 'stopped' : record.status,
    pid: record.pid, state_file: file, deadline: record.deadline};
}

// A single persisted record and exclusive process lock own each dispatch. The
// child outlives roll, but always exits at its deadline or after final delivery.
export async function ensureNotification(input) {
  const file = notificationFile(input.dispatch_id);
  fs.mkdirSync(path.dirname(file), {recursive: true});
  const existing = readRecord(file);
  if (existing && existing.parent_id !== input.parent_id) throw new Error('notification_parent_mismatch');
  if (existing && (DONE.has(existing.status) || alive(existing.pid))) return notificationReceipt(file);
  const child = spawn(process.execPath, [fileURLToPath(import.meta.url), '--watch',
    input.dispatch_id, input.parent_id], {detached: true, windowsHide: true,
    stdio: ['ignore', 'ignore', 'ignore', 'ipc'], env: process.env});
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('notification_start_timeout')), 10000);
      child.once('error', error => {clearTimeout(timer); reject(error);});
      child.once('exit', () => {clearTimeout(timer); reject(new Error('notification_start_failed'));});
      child.once('message', value => {
        clearTimeout(timer);
        if (value.ready) resolve(); else reject(new Error('notification_start_failed'));
      });
    });
  } catch (error) {
    child.kill();
    throw error;
  } finally {
    if (child.connected) child.disconnect();
    child.unref();
  }
  // A concurrent starter can acknowledge its existing lock before the owner
  // finishes loading dependencies. Only a published ready record is sufficient.
  for (let attempt = 0; attempt < 100; attempt++) {
    const receipt = notificationReceipt(file);
    if (receipt.status === 'watching' || DONE.has(receipt.status)) return receipt;
    await pause(100);
  }
  throw new Error('notification_not_ready');
}

export function notificationEvents(agent, record, now) {
  if (!agent) return record.seen || now >= record.creation_deadline
    ? [{key: 'terminal', reason: record.seen ? 'worker unavailable' : 'creation outcome unavailable'}] : [];
  if (agent.status === 'running') record.started = true;
  record.seen = true;
  if (agent.status === 'error' || agent.attentionReason === 'error' || agent.providerUnavailable)
    return [{key: 'terminal', reason: 'error'}];
  if (agent.status === 'closed' || agent.archivedAt) return [{key: 'terminal', reason: 'closed'}];
  if (agent.pendingPermissions?.length) return agent.pendingPermissions.map(p =>
    ({key: `permission:${p.id}`, reason: 'needs permission'}));
  if (agent.attentionReason === 'permission') return [{key: 'permission', reason: 'needs permission'}];
  if (agent.status === 'idle' && (agent.attentionReason === 'finished' || record.started))
    return [{key: 'terminal', reason: 'finished'}];
  return [];
}

// This small step is also used by tests with an SDK fake. Persist the pending
// message before sending: a lost acknowledgment retries the same messageId.
export async function notificationStep(client, record, persist, now = Date.now()) {
  if (DONE.has(record.status)) return true;
  const parent = (await client.fetchAgent(record.parent_id))?.agent;
  if (!parent || parent.archivedAt || parent.status === 'closed') {
    record.status = 'parent_unavailable'; persist(); return true;
  }
  if (!record.pending) {
    const agent = (await client.fetchAgent(record.dispatch_id))?.agent;
    const events = now >= record.deadline
      ? [{key: 'terminal', reason: 'notification watch expired; check worker status'}]
      : notificationEvents(agent, record, now);
    const event = events.find(event => !record.delivered.includes(event.key));
    if (event) {
      record.pending = {...event, messageId: createHash('sha256')
        .update(`router-finish:${record.dispatch_id}:${event.key}`).digest('hex'),
      text: `[Route Manager worker notification]\nDispatch: ${record.dispatch_id}\n` +
        `Worker: ${record.dispatch_id}\nState: ${event.reason}\n` +
        `Read the worker's final response and details with get_agent_activity ` +
        `(agentId: ${record.dispatch_id}); use get_agent_status for pending permissions.`};
    }
    persist();
  }
  if (!record.pending) return false;
  await client.sendMessage(record.parent_id, record.pending.text,
    {messageId: record.pending.messageId, activeTurnBehavior: 'steer'});
  const terminal = record.pending.key === 'terminal';
  record.delivered.push(record.pending.key);
  record.pending = null;
  record.status = terminal ? 'delivered' : 'watching';
  persist();
  return terminal;
}

async function watch(dispatchId, parentId) {
  const file = notificationFile(dispatchId), lock = `${file}.lock`;
  fs.mkdirSync(path.dirname(file), {recursive: true});
  let lockFd;
  try { lockFd = fs.openSync(lock, 'wx'); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    // Serialize stale-lock reclamation too: two concurrent observers must not
    // unlink a new watcher's lock after both saw the same dead predecessor.
    const recovery = `${lock}.recovery`;
    try { fs.mkdirSync(recovery); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      process.send?.({ready: false}); return;
    }
    try {
      const owner = fs.existsSync(lock) ? Number(fs.readFileSync(lock, 'utf8')) : null;
      if (owner === 0 || alive(owner)) {process.send?.({ready: Boolean(owner)}); return;}
      if (fs.existsSync(lock)) fs.unlinkSync(lock);
      lockFd = fs.openSync(lock, 'wx');
      fs.writeFileSync(lockFd, String(process.pid));
    } finally { fs.rmdirSync(recovery); }
  }
  fs.writeFileSync(lockFd, String(process.pid));
  let client;
  try {
    const record = readRecord(file) ?? {dispatch_id: dispatchId, parent_id: parentId,
      deadline: Date.now() + WATCH_LIMIT_MS, creation_deadline: Date.now() + CREATE_LIMIT_MS,
      status: 'watching', delivered: [], pending: null};
    if (record.parent_id !== parentId) throw new Error('notification_parent_mismatch');
    if (DONE.has(record.status)) {process.send?.({ready: true}); return;}
    record.pid = process.pid;
    const persist = () => saveRecord(file, record);
    // Resolve dependencies before acknowledging readiness, never launch a worker
    // whose notifier cannot even load the SDK.
    client = await createClient();
    persist();
    process.send?.({ready: true});
    if (process.connected) process.disconnect();
    while (Date.now() < record.deadline + CREATE_LIMIT_MS) {
      // The creator can receive an authoritative failure while we are polling.
      // Stop only this dispatch's watcher, without replacing its receipt.
      if (fs.existsSync(`${file}.creation-failed`)) {
        record.status = 'creation_failed'; persist(); return;
      }
      try {
        if (!client.isConnected) await client.connect();
        if (await notificationStep(client, record, persist)) return;
      } catch {
        // Do not persist SDK errors: they may contain prompts or credentials.
        await client.close().catch(() => {});
        client = await createClient();
      }
      await pause(POLL_MS);
    }
    record.status = 'delivery_failed'; persist();
  } finally {
    if (client) await client.close().catch(() => {});
    fs.closeSync(lockFd);
    fs.unlinkSync(lock);
  }
}

export function project(agent) {
  if (!agent) return null;
  const result = Object.fromEntries(['id', 'provider', 'model', 'status', 'updatedAt', 'createdAt',
    'lastUserMessageAt', 'attentionReason', 'archivedAt', 'providerUnavailable',
    'currentModeId', 'thinkingOptionId', 'effectiveThinkingOptionId', 'labels']
    .filter(k => agent[k] !== undefined).map(k => [k, agent[k]]));
  if (Array.isArray(agent.pendingPermissions)) result.pendingPermissionCount = agent.pendingPermissions.length;
  return result;
}

export async function execute(client, input, notify = ensureNotification) {
  if (input.operation === 'create') {
    const r = input.request;
    if (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id === 'top-level')
      return {ok: false, sent: false, error: 'addressable_parent_required'};
    const parent = await client.fetchAgent(input.parent_id);
    if (!parent?.agent) return {ok: false, sent: false, error: 'parent_agent_not_found'};
    let completion_notification;
    try { completion_notification = await notify(input); }
    catch { return {ok: false, sent: false, error: 'notification_start_failed'}; }
    const cwd = parent.agent.cwd, workspaceId = r.workspaceId ?? parent.agent.workspaceId;
    const slash = r.provider.indexOf('/');
    let creation;
    const options = {
      provider: r.provider.slice(0, slash), model: r.provider.slice(slash + 1),
      title: r.title, cwd, workspaceId,
      callerAgentId: input.parent_id,
      modeId: r.settings?.modeId, thinkingOptionId: r.settings?.thinkingOptionId,
      featureValues: r.settings?.features, initialPrompt: r.initialPrompt, labels: r.labels,
      agentId: input.dispatch_id, idempotencyKey: input.dispatch_id,
      clientMessageId: input.dispatch_id,
      onEvent: snapshot => { creation = snapshot; },
    };
    try {
      return {ok: true, sent: true, agent: project(await client.createAgent(options)), completion_notification};
    } catch {
      if (creation?.kind === 'agent' && creation.idempotencyKey === input.dispatch_id
          && creation.phase === 'failed' && creation.outcomeUnknown === false) {
        // Keep arbitrary SDK error text private; only structured, definitive
        // failure authorizes reservation release. Timeouts remain ambiguous.
        const file = notificationFile(input.dispatch_id);
        const record = readRecord(file);
        if (record?.parent_id === input.parent_id) {
          fs.writeFileSync(`${file}.creation-failed`, 'host_creation_failed', {mode: 0o600});
          saveRecord(file, {...record, status: 'creation_failed'});
        }
        return {ok: false, sent: false, error: 'host_creation_failed',
          completion_notification: {...completion_notification, status: 'creation_failed'}};
      }
      // A timeout can follow successful creation; never report definite failure here.
      return {ok: false, sent: true, error: 'create_outcome_uncertain_reconcile_by_label', completion_notification};
    }
  }
  if (input.operation !== 'observe') throw new Error('unsupported_operation');
  let agent = input.agent_id ? (await client.fetchAgent(input.agent_id))?.agent : null;
  if (!agent) {
    const page = await client.fetchAgents({filter: {labels: {'router.dispatch_id': input.dispatch_id},
      includeArchived: true}, page: {limit: 2}});
    const entries = page.entries ?? [];
    if (entries.length > 1) return {ok: false, error: 'duplicate_dispatch_agents'};
    if (entries.length) {
      const entry = entries[0];
      const id = entry.agent?.id ?? entry.id;
      if (id) agent = (await client.fetchAgent(id))?.agent;
    }
  }
  const file = notificationFile(input.dispatch_id);
  const record = readRecord(file);
  const completion_notification = record ? await notify(record) : notificationReceipt(file);
  return {ok: true, agent: project(agent), missing: Boolean(input.agent_id && !agent), completion_notification};
}

async function createClient() {
  const setup = path.dirname(fileURLToPath(import.meta.url));
  const sdkRoot = process.env.ROUTER_PASEO_SDK_ROOT ?? path.join(setup, 'paseo-bridge/node_modules');
  const require = createRequire(path.join(path.resolve(sdkRoot), '_router_dispatch.cjs'));
  let daemonClientModule, WebSocket;
  try {
    daemonClientModule = require.resolve('@getpaseo/client/internal/daemon-client');
    ({WebSocket} = require('ws'));
  } catch (cause) {
    throw new Error('Paseo bridge dependencies are missing in ' + sdkRoot + '. Run: npm --prefix setup/paseo-bridge ci (from the repository root), or set ROUTER_PASEO_SDK_ROOT.', {cause});
  }
  const {DaemonClient} = await import(pathToFileURL(daemonClientModule).href);
  // Local credential stays inside the adapter. No remote URL/credential forwarding.
  const home = process.env.PASEO_HOME ?? path.join(os.homedir(), '.paseo');
  let listen = '127.0.0.1:6767';
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(home, 'config.json'), 'utf8'));
    if (typeof cfg?.daemon?.listen === 'string' && cfg.daemon.listen.trim()) {
      listen = cfg.daemon.listen.trim();
    }
  } catch {}
  return new DaemonClient({url: `ws://${listen}/ws`, clientId: randomUUID(),
    clientType: 'cli', appVersion: '0.11.2', connectTimeoutMs: 10000,
    localCredential: () => fs.readFileSync(path.join(home, 'local-credential'), 'utf8').trim(),
    webSocketFactory: (url, config) => new WebSocket(url, {headers: config?.headers, protocols: config?.protocols}),
    reconnect: {enabled: false}});
}

async function main() {
  if (process.argv[2] === '--watch') return watch(process.argv[3], process.argv[4]);
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const client = await createClient();
  let connected = false;
  try {
    await client.connect();
    connected = true;
    console.log(JSON.stringify(await execute(client, input)));
  } catch {
    console.log(JSON.stringify({ok: false, sent: connected && input.operation === 'create',
      error: connected ? 'host_request_failed' : 'host_connection_failed'}));
  } finally { await client.close(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    const setupHint = error.message?.startsWith('Paseo bridge dependencies are missing') ? error.message : undefined;
    console.log(JSON.stringify({ok: false, sent: false, error: 'sdk_setup_failed', setupHint}));
    process.exitCode = 1;
  });
}
