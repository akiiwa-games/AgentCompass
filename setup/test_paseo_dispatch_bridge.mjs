import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import path from 'node:path';
import {execute, project} from './paseo_dispatch_bridge.mjs';

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
  const result = await execute(fake, {operation: 'create', dispatch_id: id, parent_id: 'parent', request});
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
    createAgent: async () => {throw new Error('secret-canary');}}, input);
  assert.equal(result.sent, true);
  assert.equal(result.ok, false);
  assert(!JSON.stringify(result).includes('secret-canary'));
  assert.deepEqual(project({id: 'child', initialPrompt: 'secret-canary'}), {id: 'child'});
});
