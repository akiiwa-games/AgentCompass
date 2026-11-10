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
const CREATE_FAILED = 'creation failed';
const WORKER_REMOVED = 'worker removed before it was observed';
const AGENT_REMOVED_ERROR = 'Previously created agent no longer exists';
const SUPERSEDED = 'superseded by a later dispatch';
const UNDELIVERED = 'turn not delivered';
// dispatch_registry.RUN_LEASE: past it, a resume whose turn has not landed can be released.
const RUN_LEASE_MS = 10 * 60 * 1000;
const WORKSPACE_LOOKUP_MS = 5000;
// A stopped watcher notices its marker within one poll.
const WATCHER_STOP_MS = 2 * POLL_MS;
// A provider that keeps retrying one request is stuck only once nothing but its retry notices has happened in the turn
// for the whole RETRY_STUCK_MS window (and it retried at least RETRY_STUCK_ATTEMPTS times). Fast retries do not shorten
// the window, and one long retry-after cannot end it early: a minute of provider overload must not stop a working
// worker. opencode never gives up by itself (opencode-agent.ts appendOpenCodeSessionStatus), so without this its worker
// stays `running` forever.
const RETRY_STUCK_ATTEMPTS = Number(process.env.AGENTCOMPASS_RETRY_STUCK_ATTEMPTS) || 1;
const RETRY_STUCK_MS = (Number(process.env.AGENTCOMPASS_RETRY_STUCK_SECONDS) || 300) * 1000;
const RETRY_TIMELINE_LIMIT = 40;
const RETRY_MESSAGE_LIMIT = 2000;
const RETRY_STUCK = 'provider retry stuck';

export function notificationFile(dispatchId) {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(dispatchId)) throw new Error('invalid_dispatch_id');
  return path.join(process.env.ROUTER_STATE_DIR ?? path.join(os.homedir(), '.agent-compass'),
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
    pid: record.pid, state_file: file, deadline: record.deadline,
    ...(record.terminal ? {terminal: record.terminal} : {})};
}

// A single persisted record and exclusive process lock own each dispatch. The
// child outlives roll, but always exits at its deadline or after final delivery.
export async function ensureNotification(input) {
  const file = notificationFile(input.dispatch_id);
  fs.mkdirSync(path.dirname(file), {recursive: true});
  const existing = readRecord(file);
  if (existing && existing.parent_id !== input.parent_id) throw new Error('notification_parent_mismatch');
  if (existing && (DONE.has(existing.status) || alive(existing.pid))) return notificationReceipt(file);
  const args = [fileURLToPath(import.meta.url), '--watch', input.dispatch_id, input.parent_id,
    input.agent_id && input.agent_id !== input.dispatch_id ? input.agent_id : '', input.create_deadline ?? ''];
  const child = spawn(process.execPath, args, {detached: true, windowsHide: true,
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

// `creation` is the daemon's durable record for a created worker: an object, null when the daemon never admitted the
// request, undefined when it was not read. Nothing is sent while a create can still succeed, however long it takes.
// No agent means the daemon answered that it has none, in memory or in storage: a host that cannot answer throws.
export function notificationEvents(agent, record, now, creation) {
  if (!agent) {
    if (record.seen || record.agent_id) return [{key: 'terminal', reason: 'worker unavailable'}];
    if ((creation?.phase === 'completed' && !creation.agent_id) || (creation?.phase === 'failed' && creation.agent_created))
      return [{key: 'terminal', reason: WORKER_REMOVED}];
    return creation?.phase === 'failed' || (creation === null && now >= record.creation_deadline)
      ? [{key: 'terminal', reason: CREATE_FAILED}] : [];
  }
  const label = agent.labels?.['router.dispatch_id'];
  // Seen under its own labels, so these are not a relabel that has yet to land: a later dispatch took this worker.
  if (record.seen && label && label !== record.dispatch_id) return [{key: 'terminal', reason: SUPERSEDED}];
  if (record.agent_id && record.agent_id !== record.dispatch_id && label !== record.dispatch_id)
    // Not bound to this dispatch yet, so its state is the previous turn's. Only its removal says anything about this one.
    return agent.archivedAt ? [{key: 'terminal', reason: 'worker unavailable'}] : [];
  if (agent.status === 'running') record.started = true;
  record.seen = true;
  // The watcher stopped a turn its provider kept retrying: the stop's idle or canceled state is not a finish.
  if (record.terminal?.failure_code === RETRY_STUCK_CODE) return [{key: 'terminal', reason: RETRY_STUCK}];
  if (agent.status === 'error' || agent.attentionReason === 'error' || agent.providerUnavailable)
    return [{key: 'terminal', reason: 'error'}];
  if (agent.status === 'closed' || agent.archivedAt) return [{key: 'terminal', reason: 'closed'}];
  if (agent.pendingPermissions?.length) return agent.pendingPermissions.map(p =>
    ({key: `permission:${p.id}`, reason: 'needs permission'}));
  if (agent.attentionReason === 'permission') return [{key: 'permission', reason: 'needs permission'}];
  // A resumed worker is relabeled before its turn is sent: until the turn is seen running or its message lands,
  // idle/finished is still the previous turn's.
  const resumed = record.agent_id && record.agent_id !== record.dispatch_id;
  const created = record.created_at ?? (record.creation_deadline ?? 0) - CREATE_LIMIT_MS;
  const turnSeen = record.started || (typeof agent.lastUserMessageAt === 'string'
    && Date.parse(agent.lastUserMessageAt) >= created);
  if (agent.status === 'idle' && (resumed ? turnSeen : (agent.attentionReason === 'finished' || record.started)))
    return [{key: 'terminal', reason: 'finished'}];
  // Its lease is not renewed from here, so the parent can end it. A turn that lands later still reports its finish.
  if (resumed && !turnSeen && now - created >= RUN_LEASE_MS) return [{key: 'undelivered', reason: UNDELIVERED}];
  return [];
}

// Paseo renders a provider's own retry notice (opencode `session.status` retry, pi `auto_retry_start`) as a timeline
// `error` item in this one fixed template of its own (opencode-agent.ts, pi/agent.ts), and omp's as a status-line tool
// call with structured metadata. Reading Paseo's template recovers the attempt and the provider's message verbatim; what
// the message means is decided by the router's classifier (provider_retry.py), never here.
const RETRY_TEMPLATE = /^Provider retry \(attempt (\d+)\)(?:: ([\s\S]*))?$/;
export const RETRY_STUCK_CODE = 'provider_retry_stuck';
// A worker whose turn ended in error carries the provider's own error text (agent.lastError, from the provider's
// turn_failed). opencode 2.x fails a free model at once this way ("Free usage limit reached. ..."); the router's classifier
// decides whether it blocks the model, the account, or nothing (provider_retry.py).
export const TURN_FAILED_CODE = 'provider_turn_failed';

// Paseo clears lastError when a turn starts, so it is this dispatch's unless this is a resumed worker whose turn has not
// started yet: then the error is the previous turn's, and says nothing about this one.
export function turnIsCurrent(agent, record) {
  if (!record?.agent_id || record.agent_id === record.dispatch_id) return true;
  const created = record.created_at ?? (record.creation_deadline ?? 0) - CREATE_LIMIT_MS;
  return Boolean(record.started || (typeof agent?.lastUserMessageAt === 'string'
    && Date.parse(agent.lastUserMessageAt) >= created));
}

export function turnFailure(agent, record) {
  if (record && !turnIsCurrent(agent, record)) return {};
  const message = typeof agent?.lastError === 'string' ? agent.lastError.trim() : '';
  return message ? {failure_code: TURN_FAILED_CODE, provider_message: message.slice(0, RETRY_MESSAGE_LIMIT)} : {};
}

// Whether a finished turn ended on the model's own reply: its last item is an assistant message and nothing back to the
// turn's prompt is an error (a provider retry is one). Paseo's idle/finished state is the same after a cancel and its
// timeline marks no cancel, so this is the only sign the provider served the turn; only it lets the router lift a
// provider hold. A turn longer than the page counts when the page holds no error.
export function endedOnReply(entries) {
  const list = entries ?? [];
  if (list[list.length - 1]?.item?.type !== 'assistant_message') return false;
  for (let i = list.length - 1; i >= 0; i--) {
    const type = list[i]?.item?.type;
    if (type === 'user_message') return true;
    if (type === 'error') return false;
  }
  return true;
}

async function turnReplied(client, agentId) {
  try {
    const page = await client.fetchAgentTimeline(agentId, {direction: 'tail', limit: RETRY_TIMELINE_LIMIT, projection: 'canonical'});
    return endedOnReply(page?.entries);
  } catch { return false; }
}

export function providerRetry(entry) {
  const item = entry?.item, at = Date.parse(entry?.timestamp ?? '');
  if (!item || !Number.isFinite(at)) return null;
  if (item.type === 'error' && typeof item.message === 'string') {
    const found = RETRY_TEMPLATE.exec(item.message);
    return found ? {attempt: Number(found[1]), message: found[2] ?? null, next: null, at} : null;
  }
  const meta = item.metadata;
  if (item.type === 'tool_call' && meta?.source === 'omp_auto_retry_start' && Number.isInteger(meta.attempt)) {
    const delay = Number(meta.delayMs);
    return {attempt: meta.attempt, message: typeof item.detail?.text === 'string' ? item.detail.text : null,
      next: Number.isFinite(delay) && delay >= 0 ? new Date(at + delay).toISOString() : null, at};
  }
  return null;
}

// The retries at the end of the turn's timeline, newest first, back to the first item that is anything else. Any such
// item (text, reasoning, a tool call, the turn's prompt) is progress, or the start of this turn.
export function retryStreak(entries) {
  const streak = [];
  for (let i = (entries ?? []).length - 1; i >= 0; i--) {
    const retry = providerRetry(entries[i]);
    if (!retry) break;
    streak.push(retry);
  }
  return streak.length ? {newest: streak[0], oldest: streak[streak.length - 1], count: streak.length} : null;
}

export function retryIsStuck(streak, now) {
  if (!streak) return false;
  return now - streak.oldest.at >= RETRY_STUCK_MS && Math.max(streak.newest.attempt, streak.count) >= RETRY_STUCK_ATTEMPTS;
}

// Whether the turn did anything before it started retrying: back from the retries to the turn's prompt, any other item
// is work. A page that ends before the prompt with older items beyond it is taken as having worked.
export function workedBeforeRetrying(entries, streak, hasOlder) {
  for (let i = (entries ?? []).length - (streak?.count ?? 0) - 1; i >= 0; i--) {
    if (entries[i]?.item?.type === 'user_message') return false;
    if (entries[i]?.item) return true;
  }
  return Boolean(hasOlder);
}

// The terminal record of a worker stuck in provider retry, or null. A timeline that cannot be read says nothing.
export async function stuckProviderRetry(client, agentId, now) {
  let page;
  try {
    page = await client.fetchAgentTimeline(agentId, {direction: 'tail', limit: RETRY_TIMELINE_LIMIT, projection: 'canonical'});
  } catch { return null; }
  const streak = retryStreak(page?.entries);
  if (!retryIsStuck(streak, now)) return null;
  const message = streak.newest.message ?? streak.oldest.message;
  return {reason: RETRY_STUCK, failure_code: RETRY_STUCK_CODE,
    partial_work: workedBeforeRetrying(page.entries, streak, page.hasOlder),
    provider_message: typeof message === 'string' ? message.slice(0, RETRY_MESSAGE_LIMIT) : null,
    next: streak.newest.next, attempts: Math.max(streak.newest.attempt, streak.count),
    retrying_since: new Date(streak.oldest.at).toISOString(), at: new Date(now).toISOString()};
}

export function retryStuckText(terminal, targetId) {
  const head = `Its provider kept retrying one request without progress (${terminal?.attempts ?? '?'} attempts since ` +
    `${terminal?.retrying_since ?? '?'}). Provider message: ${terminal?.provider_message ?? '(none)'}\n` +
    'AgentCompass holds that model (or its whole account) out of dispatch.\n';
  if (terminal?.cancelled === false)
    return head + `Stopping the worker failed, so it may still be running and may resume once the provider recovers: ` +
      `stop it with cancel_agent (agentId: ${targetId}) and check what it did before rolling this brief again.`;
  if (terminal?.partial_work)
    return head + `AgentCompass stopped the turn. The worker had already done part of the work (it is stopped, not ` +
      `removed): check its activity (get_agent_activity, agentId: ${targetId}) and its worktree before rolling this ` +
      'brief again; the router will pick another model.';
  return head + 'AgentCompass stopped the turn before the worker did any work; roll this brief again and the router ' +
    'picks another model.';
}

// This small step is also used by tests with an SDK fake. Persist the pending
// message before sending: a lost acknowledgment retries the same messageId.
export async function notificationStep(client, record, persist, now = Date.now()) {
  if (DONE.has(record.status)) return true;
  let event;
  const targetId = record.agent_id ?? record.dispatch_id;
  if (!record.pending) {
    const expired = now >= record.deadline;
    // Past the watch there is nothing left to decide from these, and one that cannot be read must not hold back the notice.
    let agent = expired
      ? await client.fetchAgent(targetId).then(found => found?.agent, () => null)
      : (await client.fetchAgent(targetId))?.agent;
    const creation = !expired && !agent && !record.seen && !record.agent_id
      ? await creationRecord(client, record.dispatch_id) : undefined;
    // Read the record, then the agent it names: a create that got this far between the two reads is not "removed".
    if (creation?.agent_id) agent = (await client.fetchAgent(creation.agent_id))?.agent;
    const events = expired
      ? [{key: 'terminal', reason: 'notification watch expired; check worker status'}]
      : notificationEvents(agent, record, now, creation);
    // A running worker bound to this dispatch whose provider keeps retrying without progress is stopped here: the
    // terminal is saved before the cancel, so the stop's idle state can never read as a finish.
    const bound = agent && (agent.labels?.['router.dispatch_id'] === record.dispatch_id
      || !record.agent_id || record.agent_id === record.dispatch_id);
    if (!events.length && !record.terminal && bound && agent.status === 'running') {
      const stuck = await stuckProviderRetry(client, targetId, now);
      if (stuck) {
        record.terminal = {...stuck, status: agent.status, attentionReason: agent.attentionReason ?? null};
        persist();
        // Retried once at once; a cancel that still fails is said to the parent, never reported as stopped.
        record.terminal.cancelled = await client.cancelAgent(targetId).then(() => true,
          () => client.cancelAgent(targetId).then(() => true, () => false));
        events.push({key: 'terminal', reason: RETRY_STUCK});
      }
    }
    event = events.find(event => !record.delivered.includes(event.key));
    // The first terminal sighting is the outcome. The registry reads it, because Paseo later closes an
    // idle finished worker or fails it on a plugin reload, and a late observation would see only that.
    if (event?.key === 'terminal' && !record.terminal)
      record.terminal = {reason: event.reason, status: agent?.status ?? null,
        attentionReason: agent?.attentionReason ?? null, at: new Date(now).toISOString(),
        ...(event.reason === 'error' ? turnFailure(agent, record) : {}),
        ...(event.reason === 'finished' ? {replied: await turnReplied(client, targetId)} : {})};
  }
  const parent = (await client.fetchAgent(record.parent_id))?.agent;
  if (!parent || parent.archivedAt || parent.status === 'closed') {
    record.status = 'parent_unavailable'; persist(); return true;
  }
  if (!record.pending) {
    if (event) {
      record.pending = {...event, messageId: createHash('sha256')
        .update(`router-finish:${record.dispatch_id}:${event.key}`).digest('hex'),
      text: `[AgentCompass worker notification]\nDispatch: ${record.dispatch_id}\n` +
        `Worker: ${targetId}\nState: ${event.reason}\n` + (event.reason === CREATE_FAILED
          ? 'Paseo created no worker for this dispatch and none can appear later; it is safe to roll this brief again.'
          : event.reason === WORKER_REMOVED
          ? 'Paseo created this worker and it was removed before this dispatch observed it. It may have done part of the work: check that before rolling this brief again.'
          : event.reason === RETRY_STUCK ? retryStuckText(record.terminal, targetId)
          : event.reason === UNDELIVERED
          ? `This worker carries this dispatch's labels, but its turn has not landed within the lease. Check it with get_agent_status ` +
            `(agentId: ${targetId}). If it should not do this work any more, release this dispatch. A finish is still reported if the turn lands.`
          : (record.terminal?.failure_code === TURN_FAILED_CODE && event.key === 'terminal'
            ? `The worker's turn ended with this error: ${record.terminal.provider_message}\nAgentCompass's classifier ` +
              'reads it: when it is the provider refusing the model or the account, that is held out of dispatch; an ' +
              "error of the worker's own work or of this task's input holds nothing. " : '') +
            `Read the worker's final response and details with get_agent_activity ` +
            `(agentId: ${targetId}); use get_agent_status for pending permissions.`)};
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

async function watch(dispatchId, parentId, agentId, createDeadline) {
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
      ...(agentId && agentId !== dispatchId ? {agent_id: agentId} : {}),
      deadline: Date.now() + WATCH_LIMIT_MS, created_at: Date.now(),
      creation_deadline: Date.parse(createDeadline) || Date.now() + CREATE_LIMIT_MS,
      status: 'watching', delivered: [], pending: null};
    if (agentId && agentId !== dispatchId && !record.agent_id) record.agent_id = agentId;
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

// A fixed vocabulary for a definitive create failure. The SDK error text stays private; the class tells the
// router whether the whole account (its provider never came up) or only this model failed.
// A provider's own fields decide; the text is only for a report without them. Text never leaves the bridge.
const INACTIVE_CODES = new Set(['subscription_inactive', 'subscription_expired', 'subscription_canceled',
  'subscription_cancelled', 'plan_expired', 'plan_inactive', 'payment_required', 'payment_failed']);
const INACTIVE_TEXT = /subscription(?:_| (?:has |is |was )?)(?:expired|cancel+ed|inactive|lapsed)|plan (?:has |is )?(?:expired|cancel+ed|inactive)|payment[_ ](?:required|failed)|past[_ ]due|订阅(?:已)?(?:过期|到期|失效|取消)/i;

export function subscriptionLapsed(code, status, text) {
  if (code || status) return status === 402 || INACTIVE_CODES.has(String(code ?? '').trim().toLowerCase());
  return typeof text === 'string' && INACTIVE_TEXT.test(text);
}

export function creationFailureClass(creation) {
  const text = typeof creation?.error === 'string' ? creation.error : '';
  if (subscriptionLapsed(creation?.errorCode, creation?.statusCode, text)) return 'subscription_inactive';
  if (/timed out refreshing|refresh(?:ing)? timed out/i.test(text)) return 'provider_refresh_timeout';
  if (/unavailable provider|provider (?:is )?unavailable|not (?:installed|signed in)|sign in/i.test(text)) return 'provider_unavailable';
  if (/quota|usage limit|rate.?limit/i.test(text)) return 'quota';
  if (/model/i.test(text) && /unknown|not found|unsupported|invalid/i.test(text)) return 'model_unavailable';
  return 'unknown';
}

// The provider's own error code as the daemon recorded it (a plugin's ProviderActionError code, e.g. INVALID_MODEL).
// An identifier, not text: anything else stays private. The router decides what a code means.
export function creationFailureCode(creation) {
  const code = creation?.errorCode;
  return typeof code === 'string' && /^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/.test(code) ? {failure_code: code} : {};
}

// The daemon owns a create across socket lifetimes and keeps its outcome under the idempotency key, so the answer the
// creating bridge was killed before receiving can still be read. Only a fixed vocabulary leaves this adapter.
export async function creationRecord(client, key) {
  const response = await client.sendCorrelatedSessionRequest({
    message: {type: 'creation.subscribe.request', kind: 'agent', idempotencyKey: key, subscribe: false},
    responseType: 'creation.subscribe.response', timeout: 10000});
  // The daemon reports a completed record whose agent is gone only as this error, never as phase `completed`.
  if (response.error === AGENT_REMOVED_ERROR) return {phase: 'completed', outcome_unknown: false, agent_id: null, agent_created: true};
  if (response.error) throw new Error('creation_unobservable');
  const snapshot = response.snapshot;
  if (!snapshot) return null;
  return {phase: snapshot.phase, outcome_unknown: Boolean(snapshot.outcomeUnknown), agent_id: snapshot.agentId ?? null,
    agent_created: Boolean(snapshot.agent),
    ...(snapshot.phase === 'failed' ? {failure_class: creationFailureClass(snapshot), ...creationFailureCode(snapshot)} : {})};
}

async function findWorkspaceDirectory(client, workspaceId) {
  let cursor;
  do {
    const page = await client.fetchWorkspaces({page: {limit: 200, ...(cursor ? {cursor} : {})}});
    const found = page.entries?.find(workspace => workspace.id === workspaceId);
    if (found) return found.workspaceDirectory ?? found.projectRootPath ?? null;
    cursor = page.pageInfo?.hasMore ? page.pageInfo.nextCursor : null;
  } while (cursor);
  return null;
}

// Only feeds a warning, so failing here must not cost the caller the agents it did read. Nor must hanging: the SDK's
// own limit is longer than the caller's, which kills this bridge and loses them.
async function workspaceDirectory(client, workspaceId) {
  let timer;
  const limit = new Promise(resolve => { timer = setTimeout(resolve, WORKSPACE_LOOKUP_MS, null); });
  try { return await Promise.race([findWorkspaceDirectory(client, workspaceId), limit]); }
  catch { return null; }
  finally { clearTimeout(timer); }
}

// The creator knows the outcome: stop only this dispatch's watcher, without replacing its receipt.
function stopWatcher(input, reason) {
  const file = notificationFile(input.dispatch_id);
  const record = readRecord(file);
  if (record?.parent_id !== input.parent_id) return;
  fs.writeFileSync(`${file}.creation-failed`, reason, {mode: 0o600});
  saveRecord(file, {...record, status: 'creation_failed'});
}

// A refused resume's stopped watcher followed the old worker. The create falling back from it needs a watcher of its
// own on the new one, and the old watcher must be gone first or its last save would overwrite the new record.
async function retireRefusedResume(input) {
  const file = notificationFile(input.dispatch_id);
  const record = readRecord(file);
  if (record?.parent_id !== input.parent_id || record.status !== 'creation_failed' || !record.agent_id) return;
  for (let waited = 0; alive(record.pid); waited += 100) {
    if (waited >= WATCHER_STOP_MS) throw new Error('notification_start_failed');
    await pause(100);
  }
  fs.rmSync(`${file}.creation-failed`, {force: true});
  fs.rmSync(file, {force: true});
}

export function project(agent) {
  if (!agent) return null;
  const result = Object.fromEntries(['id', 'provider', 'model', 'status', 'cwd', 'updatedAt', 'createdAt',
    'lastUserMessageAt', 'attentionReason', 'archivedAt', 'providerUnavailable',
    'currentModeId', 'thinkingOptionId', 'effectiveThinkingOptionId', 'labels']
    .filter(k => agent[k] !== undefined).map(k => [k, agent[k]]));
  if (Array.isArray(agent.pendingPermissions)) result.pendingPermissionCount = agent.pendingPermissions.length;
  if (subscriptionLapsed(agent.lastErrorCode, agent.lastErrorStatus, agent.lastError)) result.lastErrorClass = 'subscription_inactive';
  return result;
}

export function normalizeProviderModels(models) {
  if (!Array.isArray(models)) return [];
  const out = [];
  for (const item of models) {
    if (!item || typeof item !== 'object') continue;
    const id = typeof item.id === 'string' ? item.id.trim() : '';
    if (!id) continue;
    const aliases = Array.isArray(item.aliases)
      ? item.aliases.filter(a => typeof a === 'string' && a.trim()).map(a => a.trim())
      : [];
    const label = typeof item.label === 'string' && item.label.trim() ? item.label.trim() : undefined;
    out.push({id, ...(aliases.length ? {aliases} : {}), ...(label ? {label} : {})});
  }
  return out;
}

export async function execute(client, input, notify = ensureNotification) {
  if (input.operation === 'create') {
    const r = input.request;
    if (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id === 'top-level')
      return {ok: false, sent: false, error: 'addressable_parent_required'};
    const parent = await client.fetchAgent(input.parent_id);
    if (!parent?.agent) return {ok: false, sent: false, error: 'parent_agent_not_found'};
    let completion_notification;
    try {
      await retireRefusedResume(input);
      completion_notification = await notify(input);
    } catch { return {ok: false, sent: false, error: 'notification_start_failed'}; }
    const cwd = parent.agent.cwd, workspaceId = r.workspaceId ?? parent.agent.workspaceId;
    const slash = r.provider.indexOf('/');
    // The caller stops waiting for a create it never sent and may settle it as failed, so sending later is forbidden.
    if (input.send_by && Date.now() > Date.parse(input.send_by)) {
      stopWatcher(input, 'create_send_window_elapsed');
      return {ok: false, sent: false, error: 'create_send_window_elapsed',
        completion_notification: {...completion_notification, status: 'creation_failed'}};
    }
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
        stopWatcher(input, 'host_creation_failed');
        return {ok: false, sent: false, error: 'host_creation_failed', failure_class: creationFailureClass(creation),
          ...creationFailureCode(creation), completion_notification: {...completion_notification, status: 'creation_failed'}};
      }
      // A timeout can follow successful creation; never report definite failure here.
      return {ok: false, sent: true, error: 'create_outcome_uncertain_reconcile_by_label', completion_notification};
    }
  }
  if (input.operation === 'resume') {
    const r = input.request;
    if (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id === 'top-level')
      return {ok: false, sent: false, error: 'addressable_parent_required'};
    if (typeof input.agent_id !== 'string' || !input.agent_id.trim())
      return {ok: false, sent: false, error: 'resume_agent_required'};
    const parent = await client.fetchAgent(input.parent_id);
    if (!parent?.agent) return {ok: false, sent: false, error: 'parent_agent_not_found'};
    const existing = (await client.fetchAgent(input.agent_id))?.agent;
    if (!existing || existing.archivedAt)
      return {ok: false, sent: false, error: 'worker_not_found'};
    const labels = existing.labels ?? {};
    const owner = labels['router.parent_id'] ?? labels['paseo.parent-agent-id'];
    if (!owner || owner !== input.parent_id)
      return {ok: false, sent: false, error: 'worker_parent_mismatch'};
    const slash = r.provider.indexOf('/');
    const provider = r.provider.slice(0, slash), model = r.provider.slice(slash + 1);
    if (existing.provider !== provider || existing.model !== model)
      return {ok: false, sent: false, error: 'worker_model_mismatch'};
    const effort = existing.effectiveThinkingOptionId ?? existing.thinkingOptionId;
    if ((r.settings?.modeId && existing.currentModeId && existing.currentModeId !== r.settings.modeId)
        || (r.settings?.thinkingOptionId && effort && effort !== r.settings.thinkingOptionId))
      return {ok: false, sent: false, error: 'worker_settings_mismatch'};
    // The writer rule checked the workspace that was asked for, so the work must land there.
    if (r.workspaceId && existing.workspaceId !== r.workspaceId)
      return {ok: false, sent: false, error: 'worker_workspace_mismatch'};
    const alreadyBound = labels['router.dispatch_id'] === input.dispatch_id;
    if (!alreadyBound && (existing.status === 'running' || existing.status === 'initializing'
        || existing.status === 'error' || existing.attentionReason === 'error'
        || existing.providerUnavailable || existing.pendingPermissions?.length))
      return {ok: false, sent: false, error: 'worker_not_idle'};
    let completion_notification;
    try { completion_notification = await notify({...input, agent_id: input.agent_id}); }
    catch { return {ok: false, sent: false, error: 'notification_start_failed'}; }
    if (alreadyBound)
      return {ok: true, sent: true, agent: project(existing), completion_notification};
    let sent = false, relabeled = false;
    const mergedLabels = {...labels, ...(r.labels ?? {}), 'paseo.parent-agent-id': input.parent_id};
    try {
      // Bind first, then send. A send cut off by the caller's timeout leaves an agent already carrying this
      // dispatch's labels, so reconcile can observe it; sending first left it bound to the previous dispatch.
      await client.updateAgent(input.agent_id, {name: r.title, labels: mergedLabels});
      relabeled = true;
      await client.sendMessage(input.agent_id, r.initialPrompt, {messageId: input.dispatch_id});
      sent = true;
      const fetched = (await client.fetchAgent(input.agent_id))?.agent;
      const nowIso = new Date().toISOString();
      const fresh = fetched
        ? {...fetched, status: fetched.status === 'idle' ? 'running' : fetched.status,
           attentionReason: fetched.status === 'idle' ? undefined : fetched.attentionReason,
           updatedAt: fetched.updatedAt && fetched.updatedAt >= nowIso ? fetched.updatedAt : nowIso,
           labels: {...(fetched.labels ?? {}), ...mergedLabels}}
        : {...existing, status: 'running', attentionReason: undefined, updatedAt: nowIso, labels: mergedLabels};
      return {ok: true, sent: true, agent: project(fresh), completion_notification};
    } catch {
      if (!sent) {
        // A definite send failure falls back to a fresh create with the same dispatch labels: give the old
        // worker its previous labels back so only one agent carries this dispatch.
        if (relabeled) {
          try { await client.updateAgent(input.agent_id, {name: existing.title, labels}); }
          catch { return {ok: false, sent: true, error: 'resume_outcome_uncertain_reconcile_by_agent', completion_notification}; }
        }
        stopWatcher(input, 'host_resume_failed');
        return {ok: false, sent: false, error: 'host_resume_failed',
          completion_notification: {...completion_notification, status: 'creation_failed'}};
      }
      return {ok: false, sent: true, error: 'resume_outcome_uncertain_reconcile_by_agent', completion_notification};
    }
  }
  if (input.operation === 'workspace_agents') {
    const agents = [];
    let cursor;
    do {
      const page = await client.fetchAgents({filter: {statuses: ['initializing', 'running']},
        page: {limit: 200, ...(cursor ? {cursor} : {})}});
      for (const entry of page.entries ?? []) {
        const agent = entry.agent ?? entry;
        if (agent.workspaceId === input.workspace_id && agent.labels?.['router.dispatch_id'])
          agents.push({id: agent.id, status: agent.status, dispatch_id: agent.labels['router.dispatch_id']});
      }
      cursor = page.pageInfo?.hasMore ? page.pageInfo.nextCursor : null;
    } while (cursor);
    return {ok: true, agents, cwd: await workspaceDirectory(client, input.workspace_id)};
  }
  if (input.operation === 'provider_models') {
    const cwd = typeof input.cwd === 'string' && input.cwd.trim() ? input.cwd.trim() : undefined;
    const opts = cwd ? {cwd} : {};
    // Paseo's own provider snapshot decides which providers are Paseo's: one it does not list is `unknown_provider`
    // (not a Paseo provider, e.g. a LiteLLM proxy account), which callers neither warn about nor exclude on.
    const snapByProvider = new Map();
    let snapshotRead = false;
    if (typeof client.getProvidersSnapshot === 'function') {
      try {
        const snap = await client.getProvidersSnapshot(opts);
        const list = snap?.entries ?? snap?.providers;
        if (Array.isArray(list)) {
          snapshotRead = true;
          for (const p of list) {
            if (p && typeof p.provider === 'string' && p.provider.trim()) snapByProvider.set(p.provider.trim(), p);
          }
        }
      } catch {}
    }
    const requested = Array.isArray(input.providers)
      ? [...new Set(input.providers.filter(p => typeof p === 'string' && p.trim()).map(p => p.trim()))]
      : [...snapByProvider.keys()];
    const entries = await Promise.all(requested.map(async provider => {
      const snap = snapByProvider.get(provider);
      if (snapshotRead && !snap)
        return [provider, {ok: false, status: 'unknown_provider', error: 'unknown_provider', models: []}];
      if (snap && snap.enabled === false)
        return [provider, {ok: false, status: 'disabled', error: 'provider_disabled', models: []}];
      try {
        const res = await client.listProviderModels(provider, opts);
        if (!Array.isArray(res?.models) || (typeof res.error === 'string' && res.error.trim()))
          return [provider, {ok: false, status: snap?.status && snap.status !== 'ready' ? snap.status : 'unavailable',
            error: 'model_list_unavailable', models: []}];
        if (snap && snap.status && snap.status !== 'ready' && res.models.length === 0)
          return [provider, {ok: false, status: snap.status, error: 'provider_unavailable', models: []}];
        return [provider, {ok: true, status: 'ready', models: normalizeProviderModels(res.models)}];
      } catch {
        return [provider, {ok: false, status: snap?.status && snap.status !== 'ready' ? snap.status : 'unavailable',
          error: 'model_list_unavailable', models: []}];
      }
    }));
    return {ok: true, fetched_at: new Date().toISOString(), providers: Object.fromEntries(entries)};
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
  // Read the record, then the agent it names: a create that completes between the two lookups is not "removed".
  const creation = !agent && !input.agent_id ? await creationRecord(client, input.dispatch_id) : undefined;
  if (creation?.agent_id) agent = (await client.fetchAgent(creation.agent_id))?.agent;
  const file = notificationFile(input.dispatch_id);
  const record = readRecord(file);
  const completion_notification = record ? await notify(record) : notificationReceipt(file);
  return {ok: true, agent: project(agent), missing: Boolean(input.agent_id && !agent),
    ...(creation !== undefined ? {creation} : {}), completion_notification};
}

async function createClient() {
  const setup = path.dirname(fileURLToPath(import.meta.url));
  // A checkout installs beside this file; an installed wheel installs under the state directory, because an
  // upgrade replaces site-packages (sync_host_globals.ensure_paseo_bridge_dependencies).
  const candidates = [path.join(setup, 'paseo-bridge/node_modules'),
    ...new Set([process.env.ROUTER_STATE_DIR, path.join(os.homedir(), '.agent-compass')].filter(Boolean)
      .map((state) => path.join(state, 'paseo-bridge/node_modules')))];
  const sdkRoot = process.env.ROUTER_PASEO_SDK_ROOT ?? candidates.find((dir) => fs.existsSync(dir)) ?? candidates[0];
  const require = createRequire(path.join(path.resolve(sdkRoot), '_router_dispatch.cjs'));
  let daemonClientModule, WebSocket;
  try {
    daemonClientModule = require.resolve('@getpaseo/client/internal/daemon-client');
    ({WebSocket} = require('ws'));
  } catch (cause) {
    throw new Error('Paseo bridge dependencies are missing in ' + candidates.join(' and ') + '. Run: agentcompass setup (or npm --prefix setup/paseo-bridge ci in a checkout), or set ROUTER_PASEO_SDK_ROOT.', {cause});
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
  if (process.argv[2] === '--watch') return watch(process.argv[3], process.argv[4], process.argv[5], process.argv[6]);
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const client = await createClient();
  let connected = false;
  try {
    await client.connect();
    connected = true;
    console.log(JSON.stringify(await execute(client, input)));
  } catch {
    console.log(JSON.stringify({ok: false,
      sent: connected && (input.operation === 'create' || input.operation === 'resume'),
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
