// Narrow local SDK adapter. stdin JSON only; stdout contains metadata, never prompts or credentials.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';

export function project(agent) {
  if (!agent) return null;
  const result = Object.fromEntries(['id', 'provider', 'model', 'status', 'updatedAt', 'createdAt',
    'lastUserMessageAt', 'attentionReason', 'archivedAt', 'providerUnavailable',
    'currentModeId', 'thinkingOptionId', 'effectiveThinkingOptionId', 'labels']
    .filter(k => agent[k] !== undefined).map(k => [k, agent[k]]));
  if (Array.isArray(agent.pendingPermissions)) result.pendingPermissionCount = agent.pendingPermissions.length;
  return result;
}

export async function execute(client, input) {
  if (input.operation === 'create') {
    const r = input.request;
    if (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id === 'top-level')
      return {ok: false, sent: false, error: 'addressable_parent_required'};
    const parent = await client.fetchAgent(input.parent_id);
    if (!parent?.agent) return {ok: false, sent: false, error: 'parent_agent_not_found'};
    const cwd = parent.agent.cwd, workspaceId = r.workspaceId ?? parent.agent.workspaceId;
    const slash = r.provider.indexOf('/');
    const options = {
      provider: r.provider.slice(0, slash), model: r.provider.slice(slash + 1),
      title: r.title, cwd, workspaceId,
      callerAgentId: input.parent_id,
      modeId: r.settings?.modeId, thinkingOptionId: r.settings?.thinkingOptionId,
      featureValues: r.settings?.features, initialPrompt: r.initialPrompt, labels: r.labels,
      agentId: input.dispatch_id, idempotencyKey: input.dispatch_id,
      clientMessageId: input.dispatch_id,
    };
    try {
      return {ok: true, sent: true, agent: project(await client.createAgent(options))};
    } catch {
      // A timeout can follow successful creation; never report definite failure here.
      return {ok: false, sent: true, error: 'create_outcome_uncertain_reconcile_by_label'};
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
  return {ok: true, agent: project(agent), missing: Boolean(input.agent_id && !agent)};
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const setup = path.dirname(fileURLToPath(import.meta.url));
  const sdkRoot = process.env.ROUTER_PASEO_SDK_ROOT ?? path.resolve(setup, '../../paseo-plugins/paseo-pi-fast/node_modules');
  const require = createRequire(path.join(path.resolve(sdkRoot), '_router_dispatch.cjs'));
  const {DaemonClient} = await import(pathToFileURL(require.resolve('@getpaseo/client/internal/daemon-client')).href);
  const {WebSocket} = require('ws');
  // Local credential stays inside the adapter. No remote URL/credential forwarding.
  let listen = '127.0.0.1:6767';
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.paseo/config.json'), 'utf8'));
    if (typeof cfg?.daemon?.listen === 'string' && cfg.daemon.listen.trim()) {
      listen = cfg.daemon.listen.trim();
    }
  } catch {}
  const client = new DaemonClient({url: `ws://${listen}/ws`, clientId: randomUUID(),
    clientType: 'cli', appVersion: '0.11.2', connectTimeoutMs: 10000,
    localCredential: () => fs.readFileSync(path.join(os.homedir(), '.paseo/local-credential'), 'utf8').trim(),
    webSocketFactory: (url, config) => new WebSocket(url, {headers: config?.headers, protocols: config?.protocols}),
    reconnect: {enabled: false}});
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
  main().catch(() => { console.log(JSON.stringify({ok: false, sent: false, error: 'sdk_setup_failed'})); process.exitCode = 1; });
}
