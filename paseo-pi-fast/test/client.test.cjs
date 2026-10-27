const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

test("Fast toggle stays in Pi composers, never the workspace header", async () => {
  const exports = {};
  const source = readFileSync(path.join(__dirname, "../index.client.tsx"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, {
    exports, AbortController, console,
    require(id) {
      assert.equal(id, "./shared/fast");
      return { getFastModeRpc: "get", setFastModeRpc: "set" };
    },
  });
  const pills = new Map();
  let observer;
  let signal;
  let enabled = true;
  let commandsRemoved = 0;
  const cleanup = exports.default({
    rpc: async (contract) => {
      if (contract === "set") enabled = !enabled;
      return { enabled };
    },
    paseo: {
      agents: {
        list: async (options) => {
          signal = options.signal;
          return { subscription: { subscribe: (handlers) => { observer = handlers; } } };
        },
      },
      workspaces: { list() { assert.fail("No workspace subscription needed"); } },
    },
    addHeaderButton() { assert.fail("Fast must not appear in the header"); },
    addComposerPill({ agentId, workspaceId, button }) {
      const pill = { workspaceId, button, removed: false };
      pills.set(agentId, pill);
      return {
        update(next) { pill.button = next; },
        remove() { pill.removed = true; },
      };
    },
    addCommandCenterItem: () => () => { commandsRemoved++; },
    addSlashCommand: () => () => { commandsRemoved++; },
  });
  await new Promise(setImmediate);
  observer.snapshot({ entries: ["pi", "pi/openai", "pi:custom", "claude"].map((provider) => ({
    agent: { id: provider, workspaceId: "workspace", provider },
  })) });
  await new Promise(setImmediate);
  for (const id of ["pi", "pi/openai", "pi:custom"]) {
    assert.equal(pills.get(id).button.visible, true);
    assert.equal(pills.get(id).workspaceId, "workspace");
  }
  assert.equal(pills.get("claude").button.visible, false);
  await pills.get("pi").button.behavior.onPress();
  assert.equal(pills.get("pi/openai").button.label, "Fast: OFF");
  observer.update({ type: "agent_update", payload: {
    kind: "upsert", agent: { id: "pi", workspaceId: "workspace", provider: "codex" },
  } });
  assert.equal(pills.get("pi").button.visible, false);
  observer.update({ type: "agent_update", payload: { kind: "remove", agentId: "pi:custom" } });
  assert.equal(pills.get("pi:custom").removed, true);
  cleanup();
  assert.equal(signal.aborted, true);
  assert.equal(commandsRemoved, 2);
  for (const pill of pills.values()) assert.equal(pill.removed, true);
});
