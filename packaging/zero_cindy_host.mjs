// A stand-in for the CINDY HOST around the REAL plugin that `agentcompass cindy build-plugin` produced.
//
//   real:     the built main.js (run as Cindy runs a logic page), the real worker.cjs (a real `node` child speaking
//             JSON-RPC over stdio, as Cindy's node bridge does), and through it the real public `agentcompass roll`
//   fixture:  the host itself - trusted session_context injection and the experimental `cindy.tasks` API, which no
//             released Cindy build exposes yet (see references/herdr-cindy.md)
//
// usage: node zero_cindy_host.mjs <plugin dir> <scenario.json> <out.json>
// scenario: {workdir, session, steps:[...]}. A step is
//   {label, call: "roll"|"status"|"stop"|"cleanup", args, inject?: false, context?: {...}, parallel?: n, as?: <other session id>}
//   or {label, fixture: {...changes to the fake Cindy...}}   e.g. {capabilities, catalog, workingDir, staleRevision, runStatus, outputMessageId, pages, archiveFails}
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import crypto from 'node:crypto';
import readline from 'node:readline';
import { spawn, spawnSync } from 'node:child_process';

const [pluginDir, scenarioFile, outFile] = process.argv.slice(2);
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));
const trusted = { session_id: scenario.session, workdir: scenario.workdir, workdir_is_local: true, workdir_is_read_only: false };

// ---- the node bridge: the real worker.cjs
const worker = spawn(process.execPath, [path.join(pluginDir, 'worker.cjs')], { env: process.env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
const stderr = [];
worker.stderr.on('data', d => stderr.push(d));
const pending = new Map();
let rpcId = 0;
readline.createInterface({ input: worker.stdout }).on('line', line => {
  let reply; try { reply = JSON.parse(line); } catch { return; }
  const waiter = pending.get(reply.id); pending.delete(reply.id);
  if (waiter) waiter(reply);
});
const rpc = (method, params) => new Promise(resolve => {
  const id = ++rpcId; pending.set(id, resolve);
  worker.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
});

// ---- the fake Cindy
const host = {
  capabilities: 'full', catalog: 'matching', workingDir: null, staleRevision: false, archiveFails: false, createFault: null, finalRepeat: null,
  tasks: new Map(), runs: new Map(), createKeys: new Map(), sendKeys: new Map(), pages: 1,
  calls: [], prepared: null, nodeCalls: [], sent: [],
};
const log = (name, detail) => host.calls.push({ name, ...detail });
const ALL_OPS = ['models', 'create', 'send', 'get', 'getRun', 'listRuns', 'readMessages', 'cancel', 'archive'];

function routeOf(bundle) {
  return { agentKind: { claude: 'cc', codex: 'codex', pi: 'pi' }[bundle.harness], providerId: bundle.account, model: bundle.launch_model || bundle.model, effort: bundle.effort, fastMode: Boolean(bundle.fast) };
}
const cindy = {
  node: {
    request: async ({ method, params }) => {
      host.nodeCalls.push(method);
      const reply = await rpc(method, params);
      if (reply.error) return { ok: false, message: reply.error.message };
      if (method === 'prepare' && reply.result?.bundle) host.prepared = reply.result;
      return { ok: true, result: reply.result };
    },
  },
  tasks: {
    capabilities: async () => {
      log('capabilities', {});
      if (host.capabilities === 'denied') throw Error('PERMISSION_DENIED');
      const operations = host.capabilities === 'no-archive' ? ALL_OPS.filter(o => o !== 'archive') : ALL_OPS;
      return { operations, exactRoute: host.capabilities !== 'no-exact', createPermissionMode: true };
    },
    models: async () => {
      log('models', {});
      const route = host.prepared ? routeOf(host.prepared.bundle) : null;
      if (!route || host.catalog === 'empty') return { models: [] };
      const shown = host.catalog === 'other-model' ? { ...route, model: route.model + '-other' } : route;
      return { models: [{ route: shown, efforts: [route.effort], supportsFastMode: true }] };
    },
    create: async request => {
      log('create', { requestKey: request.requestKey, route: request.route, permissionMode: request.permissionMode });
      if (host.createKeys.has(request.requestKey)) { host.duplicateCreate = (host.duplicateCreate || 0) + 1; return structuredClone(host.tasks.get(host.createKeys.get(request.requestKey))); }
      const taskId = 'task-' + (host.tasks.size + 1);
      const task = { taskId, revision: 1, resolvedConfig: { ...request.route }, permissionMode: request.permissionMode, planModeEnabled: false, workingDir: host.workingDir || trusted.workdir };
      host.tasks.set(taskId, task); host.createKeys.set(request.requestKey, taskId);
      if (host.staleRevision) task.revision += 1;     // someone else changed the draft after it was created
      if (host.createFault === 'after') throw Error('CONNECTION_LOST after the draft was created');     // the host did the work, the answer never arrived
      return structuredClone({ ...task, revision: host.staleRevision ? task.revision - 1 : task.revision });
    },
    send: async request => {
      const task = host.tasks.get(request.taskId);
      log('send', { taskId: request.taskId, expectedRevision: request.expectedRevision, requestKey: request.requestKey, chars: request.text.length });
      if (!task) throw Error('TASK_NOT_FOUND');
      if (host.sendKeys.has(request.requestKey)) { host.duplicateSend = (host.duplicateSend || 0) + 1; return structuredClone(host.runs.get(host.sendKeys.get(request.requestKey))); }
      if (request.expectedRevision !== task.revision) throw Error('REVISION_CONFLICT: expected ' + request.expectedRevision + ', task is at ' + task.revision);
      const run = { runId: 'run-' + (host.runs.size + 1), taskId: task.taskId, status: 'running', acceptedConfig: { ...task.resolvedConfig } };
      host.runs.set(run.runId, run); host.sendKeys.set(request.requestKey, run.runId); task.revision += 1;
      const file = path.join(path.dirname(outFile), `cindy-sent-${run.runId}.txt`);
      fs.writeFileSync(file, request.text, 'utf8');
      host.sent.push({ runId: run.runId, file, sha256: crypto.createHash('sha256').update(request.text, 'utf8').digest('hex'), chars: request.text.length });
      return structuredClone(run);
    },
    getRun: async request => { log('getRun', { runId: request.runId }); return structuredClone(host.runs.get(request.runId)); },
    listRuns: async request => { log('listRuns', { taskId: request.taskId }); return { items: [...host.runs.values()].filter(r => r.taskId === request.taskId).map(r => structuredClone(r)), nextCursor: null }; },
    readMessages: async request => {
      log('readMessages', { after: request.after || null });
      const index = request.after ? Number(request.after) : 0;
      const last = index >= host.pages - 1;
      const run = [...host.runs.values()][0];
      const items = [{ id: last ? (run?.outputMessageId || 'out-final') : 'noise-' + index, role: 'assistant',
        content: last ? (host.finalRepeat ? host.finalRepeat.unit.repeat(host.finalRepeat.count) : (host.finalText || '[Worker final report] fixture result')) : 'intermediate page ' + index }];
      return { items, nextCursor: last ? null : String(index + 1) };
    },
    cancel: async request => { log('cancel', { runId: request.runId }); const run = host.runs.get(request.runId); run.status = 'stopping'; return structuredClone(run); },
    archive: async request => {
      log('archive', { taskId: request.taskId });
      if (host.archiveFails) throw Error('ARCHIVE_FAILED');
      host.tasks.get(request.taskId).archived = true; return structuredClone(host.tasks.get(request.taskId));
    },
  },
  onHostMessage: callback => { host.handler = callback; },
  send: message => { host.waiters.get(message.callId)?.(message); },
};
host.waiters = new Map();
let serial = 0;
vm.runInNewContext(fs.readFileSync(path.join(pluginDir, 'main.js'), 'utf8'), { cindy, Set, Boolean, Error, JSON });
if (host.capabilities === 'missing') delete cindy.tasks;

const operations = { last: null };
function callTool(step) {
  return new Promise(resolve => {
    const callId = 'call-' + (++serial);
    host.waiters.set(callId, resolve);
    const args = { ...(step.args || {}) };
    // "@operation" is the operation the previous roll returned; "@operation:<label>" the one a labelled step returned.
    if (typeof args.operation === 'string' && args.operation.startsWith('@operation'))
      args.operation = args.operation === '@operation' ? operations.last : operations[args.operation.slice('@operation:'.length)];
    // Cindy strips whatever session_context the caller supplied and injects the real one; `inject:false` is a host that does not.
    if (step.inject !== false) args.session_context = { ...(step.context || trusted), ...(step.as ? { session_id: step.as } : {}) };
    host.handler({ type: 'tool-call', tool: step.call, callId, args });
  });
}

const results = [];
try {
  for (const step of scenario.steps) {
    if (step.fixture) {
      const f = step.fixture;
      for (const key of ['capabilities', 'catalog', 'workingDir', 'staleRevision', 'archiveFails', 'pages', 'finalText', 'finalRepeat', 'createFault']) if (key in f) host[key] = f[key];
      if (f.capabilities === 'missing') delete cindy.tasks;
      for (const run of host.runs.values()) {
        if ('runStatus' in f) run.status = f.runStatus;
        if ('outputMessageId' in f) run.outputMessageId = f.outputMessageId;
        if ('error' in f) run.error = f.error;
      }
      results.push({ label: step.label, fixture: f });
      continue;
    }
    const n = step.parallel || 1;
    const replies = await Promise.all(Array.from({ length: n }, () => callTool(step)));
    const operation = replies.find(r => r.ok && r.result?.operation)?.result.operation;
    if (operation) { operations.last = operation; operations[step.label] = operation; }
    results.push({ label: step.label, replies });
  }
} finally {
  worker.stdin.end();
  if (worker.pid && worker.exitCode === null) spawnSync('taskkill', ['/PID', String(worker.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
}
fs.writeFileSync(outFile, JSON.stringify({
  results, calls: host.calls, nodeCalls: host.nodeCalls, sent: host.sent, prepared: host.prepared,
  duplicateCreate: host.duplicateCreate || 0, duplicateSend: host.duplicateSend || 0,
  tasks: [...host.tasks.values()], runs: [...host.runs.values()], workerStderr: Buffer.concat(stderr).toString('utf8').slice(-2000),
}, null, 1));
process.exit(0);
