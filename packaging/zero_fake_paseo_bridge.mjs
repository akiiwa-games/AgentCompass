// A stand-in for setup/paseo_dispatch_bridge.mjs: the Paseo HOST, as the router's dispatch flow sees it.
// The router runs `node <this file>` with one JSON request on stdin (ROUTER_PASEO_BRIDGE points here) and reads one
// JSON answer. Unlike the real bridge it needs no daemon: agents are records under $FIXTURE_STATE/agents, and each
// created agent is a REAL process (the fixture CLI in $FIXTURE_BIN) that finishes, fails, hangs or crashes by itself.
//
// Fault injection for the tests: $FIXTURE_STATE/faults.json {"create": "reject" | "uncertain" | "gone", "observe": "unavailable",
// "create_delay_ms": N}
// (each fault is consumed by the next call that matches it).
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

const STATE = process.env.FIXTURE_STATE;
const BIN = process.env.FIXTURE_BIN;
if (!STATE || !BIN) { console.log(JSON.stringify({ ok: false, sent: false, error: 'bridge_unavailable' })); process.exit(0); }

const readJson = (file, fallback) => { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; } };
const writeJson = (file, value) => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(value, null, 1)); };
const agentFile = id => path.join(STATE, 'agents', `${id.replace(/[^\w.-]/g, '_')}.json`);
const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
const canonicalPath = p => {
  if (typeof p !== 'string' || !p.trim()) return null;
  const resolved = path.resolve(p.trim());
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
};
const samePath = (a, b) => {
  const left = canonicalPath(a), right = canonicalPath(b);
  return Boolean(left && right && left === right);
};

function takeFault(kind) {
  const file = path.join(STATE, 'faults.json');
  const faults = readJson(file, {});
  const value = faults[kind];
  if (value) { delete faults[kind]; writeJson(file, faults); }
  return value;
}

function project(agent) {
  if (!agent) return null;
  const keys = ['id', 'provider', 'model', 'status', 'cwd', 'updatedAt', 'createdAt', 'attentionReason', 'archivedAt',
    'providerUnavailable', 'currentModeId', 'thinkingOptionId', 'effectiveThinkingOptionId', 'labels', 'lastError'];
  return Object.fromEntries(keys.filter(k => agent[k] !== undefined).map(k => [k, agent[k]]));
}

function receipt(agent) {
  const done = agent && (agent.status === 'error' || agent.status === 'closed' || (agent.status === 'idle' && agent.attentionReason === 'finished'));
  return { status: done ? 'delivered' : 'watching', delivered: done ? [agent.id] : [], pending: null };
}

function observeAgent(id) {
  const file = agentFile(id);
  const agent = readJson(file, null);
  if (!agent) return null;
  if ((agent.status === 'running' || agent.status === 'initializing') && agent.pid && !alive(agent.pid)) {
    // the process is gone without having said how it ended: that is a crash
    agent.status = 'error'; agent.attentionReason = 'error'; agent.lastError = 'worker process vanished';
    agent.updatedAt = new Date().toISOString();
    writeJson(file, agent);
  }
  return agent;
}

function create(input) {
  // A slow host: the request is logged (bridge-calls.jsonl) but nothing exists on the host yet for this long.
  const delay = Number(takeFault('create_delay_ms')) || 0;
  if (delay > 0) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, delay);
  const parents = readJson(path.join(STATE, 'parents.json'), []);
  if (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id === 'top-level')
    return { ok: false, sent: false, error: 'addressable_parent_required' };
  if (!parents.includes(input.parent_id)) return { ok: false, sent: false, error: 'parent_agent_not_found' };
  const fault = takeFault('create');
  if (fault === 'reject') return { ok: false, sent: false, error: 'host_creation_failed', failure_class: 'model_unavailable' };
  if (fault === 'reject_request') return { ok: false, sent: false, error: 'host_creation_failed', failure_class: 'request_invalid' };
  if (fault === 'reject_unknown') return { ok: false, sent: false, error: 'host_creation_failed', failure_class: 'unknown' };
  const existing = observeAgent(input.dispatch_id);
  if (existing) return { ok: true, sent: true, agent: project(existing), completion_notification: receipt(existing) };
  const request = input.request;
  const slash = request.provider.indexOf('/');
  const provider = request.provider.slice(0, slash), model = request.provider.slice(slash + 1);
  const now = new Date().toISOString();
  const agent = {
    id: input.dispatch_id, provider, model, title: request.title, status: 'running', createdAt: now, updatedAt: now,
    cwd: request.cwd || process.cwd(),
    labels: request.labels, currentModeId: request.settings?.modeId, thinkingOptionId: request.settings?.thinkingOptionId,
    effectiveThinkingOptionId: request.settings?.thinkingOptionId, parent_id: input.parent_id,
  };
  if (fault !== 'gone') {
    const family = fs.existsSync(path.join(BIN, `${provider}.exe`)) ? provider : 'codex';
    const promptFile = path.join(STATE, 'agents', `${input.dispatch_id}.prompt.md`);
    fs.mkdirSync(path.dirname(promptFile), { recursive: true });
    fs.writeFileSync(promptFile, request.initialPrompt);
    const out = fs.openSync(path.join(STATE, 'agents', `${input.dispatch_id}.out.txt`), 'w');
    const err = fs.openSync(path.join(STATE, 'agents', `${input.dispatch_id}.err.txt`), 'w');
    writeJson(agentFile(input.dispatch_id), agent);
    const child = spawn(path.join(BIN, `${family}.exe`), ['exec', '-m', model, '-'], {
      detached: true, windowsHide: true, stdio: [fs.openSync(promptFile, 'r'), out, err],
      env: { ...process.env, FIXTURE_AGENT_ID: input.dispatch_id, ROUTER_PARENT_AGENT_ID: input.parent_id,
        ROUTER_PARENT_ADDRESS: input.parent_id, ROUTER_PARENT_HOST: 'paseo', AGENTCOMPASS_WORKER: '1' },
    });
    agent.pid = child.pid;
    child.unref();
    writeJson(agentFile(input.dispatch_id), agent);
  } else {
    writeJson(agentFile(input.dispatch_id), agent);   // created on the host, but the answer never arrives
  }
  if (fault === 'uncertain' || fault === 'gone')
    return { ok: false, sent: true, error: 'create_outcome_uncertain_reconcile_by_label', completion_notification: receipt(agent) };
  return { ok: true, sent: true, agent: project(agent), completion_notification: receipt(agent) };
}

function resume(input) {
  const parents = readJson(path.join(STATE, 'parents.json'), []);
  if (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id === 'top-level')
    return { ok: false, sent: false, error: 'addressable_parent_required' };
  if (!parents.includes(input.parent_id)) return { ok: false, sent: false, error: 'parent_agent_not_found' };
  const fault = takeFault('resume');
  if (fault === 'reject') return { ok: false, sent: false, error: 'host_resume_failed' };
  const existing = observeAgent(input.agent_id);
  if (!existing) return { ok: false, sent: false, error: 'resume_agent_not_found' };
  const request = input.request;
  const slash = request.provider.indexOf('/');
  const provider = request.provider.slice(0, slash), model = request.provider.slice(slash + 1);
  const now = new Date().toISOString();
  existing.status = 'running';
  existing.updatedAt = now;
  delete existing.attentionReason;
  existing.labels = { ...(existing.labels || {}), ...(request.labels || {}) };
  const family = fs.existsSync(path.join(BIN, `${provider}.exe`)) ? provider : 'codex';
  const promptFile = path.join(STATE, 'agents', `${input.dispatch_id}.prompt.md`);
  fs.mkdirSync(path.dirname(promptFile), { recursive: true });
  fs.writeFileSync(promptFile, request.initialPrompt);
  const out = fs.openSync(path.join(STATE, 'agents', `${input.dispatch_id}.out.txt`), 'w');
  const err = fs.openSync(path.join(STATE, 'agents', `${input.dispatch_id}.err.txt`), 'w');
  writeJson(agentFile(existing.id), existing);
  const child = spawn(path.join(BIN, `${family}.exe`), ['exec', '-m', model, '-'], {
    detached: true, windowsHide: true, stdio: [fs.openSync(promptFile, 'r'), out, err],
    env: { ...process.env, FIXTURE_AGENT_ID: existing.id, ROUTER_PARENT_AGENT_ID: input.parent_id,
      ROUTER_PARENT_ADDRESS: input.parent_id, ROUTER_PARENT_HOST: 'paseo', AGENTCOMPASS_WORKER: '1' },
  });
  existing.pid = child.pid;
  child.unref();
  writeJson(agentFile(existing.id), existing);
  return { ok: true, sent: true, agent: project(existing), completion_notification: receipt(existing) };
}

function observe(input) {
  if (takeFault('observe') === 'unavailable') return { ok: false, error: 'bridge_unavailable' };
  let agent = input.agent_id ? observeAgent(input.agent_id) : null;
  if (!agent) {
    const dir = path.join(STATE, 'agents');
    const found = fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => f.endsWith('.json'))
      .map(f => readJson(path.join(dir, f), null)).filter(a => a?.labels?.['router.dispatch_id'] === input.dispatch_id) : [];
    if (found.length > 1) return { ok: false, error: 'duplicate_dispatch_agents' };
    agent = found[0] ? observeAgent(found[0].id) : null;
  }
  // Like the daemon: no record for a create that never reached this host.
  return { ok: true, agent: project(agent), missing: Boolean(input.agent_id && !agent), ...(!agent && !input.agent_id ? { creation: null } : {}),
    completion_notification: receipt(agent) };
}

function workspaceAgents(input) {
  if (!input.cwd) return { ok: false, error: 'workspace_directory_required' };
  const dir = path.join(STATE, 'agents');
  const agents = [];
  if (fs.existsSync(dir)) {
    for (const file of fs.readdirSync(dir).filter(f => f.endsWith('.json'))) {
      const a = readJson(path.join(dir, file), null);
      const latest = a?.id ? observeAgent(a.id) : null;
      if (!latest || !['running', 'initializing'].includes(latest.status)) continue;
      if (latest.labels?.['router.dispatch_id'] && samePath(latest.cwd, input.cwd)) {
        agents.push({ id: latest.id, status: latest.status, dispatch_id: latest.labels['router.dispatch_id'] });
      }
    }
  }
  return { ok: true, agents, placeable: true };
}

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  let input;
  try { input = JSON.parse(raw); } catch { console.log(JSON.stringify({ ok: false, error: 'invalid_request' })); return; }
  const log = path.join(STATE, 'bridge-calls.jsonl');
  fs.mkdirSync(STATE, { recursive: true });
  fs.appendFileSync(log, JSON.stringify({ at: new Date().toISOString(), operation: input.operation, dispatch_id: input.dispatch_id, parent_id: input.parent_id }) + '\n');
  let result;
  try {
    result = input.operation === 'create' ? create(input)
      : input.operation === 'resume' ? resume(input)
      : input.operation === 'observe' ? observe(input)
      : input.operation === 'workspace_agents' ? workspaceAgents(input)
      : { ok: false, sent: false, error: 'unsupported_operation' };
  } catch (error) {
    result = { ok: false, sent: input.operation === 'create' || input.operation === 'resume', error: String(error.message || error) };
  }
  fs.appendFileSync(path.join(STATE, 'bridge-responses.jsonl'), JSON.stringify({ at: new Date().toISOString(), operation: input.operation, dispatch_id: input.dispatch_id,
    answer: { ok: result.ok, sent: result.sent, error: result.error, missing: result.missing, agent: result.agent ? { id: result.agent.id, status: result.agent.status, attentionReason: result.agent.attentionReason } : null } }) + String.fromCharCode(10));
  console.log(JSON.stringify(result));
});
