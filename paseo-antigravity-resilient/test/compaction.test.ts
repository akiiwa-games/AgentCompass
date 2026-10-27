import assert from "node:assert/strict";
import { appendFileSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import {
  beginBusinessTurn, beginCompact, emptyCompactionState, finishCompact,
  nativeObservation, parseNativeCompactionLog, RAPID_REFILL_REQUEST_WINDOW, reconcileNativeCompaction,
  recoverInterruptedCompacts, requestsSinceLastCompact,
  type NativeObservation,
} from "../server/internal/compaction.ts";
import { ConversationStore, DEFAULT_FS_OPS } from "../server/internal/store.ts";

const T = Date.parse("2026-10-16T03:00:00Z");
function flat(step: number, type: string, extra: Record<string, unknown> = {}) {
  return { step_index: step, source: "model", type, status: "DONE",
    created_at: new Date(T + step * 1_000).toISOString(), ...extra };
}
function observations(rows: unknown[]) { return parseNativeCompactionLog(rows.map((r) => JSON.stringify(r)).join("\n")); }

test("actual flat transcript shape retains metadata only; torn rows, non-DONE and repeated steps are safe", () => {
  const rows = [flat(0, "USER_INPUT", { content: "Research without persisting this text in telemetry" }),
    flat(1, "PLANNER_RESPONSE", { input_tokens: 50_000, cache_read_tokens: 200_000, output_tokens: 300, thinking: "private" }),
    flat(2, "CHECKPOINT", { content: "private summary", truncated_fields: [] }),
    flat(3, "PLANNER_RESPONSE", { input_tokens: 20_000, output_tokens: 100 }),
    flat(4, "CHECKPOINT", { status: "ACTIVE" })];
  const parsed = parseNativeCompactionLog(rows.map((r) => JSON.stringify(r)).join("\n") + '\nmalformed\n{"step_index":5');
  assert.equal(parsed.length, 4);
  const state = emptyCompactionState();
  reconcileNativeCompaction(state, [...parsed].reverse());
  assert.equal(state.events.length, 1);
  assert.deepEqual([state.events[0].preTokens, state.events[0].postTokens], [250_300, 20_100]);
  assert.equal(state.events[0].id, "native-checkpoint:2");
  assert.equal(state.lastContextTokens, 20_100);
  assert.equal(JSON.stringify(state).includes("private"), false);
  const saved = JSON.stringify(state);
  reconcileNativeCompaction(state, [...parsed, parsed[2]]);
  assert.equal(JSON.stringify(state), saved);
  assert.equal(nativeObservation({ event: "step_update", timestamp: new Date(T).toISOString(),
    step_update: { step_index: 10, state: "DONE", step_type: "CHECKPOINT" } })?.kind, "checkpoint");
});

test("manual and idle attempts associate a native checkpoint exactly once, even when the log arrives after the result", () => {
  const state = emptyCompactionState();
  beginBusinessTurn(state);
  beginCompact(state, "turn-2:auto-compact", "idle", T);
  finishCompact(state, "turn-2:auto-compact", true, T + 4_000, 20_000);
  const rows = observations([flat(1, "USER_INPUT", { content: "/compact" }), flat(2, "CHECKPOINT"),
    flat(3, "PLANNER_RESPONSE", { input_tokens: 25_000, output_tokens: 100 })]);
  reconcileNativeCompaction(state, rows);
  assert.equal(state.events.length, 1);
  assert.equal(state.events[0].source, "idle");
  assert.equal(state.events[0].nativeStep, 2);
  assert.equal(state.events[0].postTokens, 25_100);
  beginBusinessTurn(state);
  beginCompact(state, "turn-4:manual-compact", "manual", T + 10_000);
  reconcileNativeCompaction(state, [...rows, ...observations([flat(11, "USER_INPUT", { content: "/compact focus" }), flat(12, "CHECKPOINT")])]);
  finishCompact(state, "turn-4:manual-compact", true, T + 13_000, 15_000);
  assert.equal(state.events.length, 2);
  assert.equal(state.consecutiveRapidRefills, 0);
  assert.equal(state.lastContextTokens, 15_000);
});

test("late repaired checkpoint is reconciled without a duplicate or a new turn; native checkpoints never trip the idle breaker", () => {
  const state = emptyCompactionState();
  const rows = [flat(0, "USER_INPUT", { content: "work" }), flat(2, "CHECKPOINT"),
    flat(4, "CHECKPOINT"), flat(6, "CHECKPOINT"), flat(8, "CHECKPOINT")];
  reconcileNativeCompaction(state, observations(rows.filter((r) => r.step_index !== 4)));
  assert.equal(state.consecutiveRapidRefills, 0);
  reconcileNativeCompaction(state, observations(rows));
  assert.equal(state.events.length, 4);
  assert.equal(state.lastCompactId, "native-checkpoint:8");
  assert.equal(state.consecutiveRapidRefills, 0, "agy's own checkpoints in one long turn are normal work, not refills");
  assert.equal(state.rapidRefillBreakerTripped, false);
  const snapshot = JSON.stringify(state);
  reconcileNativeCompaction(state, observations(rows.reverse()));
  assert.equal(JSON.stringify(state), snapshot);
  assert.equal(parseNativeCompactionLog(JSON.stringify(flat(9, "CHECKPOINT", { created_at: "broken" }))).length, 0);
});

test("sanitized real legacy completion timings reconcile six old rows plus ten checkpoints to ten actual events", () => {
  // Real relative times/steps only; no conversation identity, content or paths.
  const metadata: Array<[number, number, string]> = [
    [0,0,"u"],[47,363000,"u"],[123,742000,"c"],[208,1733000,"m"],[210,1733000,"c"],
    [211,1937000,"u"],[252,2799000,"u"],[299,3429000,"m"],[301,3430000,"c"],
    [302,5475000,"u"],[313,10566000,"u"],[341,11170000,"m"],[343,11170000,"c"],
    [344,37325000,"u"],[430,37641000,"c"],[522,38673000,"m"],[524,38674000,"c"],
    [525,41301000,"u"],[536,41378000,"u"],[545,43384000,"u"],[566,43607000,"u"],
    [581,43999000,"m"],[583,44000000,"c"],[584,44592000,"u"],[600,45710000,"u"],
    [640,46413000,"m"],[642,46413000,"c"],[643,46994000,"u"],[678,52048000,"u"],
    [690,54208000,"u"],[691,54209000,"c"],[777,55247000,"u"],[785,56883000,"u"],
    [789,59020000,"u"],[807,59421000,"u"],[810,59910000,"u"],[829,61956000,"u"],
    [830,61956000,"c"],[834,65254000,"u"],[858,66526000,"u"],[860,66594000,"u"],[920,67062000,"u"],
  ];
  const rows: NativeObservation[] = metadata.map(([step, offset, kind]) => ({
    step, at: T + offset, kind: kind === "c" ? "checkpoint" : "user", manual: kind === "m",
  }));
  const completed = [1765965, 3466605, 11221493, 38726042, 44049864, 46461878];
  const state = emptyCompactionState();
  completed.forEach((offset, i) => {
    state.logicalTurn = [2, 4, 6, 7, 11, 13][i];
    const event = beginCompact(state, `legacy-${i}`, "idle", T + offset);
    event.legacyCompletedAt = event.at;
    finishCompact(state, event.id, true, event.at);
  });
  reconcileNativeCompaction(state, rows);
  assert.equal(state.events.length, 10);
  assert.equal(state.events.filter((e) => e.source === "idle" && e.nativeStep !== undefined).length, 6);
  assert.deepEqual(state.events.filter((e) => e.source === "idle").map((e) => e.nativeStep), [210,301,343,524,583,642]);
  assert.deepEqual(state.events.filter((e) => e.source === "idle").map((e) => e.finishedAt! - e.at), [32965,36605,51493,52042,49864,48878]);
  assert.equal(state.lastCompactId, "native-checkpoint:830");
  assert.equal(state.consecutiveRapidRefills, 0);
  assert.equal(state.rapidRefillBreakerTripped, false);
  assert.equal(state.turnsSinceLastCompact, 4);
  const once = JSON.stringify(state);
  reconcileNativeCompaction(state, rows);
  assert.equal(JSON.stringify(state), once);
});

test("legacy matching requires explicit unambiguous compact interval; modern attempts keep strict start bound", () => {
  const validRows = observations([flat(0, "USER_INPUT", { content: "/compact" }), flat(1, "CHECKPOINT"),
    flat(60, "USER_INPUT", { content: "real work" })]);
  for (const scenario of ["not-compact", "no-next-input", "two-checkpoints", "modern", "two-completions"]) {
    const state = emptyCompactionState();
    const event = beginCompact(state, "legacy", "idle", T + 40_000);
    if (scenario !== "modern") event.legacyCompletedAt = event.at;
    finishCompact(state, event.id, true, event.at);
    if (scenario === "two-completions") {
      const duplicate = beginCompact(state, "other", "idle", T + 45_000);
      duplicate.legacyCompletedAt = duplicate.at;
      finishCompact(state, duplicate.id, true, duplicate.at);
    }
    const rows = validRows.map((row) => ({ ...row }));
    if (scenario === "not-compact") rows[0].manual = false;
    if (scenario === "no-next-input") rows.pop();
    if (scenario === "two-checkpoints") rows.push({ step: 2, at: T + 2_000, kind: "checkpoint" });
    reconcileNativeCompaction(state, rows);
    assert.equal(event.nativeStep, undefined, scenario);
  }
});

test("native history uses step ordering and cannot rewind live context or re-clear newer failures", () => {
  const state = emptyCompactionState();
  const old = observations([flat(0, "USER_INPUT", { content: "work" }), flat(1, "CHECKPOINT"),
    flat(2, "PLANNER_RESPONSE", { input_tokens: 20_000 })]);
  reconcileNativeCompaction(state, old);
  beginCompact(state, "turn-2:auto-compact", "idle", T + 5_000);
  finishCompact(state, "turn-2:auto-compact", false, T + 6_000);
  state.lastContextTokens = 270_000;
  state.contextAt = T + 7_000;
  state.contextStep = 10;
  const before = JSON.stringify(state);
  reconcileNativeCompaction(state, old.reverse());
  assert.equal(JSON.stringify(state), before);
  assert.equal(state.consecutiveCompactFailures, 1);
  const later = observations([flat(11, "CHECKPOINT", { created_at: new Date(T + 6_500).toISOString() })]);
  reconcileNativeCompaction(state, later, false);
  assert.equal(state.lastContextTokens, 0, "new checkpoint with no post sample means unknown, not old input size");
});

test("failure and rapid-refill state survives independent stores; explicit manual compact remains an escape", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-compact-state-"));
  try {
    let state = emptyCompactionState();
    state.modelRequests = 0;
    for (let i = 0; i < 3; i++) {
      beginBusinessTurn(state);
      beginCompact(state, `fail-${i}`, "idle", T + i * 5_000);
      finishCompact(state, `fail-${i}`, false, T + i * 5_000 + 1_000);
      assert.equal(new ConversationStore({ storeDir: dir, brainDir: dir }).saveCompaction("conv", state), true);
      state = new ConversationStore({ storeDir: dir, brainDir: dir }).loadCompaction("conv");
      assert.equal(state.consecutiveCompactFailures, i + 1);
    }
    beginCompact(state, "escape", "manual", T + 20_000);
    finishCompact(state, "escape", true, T + 21_000, 10_000);
    assert.equal(state.consecutiveCompactFailures, 0);
    for (let i = 0; i < 3; i++) {
      beginBusinessTurn(state);
      state.modelRequests += 2;
      beginCompact(state, `rapid-${i}`, "idle", T + 30_000 + i * 5_000);
      finishCompact(state, `rapid-${i}`, true, T + 31_000 + i * 5_000, 10_000);
      new ConversationStore({ storeDir: dir, brainDir: dir }).saveCompaction("conv", state);
      state = new ConversationStore({ storeDir: dir, brainDir: dir }).loadCompaction("conv");
    }
    assert.equal(state.rapidRefillBreakerTripped, true);
    assert.equal(state.consecutiveRapidRefills, 3);
    beginCompact(state, "manual-escape", "manual", T + 60_000);
    finishCompact(state, "manual-escape", true, T + 61_000, 11_000);
    assert.equal(state.rapidRefillBreakerTripped, false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("interrupted attempt is counted once on reopen; a native checkpoint proves completion before crash", () => {
  const failed = emptyCompactionState();
  beginCompact(failed, "pending", "idle", T);
  recoverInterruptedCompacts(failed, T + 5_000);
  recoverInterruptedCompacts(failed, T + 6_000);
  assert.equal(failed.consecutiveCompactFailures, 1);
  assert.equal(failed.events[0].status, "interrupted");
  const completed = emptyCompactionState();
  beginCompact(completed, "pending", "idle", T);
  reconcileNativeCompaction(completed, observations([flat(2, "CHECKPOINT")]));
  recoverInterruptedCompacts(completed, T + 5_000);
  assert.equal(completed.consecutiveCompactFailures, 0);
  assert.equal(completed.events.length, 1);
  assert.equal(completed.events[0].status, "completed");
});

test("legacy timeline migration and native log repair are non-destructive; atomic state failure preserves previous primary", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-compact-atomic-"));
  try {
    const store = new ConversationStore({ storeDir: dir, brainDir: dir });
    store.upsert("conv", { id: "u1", type: "user_message", text: "Full research text retained" }, new Date(T).toISOString());
    store.upsert("conv", { id: "turn-2:auto-compact", type: "notification", level: "info",
      message: "Auto-compacted context (250,000 tokens) 1m before 5m hot cache expiry." }, new Date(T + 2_000).toISOString());
    store.flush("conv");
    const original = readFileSync(store.snapshotPath("conv"), "utf8");
    const state = store.loadCompaction("conv");
    assert.equal(state.hasCompactedInSession, true);
    assert.equal(state.events[0].preTokens, 250_000);
    assert.equal(readFileSync(store.snapshotPath("conv"), "utf8"), original);
    assert.equal(store.saveCompaction("conv", state), true);
    const saved = readFileSync(store.compactionPath("conv"), "utf8");
    const broken = new ConversationStore({ storeDir: dir, brainDir: dir, fsOps: {
      renameSync: ((from, to) => {
        if (String(to) === store.compactionPath("conv") && !String(from).includes(".prev.tmp")) throw new Error("test rename failure");
        DEFAULT_FS_OPS.renameSync(from, to);
      }) as typeof DEFAULT_FS_OPS.renameSync,
    } });
    state.consecutiveCompactFailures = 3;
    assert.equal(broken.saveCompaction("conv", state), false);
    assert.equal(readFileSync(store.compactionPath("conv"), "utf8"), saved);
    writeFileSync(store.compactionPath("conv"), "{broken");
    assert.equal(store.loadCompaction("conv").consecutiveCompactFailures, 0, "verified backup restores state");
    const logs = path.join(dir, "conv", ".system_generated", "logs");
    mkdirSync(logs, { recursive: true });
    writeFileSync(path.join(logs, "transcript.jsonl"), JSON.stringify(flat(4, "CHECKPOINT")) + '\n{"step_index":');
    assert.equal(store.readNativeCompaction("conv").length, 1);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("native transcript reads are incremental across appends and fall back to a full reread on truncation or rewrite", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-compact-incremental-"));
  try {
    const reads: Array<{ position: number; length: number }> = [];
    const store = new ConversationStore({ storeDir: dir, brainDir: dir, fsOps: {
      readSync: ((fd, buffer, offset, length, position) => {
        const read = DEFAULT_FS_OPS.readSync(fd, buffer, offset, length, position);
        reads.push({ position: Number(position), length: read });
        return read;
      }) as typeof DEFAULT_FS_OPS.readSync,
    } });
    const logs = path.join(dir, "conv", ".system_generated", "logs");
    mkdirSync(logs, { recursive: true });
    const file = path.join(logs, "transcript.jsonl");
    const line = (row: unknown) => JSON.stringify(row) + "\n";
    const bytesRead = () => reads.reduce((sum, r) => sum + r.length, 0);
    const expectFull = (text: string) => assert.deepEqual([...store.readNativeCompaction("conv")], parseNativeCompactionLog(text));

    assert.equal(store.readNativeCompaction("conv").length, 0, "missing transcript");
    // A large first row spans several 1 MiB read chunks.
    let text = line(flat(0, "USER_INPUT", { content: "x".repeat(2_500_000) })) +
      line(flat(1, "PLANNER_RESPONSE", { input_tokens: 250_000 })) + line(flat(2, "CHECKPOINT"));
    writeFileSync(file, text);
    const first = store.readNativeCompaction("conv");
    assert.deepEqual(first.map((o) => o.kind), ["user", "usage", "checkpoint"]);
    expectFull(text);
    assert.ok(Object.isFrozen(first), "shared snapshot cannot be mutated by callers");

    reads.length = 0;
    assert.equal(store.readNativeCompaction("conv"), first, "unchanged file returns the cached snapshot");
    assert.equal(reads.length, 0, "unchanged file performs no reads");

    // Append one complete row plus a torn row: only the new range is scanned.
    const appended = line(flat(3, "PLANNER_RESPONSE", { input_tokens: 12_000 })) + '{"step_index":4,"type":"CHECK';
    appendFileSync(file, appended);
    text += appended;
    reads.length = 0;
    const second = store.readNativeCompaction("conv");
    assert.equal(second.at(-1)?.step, 3);
    expectFull(text);
    // New bytes plus constant-size fingerprints (head, anchor check, new anchor).
    assert.ok(bytesRead() <= appended.length + 3 * 256, `incremental read touched ${bytesRead()} bytes`);
    assert.ok(reads.every((r) => r.length <= 256 || r.position >= text.length - appended.length));

    // The torn row heals once its remainder and newline arrive.
    const completion = `POINT","status":"DONE","created_at":"${new Date(T + 4_000).toISOString()}"}\n`;
    appendFileSync(file, completion);
    text += completion;
    reads.length = 0;
    assert.equal(store.readNativeCompaction("conv").at(-1)?.kind, "checkpoint");
    expectFull(text);
    assert.ok(bytesRead() < 2_000, "torn row is re-read from its start only");

    // Truncation (shorter file) rereads in full and drops vanished steps.
    text = line(flat(0, "USER_INPUT", { content: "fresh" })) + line(flat(1, "CHECKPOINT"));
    writeFileSync(file, text);
    expectFull(text);
    assert.equal(store.readNativeCompaction("conv").some((o) => o.step === 3), false);

    // A replaced file that is longer but has a different prefix is not treated as an append.
    text = line(flat(0, "USER_INPUT", { content: "other" })) + line(flat(5, "PLANNER_RESPONSE", { output_tokens: 7 })) +
      line(flat(6, "CHECKPOINT"));
    writeFileSync(file, text);
    expectFull(text);
    assert.equal(store.readNativeCompaction("conv").some((o) => o.step === 1), false);

    rmSync(file);
    assert.equal(store.readNativeCompaction("conv").length, 0, "deleted transcript resets the cursor");
    writeFileSync(file, line(flat(9, "CHECKPOINT")));
    assert.deepEqual(store.readNativeCompaction("conv").map((o) => o.step), [9]);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("reconciling the same native rows twice is idempotent and never mutates the input", () => {
  const rows = observations([flat(0, "USER_INPUT", { content: "task" }),
    flat(1, "PLANNER_RESPONSE", { input_tokens: 210_000 }), flat(2, "CHECKPOINT"),
    flat(3, "PLANNER_RESPONSE", { input_tokens: 9_000 }), flat(4, "USER_INPUT", { content: "/compact" }),
    flat(5, "CHECKPOINT"), flat(6, "PLANNER_RESPONSE", { output_tokens: 4_000 })]);
  const frozen = Object.freeze(rows.map((r) => Object.freeze({ ...r })));
  const state = emptyCompactionState();
  const added = reconcileNativeCompaction(state, frozen);
  assert.equal(added.length, 2);
  const once = JSON.stringify(state);
  assert.equal(reconcileNativeCompaction(state, frozen).length, 0, "no new events on replay");
  assert.equal(JSON.stringify(state), once);
  assert.equal(reconcileNativeCompaction(state, [...frozen].reverse()).length, 0, "order independent");
  assert.equal(JSON.stringify(state), once);
  assert.deepEqual(state.events.map((e) => [e.nativeStep, e.source, e.postTokens]), [[2, "native", 9_000], [5, "manual", 4_000]]);
  assert.equal(state.lastContextTokens, 4_000);
  assert.equal(state.nativeStep, 6);
});

test("rapid refills count idle compacts only; native checkpoints keep distance but neither add nor clear", () => {
  const state = emptyCompactionState();
  // `requests` is the model-request count the turn's transcript reread reports.
  const idle = (requests: number, at: number) => {
    beginBusinessTurn(state);
    state.modelRequests = requests;
    beginCompact(state, `idle-${requests}`, "idle", T + at);
    finishCompact(state, `idle-${requests}`, true, T + at + 500, 10_000);
  };
  idle(0, 0);
  assert.equal(state.consecutiveRapidRefills, 0, "the first compact is not a refill");
  idle(5, 10_000);
  assert.equal(state.consecutiveRapidRefills, 1);
  // A native checkpoint between idle compacts is neutral.
  beginBusinessTurn(state);
  state.modelRequests = 9;
  reconcileNativeCompaction(state, observations([flat(30, "CHECKPOINT", { created_at: new Date(T + 20_000).toISOString() })]), false);
  assert.equal(state.lastCompactId, "native-checkpoint:30");
  assert.equal(state.consecutiveRapidRefills, 1);
  idle(15, 40_000);
  assert.equal(state.consecutiveRapidRefills, 2, "distance is measured from the native checkpoint (6, not 10)");
  idle(20, 50_000);
  assert.equal(state.consecutiveRapidRefills, 3);
  assert.equal(state.rapidRefillBreakerTripped, true, "real idle thrashing still trips the breaker");
  state.modelRequests = 20 + RAPID_REFILL_REQUEST_WINDOW - 1;
  beginBusinessTurn(state);
  assert.equal(state.rapidRefillBreakerTripped, true, "still inside the window");
  state.modelRequests = 20 + RAPID_REFILL_REQUEST_WINDOW;
  beginBusinessTurn(state);
  assert.equal(state.rapidRefillBreakerTripped, false, "a full window of requests releases the breaker");
  assert.equal(state.consecutiveRapidRefills, 0);
});

test("refill distance is model requests, not Paseo prompts: one long prompt per compact is normal work", () => {
  // Same prompt count either way; only the agy requests inside each prompt differ.
  const run = (requestsPerPrompt: number) => {
    const state = emptyCompactionState();
    const rows: Record<string, unknown>[] = [];
    for (let prompt = 0; prompt < 4; prompt++) {
      beginBusinessTurn(state);
      rows.push(flat(rows.length, "USER_INPUT", { content: "long task" }));
      for (let i = 0; i < requestsPerPrompt; i++) {
        rows.push(flat(rows.length, "PLANNER_RESPONSE", { input_tokens: 2_000, cache_read_tokens: 20_000 + i * 3_000 }));
      }
      reconcileNativeCompaction(state, observations(rows)); // the turn's result rereads the transcript
      const id = `turn-${prompt}:auto-compact`;
      beginCompact(state, id, "idle", T + rows.length * 1_000);
      rows.push(flat(rows.length, "USER_INPUT", { content: "/compact" }), flat(rows.length + 1, "CHECKPOINT"));
      reconcileNativeCompaction(state, observations(rows));
      finishCompact(state, id, true, T + rows.length * 1_000, 20_000);
    }
    return { state, rows };
  };
  const long = run(45);
  assert.equal(long.state.events.filter((e) => e.source === "idle" && e.nativeStep !== undefined).length, 4);
  assert.equal(long.state.modelRequests, 180);
  assert.equal(long.state.consecutiveRapidRefills, 0);
  assert.equal(long.state.rapidRefillBreakerTripped, false);
  assert.equal(run(3).state.rapidRefillBreakerTripped, true, "3 requests per refill is still thrashing");

  // A state stored by the prompt-distance build (3 refills, tripped) is cleared
  // on the next transcript reread.
  long.state.consecutiveRapidRefills = 3;
  long.state.rapidRefillBreakerTripped = true;
  long.rows.push(flat(long.rows.length, "USER_INPUT", { content: "next" }),
    ...Array.from({ length: 12 }, (_, i) => flat(long.rows.length + 1 + i, "PLANNER_RESPONSE", { input_tokens: 9_000 })));
  reconcileNativeCompaction(long.state, observations(long.rows));
  assert.equal(requestsSinceLastCompact(long.state), 12);
  assert.equal(long.state.rapidRefillBreakerTripped, false);
  assert.equal(long.state.consecutiveRapidRefills, 0);
});

test("unknown request counts never count as refills, and states from older builds load with them unknown", () => {
  const state = emptyCompactionState();
  for (let i = 0; i < 4; i++) {
    beginBusinessTurn(state);
    beginCompact(state, `idle-${i}`, "idle", T + i * 10_000);
    finishCompact(state, `idle-${i}`, true, T + i * 10_000 + 500, 10_000);
  }
  assert.equal(state.events.some((e) => e.requestIndex !== undefined), false);
  assert.equal(state.consecutiveRapidRefills, 0, "no transcript, no measured refill");
  assert.equal(state.rapidRefillBreakerTripped, false);

  const dir = mkdtempSync(path.join(tmpdir(), "agy-compact-upgrade-"));
  try {
    const store = new ConversationStore({ storeDir: dir, brainDir: dir });
    const { modelRequests: _added, ...older } = state;
    writeFileSync(store.compactionPath("conv"), JSON.stringify({ ...older, consecutiveCompactFailures: 2 }));
    const loaded = store.loadCompaction("conv");
    assert.equal(loaded.consecutiveCompactFailures, 2, "older file is accepted, not replaced by a timeline migration");
    assert.equal(loaded.modelRequests, -1);
    assert.equal(requestsSinceLastCompact(loaded), null);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("older states tripped only by native checkpoints are cleared on reconcile; a real blocked idle trip is kept", () => {
  // Mirrors a real conversation: three native checkpoints in turns 3, 4, 4, then
  // an older build blocked the next idle compact as the "3rd refill".
  const rows = [flat(0, "USER_INPUT", { content: "a" }), flat(1, "USER_INPUT", { content: "b" }),
    flat(2, "USER_INPUT", { content: "c" }), flat(103, "CHECKPOINT"), flat(133, "USER_INPUT", { content: "d" }),
    flat(198, "CHECKPOINT"), flat(344, "CHECKPOINT"), flat(373, "USER_INPUT", { content: "e" })];
  const stale = emptyCompactionState();
  reconcileNativeCompaction(stale, observations(rows));
  stale.consecutiveRapidRefills = 3;
  stale.rapidRefillBreakerTripped = true;
  reconcileNativeCompaction(stale, observations(rows));
  assert.equal(stale.rapidRefillBreakerTripped, false);
  assert.ok(stale.consecutiveRapidRefills <= 1);

  const real = emptyCompactionState();
  for (let i = 0; i < 3; i++) {
    beginBusinessTurn(real);
    real.modelRequests = i * 2;
    beginCompact(real, `idle-${i}`, "idle", T + i * 10_000);
    finishCompact(real, `idle-${i}`, true, T + i * 10_000 + 500, 10_000);
  }
  assert.equal(real.consecutiveRapidRefills, 2);
  real.consecutiveRapidRefills = 3; // the session blocked the next idle attempt
  real.rapidRefillBreakerTripped = true;
  reconcileNativeCompaction(real, []);
  assert.equal(real.rapidRefillBreakerTripped, true);
  assert.equal(real.consecutiveRapidRefills, 3);
});
