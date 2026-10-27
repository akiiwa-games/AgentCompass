const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

function fixture(home, fsOverride = fs) {
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../server/fast.ts"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  vm.runInNewContext(code, { exports, console, require(id) {
    if (id === "node:fs") return fsOverride;
    if (id === "node:os") return { homedir: () => home };
    return require(id);
  } });
  return exports;
}

test("Fast mode atomically persists and reads the requested state", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-fast-"));
  try {
    const api = fixture(dir);
    assert.equal(api.readFastModeState().enabled, true);
    assert.equal((await api.handleSetFastMode({ enabled: false })).enabled, false);
    assert.equal(api.readFastModeState().enabled, false);
    assert.equal((await api.handleSetFastMode({})).enabled, true);
    assert.deepEqual(fs.readdirSync(path.join(dir, ".pi", "agent")), ["fast-mode.json"]);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test("Fast RPC rejects failed replacement and preserves the old setting", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-fast-fault-"));
  try {
    fixture(dir).writeFastModeState(false);
    const fault = fixture(dir, { ...fs, renameSync() { throw new Error("simulated replacement denied"); } });
    await assert.rejects(fault.handleSetFastMode({ enabled: true }), /replacement denied/);
    assert.equal(fault.readFastModeState().enabled, false);
    assert.deepEqual(fs.readdirSync(path.join(dir, ".pi", "agent")), ["fast-mode.json"]);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test("Malformed Fast config is reported, never silently enabled or overwritten", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-fast-corrupt-"));
  try {
    const api = fixture(dir);
    api.writeFastModeState(false);
    const target = path.join(dir, ".pi", "agent", "fast-mode.json");
    for (const invalid of ['{', '{"enabled":"false"}']) {
      fs.writeFileSync(target, invalid);
      await assert.rejects(api.handleSetFastMode({ enabled: true }));
      assert.equal(fs.readFileSync(target, "utf8"), invalid);
    }
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
