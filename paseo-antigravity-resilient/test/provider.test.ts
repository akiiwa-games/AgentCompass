import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import type {
  ProviderCatalog,
  ProviderEvent,
  ProviderHost,
  ProviderPrompt,
  ProviderSessionConfig,
} from "@getpaseo/plugin/server/provider";
import {
  evaluateRemoteSyncSafety,
  parsePorcelainStatus,
} from "../scripts/sync-safety.mjs";
import {
  DEFAULT_MODE,
  getCatalog,
  MODES,
  parseModels,
  resolveContextWindowMaxTokens,
  type ProviderDiscovery,
} from "../server/internal/catalog.ts";
import type { SpawnFunction } from "../server/internal/process.ts";
import {
  AUTOCOMPACT_THRASHING_MESSAGE,
  CONTEXT_OUTPUT_GUIDANCE,
  DEFAULT_AUTO_COMPACT_LEAD_MS,
  DEFAULT_AUTO_COMPACT_MAX_LATE_MS,
  DEFAULT_AUTO_COMPACT_TOKEN_THRESHOLD,
  DEFAULT_HOT_CACHE_TTL_MS,
  isCompactPromptText,
  MAX_CONSECUTIVE_COMPACT_FAILURES,
  MIN_ENV_AUTO_COMPACT_TOKEN_THRESHOLD,
  RAPID_REFILL_BREAKER_LIMIT,
  RAPID_REFILL_REQUEST_WINDOW,
  Session,
  TOKEN_GAP_CONTINUE_PROMPT,
} from "../server/internal/session.ts";
import { ProviderActionError } from "../server/internal/signals.ts";
import {
  ConversationStore,
  DEFAULT_FS_OPS,
} from "../server/internal/store.ts";
import {
  diagnostic,
  isAuthDiagnosticError,
  isStreamInterruption,
  isTokenGap,
} from "../server/internal/wire.ts";
import { antigravityResilientProvider, PROVIDER_ID } from "../server/provider.ts";
import {
  beginBusinessTurn, beginCompact, emptyCompactionState, finishCompact, POST_COMPACT_SUMMARY_TOKENS,
} from "../server/internal/compaction.ts";

class FakeChildProcess extends EventEmitter {
  readonly args: readonly string[];
  stdin = new PassThrough();
  stdout = new PassThrough();
  stderr = new PassThrough();
  exitCode: number | null = null;
  signalCode: string | null = null;
  killed = false;
  pid = 42424;

  constructor(
    args: readonly string[],
    onPrompt: (prompt: { role: string; content: string }, child: FakeChildProcess) => void,
  ) {
    super();
    this.args = args;
    this.stdin.setEncoding("utf8");
    let buffer = "";
    this.stdin.on("data", (chunk: string) => {
      buffer += chunk;
      while (buffer.includes("\n")) {
        const newline = buffer.indexOf("\n");
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) {
          onPrompt(JSON.parse(line) as { role: string; content: string }, this);
        }
      }
    });
  }

  sendFrame(frame: Record<string, unknown>): void {
    this.stdout.write(`${JSON.stringify(frame)}\n`);
  }

  kill(): boolean {
    if (this.exitCode === null && this.signalCode === null) {
      this.killed = true;
      this.exitCode = 0;
      this.stdout.end();
      this.stderr.end();
      this.emit("close", 0, null);
    }
    return true;
  }
}

const TEST_CATALOG: ProviderDiscovery = {
  models: [
    { id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)", isDefault: true },
  ],
  modes: MODES,
  defaultModel: "gemini-4-argon-high",
  defaultMode: DEFAULT_MODE,
};

function makeConfig(overrides: Partial<ProviderSessionConfig> = {}): ProviderSessionConfig {
  return {
    cwd: "/workspace/route_manager",
    env: { SECRET_KEY: "ya29.should_never_be_persisted_in_timeline_store" },
    mcpServers: {},
    model: "gemini-4-argon-high",
    mode: "full-access",
    settings: {},
    persist: true,
    ...overrides,
  };
}

function userPrompt(text: string, clientMessageId = "msg-1"): ProviderPrompt {
  return {
    clientMessageId,
    delivery: "auto",
    input: {
      type: "message",
      content: [{ type: "text", text }],
    },
  };
}

function waitForTurnEnd(
  events: ProviderEvent[],
): Promise<Extract<ProviderEvent, { type: "session.turn" }>> {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 3_000;
    const poll = () => {
      const done = events.find(
        (
          event,
        ): event is Extract<ProviderEvent, { type: "session.turn" }> =>
          event.type === "session.turn" &&
          (event.state === "completed" ||
            event.state === "failed" ||
            event.state === "canceled"),
      );
      if (done) {
        resolve(done);
        return;
      }
      if (Date.now() > deadline) {
        reject(
          new Error(
            `Timed out waiting for session.turn end; events=${JSON.stringify(events)}`,
          ),
        );
        return;
      }
      setTimeout(poll, 10);
    };
    poll();
  });
}

/** A native transcript holding the conversation's first model request, which
 * anchors the post-compact estimate (first request + summary). */
function writeFirstRequest(brainDir: string, conv: string, tokens: number): void {
  const logs = path.join(brainDir, conv, ".system_generated", "logs");
  mkdirSync(logs, { recursive: true });
  const at = new Date(Date.now() - 60_000).toISOString();
  writeFileSync(path.join(logs, "transcript.jsonl"), [
    { step_index: 0, type: "USER_INPUT", status: "DONE", created_at: at, content: "first request" },
    { step_index: 1, type: "PLANNER_RESPONSE", status: "DONE", created_at: at, input_tokens: tokens },
  ].map((row) => JSON.stringify(row)).join("\n") + "\n");
}

function usageSamples(events: ProviderEvent[]): Array<Extract<ProviderEvent, { type: "session.usage" }>> {
  return events.filter((e): e is Extract<ProviderEvent, { type: "session.usage" }> => e.type === "session.usage");
}

test("401 refresh-race before progress retries once with original prompt and succeeds", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-test-"));
  try {
    const events: ProviderEvent[] = [];
    const prompts: Array<{ args: readonly string[]; content: string }> = [];

    const spawn: SpawnFunction = (_command, args) => {
      const attempt = prompts.length + 1;
      const child = new FakeChildProcess(args, (prompt) => {
        prompts.push({ args, content: prompt.content });
        if (attempt === 1) {
          child.sendFrame({
            event: "result",
            conversation_id: "conv-401-no-progress",
            status: "error",
            errors: [
              "UNAUTHENTICATED (code 401): Request had invalid authentication credentials. Expected OAuth 2 access token, login cookie or other valid authentication credential.",
            ],
          });
          return;
        }
        child.sendFrame({
          event: "message",
          conversation_id: "conv-401-no-progress",
          role: "assistant",
          content: "Completed after token refresh",
        });
        child.sendFrame({
          event: "result",
          conversation_id: "conv-401-no-progress",
          status: "success",
          response: "Completed after token refresh",
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-401-no-progress",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "session-1",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir, tokenGapRetryDelayMs: 10 },
    );

    await session.prompt(userPrompt("Run the check"));
    const finalTurn = await waitForTurnEnd(events);

    assert.equal(finalTurn.state, "completed");
    assert.equal(prompts.length, 2);
    assert.equal(prompts[0]?.content, `${CONTEXT_OUTPUT_GUIDANCE}\n\nRun the check`);
    assert.equal(prompts[1]?.content, prompts[0]?.content);
    assert.ok(prompts[1]?.args.includes("--conversation"));
    assert.ok(prompts[1]?.args.includes("conv-401-no-progress"));
    assert.equal(
      events.some(
        (event) => event.type === "session.turn" && event.state === "failed",
      ),
      false,
    );
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("401 refresh-race after partial progress retries once with continuation prompt", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-test-"));
  try {
    const events: ProviderEvent[] = [];
    const prompts: string[] = [];

    const spawn: SpawnFunction = (_command, args) => {
      const attempt = prompts.length + 1;
      const child = new FakeChildProcess(args, (prompt) => {
        prompts.push(prompt.content);
        if (attempt === 1) {
          child.sendFrame({
            event: "message",
            conversation_id: "conv-401-progress",
            role: "assistant",
            content: "Step 1 done.",
          });
          child.sendFrame({
            event: "result",
            conversation_id: "conv-401-progress",
            status: "error",
            errors: [
              "pre-invocation hook: context summarization: UNAUTHENTICATED (code 401): Request had invalid authentication credentials.",
            ],
          });
          return;
        }
        child.sendFrame({
          event: "message",
          conversation_id: "conv-401-progress",
          role: "assistant",
          content: "Step 2 done.",
        });
        child.sendFrame({
          event: "result",
          conversation_id: "conv-401-progress",
          status: "success",
          response: "Step 2 done.",
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-401-progress",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "session-2",
      "agy",
      [],
      {},
      makeConfig(),
      { version: 1, data: { conversationId: "conv-401-progress" } },
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir, tokenGapRetryDelayMs: 10 },
    );

    await session.prompt(userPrompt("Do two steps"));
    const finalTurn = await waitForTurnEnd(events);

    assert.equal(finalTurn.state, "completed");
    assert.deepEqual(prompts, [`${CONTEXT_OUTPUT_GUIDANCE}\n\nDo two steps`, TOKEN_GAP_CONTINUE_PROMPT]);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("repeated 401 fails once after single retry without infinite loop", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-test-"));
  try {
    const events: ProviderEvent[] = [];
    let spawns = 0;

    const spawn: SpawnFunction = (_command, args) => {
      spawns += 1;
      const child = new FakeChildProcess(args, () => {
        child.sendFrame({
          event: "result",
          conversation_id: "conv-401-twice",
          status: "error",
          errors: [
            "UNAUTHENTICATED (code 401): Request had invalid authentication credentials.",
          ],
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-401-twice",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "session-3",
      "agy",
      [],
      {},
      makeConfig(),
      { version: 1, data: { conversationId: "conv-401-twice" } },
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir, tokenGapRetryDelayMs: 10 },
    );

    await session.prompt(userPrompt("Check auth"));
    const finalTurn = await waitForTurnEnd(events);

    assert.equal(spawns, 2);
    assert.equal(finalTurn.state, "failed");
    assert.match(
      finalTurn.error?.message ?? "",
      /Antigravity authentication failed/,
    );
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("narrowed diagnostic does not rewrite non-auth errors containing 'author' or 'auth'", async () => {
  assert.equal(
    isAuthDiagnosticError("Invalid author field in package.json"),
    false,
  );
  assert.equal(
    diagnostic("Invalid author field in package.json"),
    "Invalid author field in package.json",
  );
  assert.equal(
    diagnostic("You are not logged into Antigravity."),
    "Antigravity authentication failed. Run `agy` and sign in, then retry.",
  );
  assert.equal(
    isTokenGap(
      'GetToken failed: oauth2: cannot fetch token: Post "https://oauth2.googleapis.com/token": net/http: TLS handshake timeout',
    ),
    true,
  );
  assert.equal(
    isStreamInterruption(
      "The stream was interrupted. Please continue the task you were working on.",
    ),
    true,
  );
});

test("sticky stream-interruption error on a completed turn is ignored instead of failing the turn", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-test-"));
  try {
    const events: ProviderEvent[] = [];
    let spawns = 0;

    const spawn: SpawnFunction = (_command, args) => {
      spawns += 1;
      const child = new FakeChildProcess(args, () => {
        child.sendFrame({
          event: "message",
          conversation_id: "conv-sticky-stream",
          role: "assistant",
          content: "Full response completed after internal retry.",
        });
        child.sendFrame({
          event: "result",
          conversation_id: "conv-sticky-stream",
          status: "error",
          errors: [
            "The stream was interrupted. Please continue the task you were working on.",
          ],
          response: "Full response completed after internal retry.",
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-sticky-stream",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "session-sticky",
      "agy",
      [],
      {},
      makeConfig(),
      { version: 1, data: { conversationId: "conv-sticky-stream" } },
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir },
    );

    await session.prompt(userPrompt("Render scene"));
    const finalTurn = await waitForTurnEnd(events);

    assert.equal(finalTurn.state, "completed");
    assert.equal(spawns, 1);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("unrecovered mid-turn stream interruption retries once and succeeds even if trajectory error remains sticky", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-test-"));
  try {
    const events: ProviderEvent[] = [];
    const prompts: string[] = [];

    const spawn: SpawnFunction = (_command, args) => {
      const attempt = prompts.length + 1;
      const child = new FakeChildProcess(args, (prompt) => {
        prompts.push(prompt.content);
        if (attempt === 1) {
          child.sendFrame({
            event: "thinking",
            conversation_id: "conv-mid-interrupt",
            content: "Planning scene...",
          });
          child.sendFrame({
            event: "result",
            conversation_id: "conv-mid-interrupt",
            status: "error",
            errors: [
              "The stream was interrupted. Please continue the task you were working on.",
            ],
          });
          return;
        }
        child.sendFrame({
          event: "message",
          conversation_id: "conv-mid-interrupt",
          role: "assistant",
          content: "Recovered and finished.",
        });
        child.sendFrame({
          event: "result",
          conversation_id: "conv-mid-interrupt",
          status: "error",
          errors: [
            "The stream was interrupted. Please continue the task you were working on.",
          ],
          response: "Recovered and finished.",
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-mid-interrupt",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "session-interrupt",
      "agy",
      [],
      {},
      makeConfig(),
      { version: 1, data: { conversationId: "conv-mid-interrupt" } },
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir, streamInterruptRetryDelayMs: 10 },
    );

    await session.prompt(userPrompt("Build scene"));
    const finalTurn = await waitForTurnEnd(events);

    assert.equal(finalTurn.state, "completed");
    assert.deepEqual(prompts, [`${CONTEXT_OUTPUT_GUIDANCE}\n\nBuild scene`, `${CONTEXT_OUTPUT_GUIDANCE}\n\nBuild scene`]);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("OAuth recovery never marks unrelated result errors as recovered", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-oauth-mixed-"));
  let session: Session | undefined;
  try {
    const events: ProviderEvent[] = [];
    let attempts = 0;
    const spawn: SpawnFunction = (_command, args) => {
      const child = new FakeChildProcess(args, () => {
        attempts++;
        child.sendFrame({
          event: "result", status: "error", response: "Partial output",
          errors: ["UNAUTHENTICATED (code 401)", "Unrelated fatal error"],
        });
      });
      queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: "conv-oauth-mixed" }));
      return child as unknown as ReturnType<SpawnFunction>;
    };
    session = new Session("session-oauth-mixed", "agy", [], {}, makeConfig(),
      { version: 1, data: { conversationId: "conv-oauth-mixed" } }, TEST_CATALOG,
      (event) => events.push(event), { spawn, storeDir: tempDir, tokenGapRetryDelayMs: 1 });
    await session.prompt(userPrompt("Finish task"));
    assert.equal((await waitForTurnEnd(events)).state, "failed");
    assert.equal(attempts, 2);
  } finally {
    await session?.close("test-cleanup");
    rmSync(tempDir, { recursive: true, force: true });
  }
});

const IDLE_TIMEOUT_ERROR =
  "API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded";

test("idle timeout classification does not swallow unrelated deadline or tool errors", () => {
  assert.equal(isStreamInterruption(IDLE_TIMEOUT_ERROR), true);
  assert.equal(isStreamInterruption("context deadline exceeded"), false);
  assert.equal(isStreamInterruption("Tool execution timed out"), false);
  assert.equal(isAuthDiagnosticError(IDLE_TIMEOUT_ERROR), false);
});

for (const scenario of [
  { name: "legacy sticky result", response: true, nested: false, progress: false, recover: false, mixed: false, expected: "completed", attempts: 1 },
  { name: "nested sticky result", response: true, nested: true, progress: false, recover: false, mixed: false, expected: "completed", attempts: 1 },
  { name: "interruption without progress", response: false, nested: true, progress: false, recover: true, mixed: false, expected: "completed", attempts: 2 },
  { name: "interruption after progress", response: false, nested: true, progress: true, recover: true, mixed: false, expected: "completed", attempts: 2 },
  { name: "persistent timeout stops after one retry", response: false, nested: true, progress: false, recover: false, mixed: false, expected: "failed", attempts: 2 },
  { name: "unrelated result error is not hidden", response: true, nested: false, progress: false, recover: false, mixed: true, expected: "failed", attempts: 2 },
]) {
  test(`idle timeout: ${scenario.name}`, async () => {
    const tempDir = mkdtempSync(path.join(tmpdir(), "agy-idle-timeout-test-"));
    let session: Session | undefined;
    try {
      const events: ProviderEvent[] = [];
      const prompts: string[] = [];
      const children: FakeChildProcess[] = [];
      const spawn: SpawnFunction = (_command, args) => {
        const child = new FakeChildProcess(args, (prompt) => {
          prompts.push(prompt.content);
          if (scenario.progress) {
            child.sendFrame({ event: "message", role: "assistant", content: "Partial progress" });
          }
          const response = scenario.response || (scenario.recover && prompts.length > 1)
            ? "Completed after CLI recovery."
            : "";
          child.sendFrame(scenario.nested ? {
            event: "result",
            result: { status: "ERROR", error: IDLE_TIMEOUT_ERROR, response },
          } : {
            event: "result",
            status: "error",
            errors: scenario.mixed ? [IDLE_TIMEOUT_ERROR, "Unrelated fatal error"] : [IDLE_TIMEOUT_ERROR],
            response,
          });
        });
        children.push(child);
        queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: "conv-idle-timeout" }));
        return child as unknown as ReturnType<SpawnFunction>;
      };
      session = new Session(
        "session-idle-timeout", "agy", [], {}, makeConfig(),
        { version: 1, data: { conversationId: "conv-idle-timeout" } },
        TEST_CATALOG, (event) => events.push(event),
        { spawn, storeDir: tempDir, streamInterruptRetryDelayMs: 1 },
      );
      await session.prompt(userPrompt("Finish task"));
      const finalTurn = await waitForTurnEnd(events);
      assert.equal(finalTurn.state, scenario.expected);
      assert.equal(children.length, scenario.attempts);
      if (scenario.attempts === 2) {
        assert.equal(children[0].killed, true);
        assert.equal(prompts[1], scenario.progress ? TOKEN_GAP_CONTINUE_PROMPT : `${CONTEXT_OUTPUT_GUIDANCE}\n\nFinish task`);
      }
    } finally {
      await session?.close("test-cleanup");
      rmSync(tempDir, { recursive: true, force: true });
    }
  });
}

test("durable timeline persistence, replay before session.ready, multi-turn ID continuity, and corruption recovery", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-store-"));
  try {
    const turn1Events: ProviderEvent[] = [];

    const spawnTurn1: SpawnFunction = (_command, args) => {
      const child = new FakeChildProcess(args, () => {
        child.sendFrame({
          event: "thinking",
          conversation_id: "conv-replay-1",
          content: "Thinking chunk 1. ",
          timestamp: "2026-10-15T04:00:01.000Z",
        });
        child.sendFrame({
          event: "thinking",
          conversation_id: "conv-replay-1",
          content: "Thinking chunk 2.",
          timestamp: "2026-10-15T04:00:02.000Z",
        });
        child.sendFrame({
          event: "tool_call",
          conversation_id: "conv-replay-1",
          id: "call-1",
          name: "run_command",
          arguments: { CommandLine: "echo hello", Cwd: "/workspace/route_manager" },
          timestamp: "2026-10-15T04:00:03.000Z",
        });
        child.sendFrame({
          event: "tool_result",
          conversation_id: "conv-replay-1",
          id: "call-1",
          name: "run_command",
          content: "hello token ya29.secret_oauth_token_1234567890abcdef",
          is_error: false,
          timestamp: "2026-10-15T04:00:04.000Z",
        });
        child.sendFrame({
          event: "message",
          conversation_id: "conv-replay-1",
          role: "assistant",
          content: "Hello ",
          timestamp: "2026-10-15T04:00:05.000Z",
        });
        child.sendFrame({
          event: "message",
          conversation_id: "conv-replay-1",
          role: "assistant",
          content: "world!",
          timestamp: "2026-10-15T04:00:06.000Z",
        });
        child.sendFrame({
          event: "result",
          conversation_id: "conv-replay-1",
          status: "success",
          response: "Hello world!",
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-replay-1",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session1 = new Session(
      "s-1",
      "agy",
      [],
      { SECRET_ENV: "ya29.env_token_should_not_leak_1234567890" },
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (event) => turn1Events.push(event),
      { spawn: spawnTurn1, storeDir: tempDir },
    );
    session1.open("req-open-1", ["prompt.message", "session.persistence"], "skip");
    await session1.prompt(userPrompt("First user message", "client-msg-1"));
    const turn1End = await waitForTurnEnd(turn1Events);
    assert.equal(turn1End.state, "completed");

    // Verify snapshot file exists on disk, journal is compacted after turn completion,
    // and no credentials/env are leaked
    const snapshotPath = path.join(tempDir, "conv-replay-1.json");
    const journalPath = path.join(tempDir, "conv-replay-1.jsonl");
    assert.equal(existsSync(snapshotPath), true);
    assert.equal(readFileSync(journalPath, "utf8").trim(), "");
    const rawSnapshot = readFileSync(snapshotPath, "utf8");
    assert.doesNotMatch(rawSnapshot, /ya29\./);
    assert.doesNotMatch(rawSnapshot, /SECRET_KEY/);
    assert.doesNotMatch(rawSnapshot, /SECRET_ENV/);
    assert.match(rawSnapshot, /<REDACTED_OAUTH_TOKEN>/);

    // Simulate daemon restart / plugin reload: open Session 2 with history="replay"
    const replayEvents: ProviderEvent[] = [];
    const session2 = new Session(
      "s-2",
      "agy",
      [],
      {},
      makeConfig(),
      { version: 1, data: { conversationId: "conv-replay-1" } },
      TEST_CATALOG,
      (event) => replayEvents.push(event),
      { storeDir: tempDir },
    );
    session2.open(
      "req-open-2",
      ["prompt.message", "session.persistence"],
      "replay",
    );

    const openedIdx = replayEvents.findIndex((e) => e.type === "session.opened");
    const readyIdx = replayEvents.findIndex((e) => e.type === "session.ready");
    const timelineEvents = replayEvents
      .map((event, index) => ({ event, index }))
      .filter(
        (
          entry,
        ): entry is {
          event: Extract<ProviderEvent, { type: "timeline.item" }>;
          index: number;
        } => entry.event.type === "timeline.item",
      );

    assert.equal(timelineEvents.length, 4);
    for (const entry of timelineEvents) {
      assert.ok(
        entry.index > openedIdx && entry.index < readyIdx,
        "Replayed timeline.item must be emitted after session.opened and before session.ready",
      );
    }

    assert.deepEqual(
      timelineEvents.map((entry) => ({
        id: entry.event.item.id,
        type: entry.event.item.type,
      })),
      [
        { id: "user:client-msg-1", type: "user_message" },
        { id: "turn-1:reasoning:1", type: "reasoning" },
        { id: "tool:call-1", type: "tool_call" },
        { id: "turn-1:message:1", type: "assistant_message" },
      ],
    );
    const replayedAssistant = timelineEvents[3]?.event.item;
    assert.equal(
      replayedAssistant?.type === "assistant_message"
        ? replayedAssistant.text
        : "",
      "Hello world!",
    );
    const replayedTool = timelineEvents[2]?.event.item;
    assert.equal(
      replayedTool?.type === "tool_call" ? replayedTool.status : "",
      "completed",
    );

    // Perform a second flush so .json.bak holds the 4-item snapshot, then corrupt
    // the primary .json snapshot and verify recovery from .json.bak
    session2.getStore().upsert("conv-replay-1", {
      id: "turn-2:message:1",
      type: "assistant_message",
      text: "Second turn reply",
    });
    session2.getStore().flush("conv-replay-1");
    writeFileSync(snapshotPath, "{corrupted json", "utf8");
    const recoveredStore = new ConversationStore({ storeDir: tempDir });
    const recoveredItems = recoveredStore.load("conv-replay-1");
    assert.equal(recoveredItems.length, 4);
    assert.equal(recoveredItems[0]?.id, "user:client-msg-1");
    assert.equal(recoveredItems[3]?.id, "turn-1:message:1");
    // Primary snapshot should now be healed on disk
    const healedParsed = JSON.parse(readFileSync(snapshotPath, "utf8")) as {
      version: number;
      items: unknown[];
    };
    assert.equal(healedParsed.version, 1);
    assert.equal(healedParsed.items.length, 4);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("streaming updates append only to journal until flush/compaction, recover before compaction, and auto-compact when exceeding threshold", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-journal-"));
  try {
    let snapshotTmpWrites = 0;
    let journalAppends = 0;

    const store = new ConversationStore({
      storeDir: tempDir,
      maxJournalEntriesBeforeCompact: 100,
      fsOps: {
        appendFileSync(filePath, data, options) {
          journalAppends += 1;
          return DEFAULT_FS_OPS.appendFileSync(filePath, data, options);
        },
        writeFileSync(filePath, data, options) {
          const pathStr = String(filePath);
          if (
            pathStr.endsWith(".tmp") &&
            !pathStr.endsWith(".journal.tmp") &&
            !pathStr.endsWith(".bak.tmp")
          ) {
            snapshotTmpWrites += 1;
          }
          return DEFAULT_FS_OPS.writeFileSync(filePath, data, options);
        },
      },
    });

    const convId = "conv-streaming-perf";
    store.upsert(
      convId,
      { id: "user:msg-1", type: "user_message", text: "Stream 40 deltas" },
      "2026-10-15T05:00:00.000Z",
    );

    let accumulated = "";
    for (let i = 1; i <= 40; i += 1) {
      accumulated += `token-${i} `;
      store.upsert(
        convId,
        {
          id: "turn-1:message:1",
          type: "assistant_message",
          text: accumulated,
        },
        `2026-10-15T05:00:${String(i).padStart(2, "0")}.000Z`,
      );
    }

    // 41 updates should have appended to .jsonl 41 times with ZERO full-snapshot writes
    assert.equal(journalAppends, 41);
    assert.equal(snapshotTmpWrites, 0);
    assert.equal(existsSync(store.snapshotPath(convId)), false);
    assert.equal(store.uncompactedJournalCount(convId), 41);

    // Simulate a crash before flush(): a fresh ConversationStore must recover all 41
    // updates from the .jsonl journal, heal the .json snapshot, and compact .jsonl!
    const crashedRecoveryStore = new ConversationStore({ storeDir: tempDir });
    const recovered = crashedRecoveryStore.load(convId);
    assert.equal(recovered.length, 2);
    assert.equal(recovered[0]?.id, "user:msg-1");
    assert.equal(recovered[1]?.id, "turn-1:message:1");
    assert.equal(
      recovered[1]?.item.type === "assistant_message"
        ? recovered[1].item.text
        : "",
      accumulated,
    );
    assert.equal(existsSync(crashedRecoveryStore.snapshotPath(convId)), true);
    assert.equal(
      readFileSync(crashedRecoveryStore.journalPath(convId), "utf8").trim(),
      "",
    );
    assert.equal(crashedRecoveryStore.uncompactedJournalCount(convId), 0);

    // Also verify bounded auto-compaction when streaming updates exceed maxJournalEntriesBeforeCompact
    const boundedStore = new ConversationStore({
      storeDir: tempDir,
      maxJournalEntriesBeforeCompact: 10,
    });
    const boundedConvId = "conv-bounded-journal";
    for (let i = 1; i <= 25; i += 1) {
      boundedStore.upsert(boundedConvId, {
        id: "turn-1:message:1",
        type: "assistant_message",
        text: `chunk-${i}`,
      });
    }
    // 25 updates with threshold 10 compacts at 10 and 20, leaving 5 uncompacted entries
    assert.equal(boundedStore.uncompactedJournalCount(boundedConvId), 5);
    assert.equal(existsSync(boundedStore.snapshotPath(boundedConvId)), true);
    boundedStore.flush(boundedConvId);
    assert.equal(boundedStore.uncompactedJournalCount(boundedConvId), 0);
    assert.equal(
      readFileSync(boundedStore.journalPath(boundedConvId), "utf8").trim(),
      "",
    );
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("Windows-safe atomic replacement never overwrites primary in-place and preserves valid snapshot + journal on rename failure", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-atomic-"));
  try {
    const convId = "conv-atomic-fault";
    const snapPath = path.join(tempDir, `${convId}.json`);
    const bakPath = path.join(tempDir, `${convId}.json.bak`);

    let failDirectOverwriteRename = false;
    let failAllSnapRenames = false;
    const directSnapWrites: string[] = [];

    const store = new ConversationStore({
      storeDir: tempDir,
      fsOps: {
        writeFileSync(filePath, data, options) {
          const target = String(filePath);
          if (target === snapPath || target === bakPath) {
            directSnapWrites.push(target);
          }
          return DEFAULT_FS_OPS.writeFileSync(filePath, data, options);
        },
        renameSync(oldPath, newPath) {
          const src = String(oldPath);
          const dst = String(newPath);
          if (dst === snapPath && src.endsWith(".tmp") && !src.endsWith(".snap.prev.tmp")) {
            if (failAllSnapRenames) {
              throw Object.assign(new Error("Simulated EIO replacing snapshot"), {
                code: "EIO",
              });
            }
            if (failDirectOverwriteRename && existsSync(snapPath)) {
              throw Object.assign(
                new Error("Simulated Windows EPERM on existing destination"),
                { code: "EPERM" },
              );
            }
          }
          return DEFAULT_FS_OPS.renameSync(oldPath, newPath);
        },
      },
    });

    // Turn 1: initial flush creates conv-atomic-fault.json
    store.upsert(convId, {
      id: "user:1",
      type: "user_message",
      text: "Turn 1 user",
    });
    assert.equal(store.flush(convId), true);
    assert.equal(existsSync(snapPath), true);

    // Turn 2: simulate Windows blocking direct rename over existing snapPath,
    // forcing the 2-step displaced move (snapPath -> prev.tmp, tmp -> snapPath)
    failDirectOverwriteRename = true;
    store.upsert(convId, {
      id: "turn-1:message:1",
      type: "assistant_message",
      text: "Turn 1 assistant",
    });
    assert.equal(store.flush(convId), true);
    assert.equal(directSnapWrites.length, 0);
    const afterTwoStep = JSON.parse(readFileSync(snapPath, "utf8")) as {
      items: Array<{ id: string }>;
    };
    assert.equal(afterTwoStep.items.length, 2);
    assert.equal(existsSync(bakPath), true);

    // Turn 3: simulate total failure of renaming tmp -> snapPath even after moving
    // snapPath -> prev.tmp. Rollback must restore snapPath to the 2-item snapshot,
    // never call writeFileSync(snapPath), and keep the 3rd item in .jsonl!
    failAllSnapRenames = true;
    store.upsert(convId, {
      id: "user:2",
      type: "user_message",
      text: "Turn 2 user",
    });
    assert.equal(store.flush(convId), false);
    assert.equal(directSnapWrites.length, 0);

    // Primary snapshot must still be valid and contain the 2 items from Turn 2
    const preservedSnap = JSON.parse(readFileSync(snapPath, "utf8")) as {
      items: Array<{ id: string }>;
    };
    assert.equal(preservedSnap.items.length, 2);

    // Journal was NOT compacted because flush() failed, so a fresh store recovers all 3 items!
    const recoveredStore = new ConversationStore({ storeDir: tempDir });
    const allItems = recoveredStore.load(convId);
    assert.equal(allItems.length, 3);
    assert.deepEqual(
      allItems.map((entry) => entry.id),
      ["user:1", "turn-1:message:1", "user:2"],
    );
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("provider exposes only full-access mode and rejects unverified modes", async () => {
  assert.deepEqual(
    MODES.map((mode) => mode.id),
    ["full-access"],
  );
  assert.equal(DEFAULT_MODE, "full-access");

  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-modes-"));
  try {
    assert.throws(
      () =>
        new Session(
          "s-invalid-mode",
          "agy",
          [],
          {},
          makeConfig({ mode: "read-only" }),
          undefined,
          TEST_CATALOG,
          () => undefined,
          { storeDir: tempDir },
        ),
      (err: unknown) =>
        err instanceof ProviderActionError &&
        err.code === "INVALID_MODE" &&
        /Unknown Antigravity mode: read-only/.test(err.message),
    );

    const session = new Session(
      "s-valid-mode",
      "agy",
      [],
      {},
      makeConfig({ mode: "full-access" }),
      undefined,
      TEST_CATALOG,
      () => undefined,
      { storeDir: tempDir },
    );

    await assert.rejects(
      () =>
        session.configure(
          "req-cfg-1",
          { mode: "auto" },
          async (model) => model,
        ),
      (err: unknown) =>
        err instanceof ProviderActionError &&
        err.code === "INVALID_MODE" &&
        /Unknown Antigravity mode: auto/.test(err.message),
    );
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("remote git sync safety evaluator enforces fail-closed fast-forward rules", () => {
  const parsed = parsePorcelainStatus(
    " M setup/router_gate_hook.py\n?? scratch/notes.txt\n?? paseo-antigravity-resilient/scripts/sync-safety.mjs\n",
  );
  assert.deepEqual(parsed.trackedDirty, [
    { status: "M", path: "setup/router_gate_hook.py" },
  ]);
  assert.deepEqual(parsed.untracked, [
    "scratch/notes.txt",
    "paseo-antigravity-resilient/scripts/sync-safety.mjs",
  ]);

  // 1. Clean fast-forward on main
  const cleanFf = evaluateRemoteSyncSafety({
    branch: "main",
    remoteHead: "0928fda990e368e015ac5afe4d69f952df5f95ea",
    localHead: "1111111111111111111111111111111111111111",
    isAncestor: true,
    porcelainStatus: "?? scratch/unrelated-local-note.txt\n",
    incomingChangedFiles: [
      "paseo-antigravity-resilient/server/internal/store.ts",
    ],
  });
  assert.equal(cleanFf.safe, true);
  assert.equal(cleanFf.action, "fast-forward");
  assert.deepEqual(cleanFf.nonCollidingUntracked, [
    "scratch/unrelated-local-note.txt",
  ]);

  // 2. Already up-to-date (noop)
  const noop = evaluateRemoteSyncSafety({
    branch: "main",
    remoteHead: "0928fda990e368e015ac5afe4d69f952df5f95ea",
    localHead: "0928fda990e368e015ac5afe4d69f952df5f95ea",
    isAncestor: true,
    porcelainStatus: "",
    incomingChangedFiles: [],
  });
  assert.equal(noop.safe, true);
  assert.equal(noop.action, "noop");

  // 3. Refuse when tracked files are dirty
  const dirtyTracked = evaluateRemoteSyncSafety({
    branch: "main",
    remoteHead: "0928fda990e368e015ac5afe4d69f952df5f95ea",
    localHead: "1111111111111111111111111111111111111111",
    isAncestor: true,
    porcelainStatus: " M paseo-antigravity-resilient/README.md\n",
    incomingChangedFiles: ["paseo-antigravity-resilient/README.md"],
  });
  assert.equal(dirtyTracked.safe, false);
  assert.equal(dirtyTracked.action, "refuse");
  assert.ok(
    dirtyTracked.reasons.some((r) => /uncommitted tracked change/.test(r)),
  );

  // 4. Refuse when untracked file would be overwritten by incoming changes
  const collidingUntracked = evaluateRemoteSyncSafety({
    branch: "main",
    remoteHead: "0928fda990e368e015ac5afe4d69f952df5f95ea",
    localHead: "1111111111111111111111111111111111111111",
    isAncestor: true,
    porcelainStatus:
      "?? paseo-antigravity-resilient/scripts/sync-safety.mjs\n",
    incomingChangedFiles: [
      "paseo-antigravity-resilient/scripts/sync-safety.mjs",
    ],
  });
  assert.equal(collidingUntracked.safe, false);
  assert.equal(collidingUntracked.action, "refuse");
  assert.deepEqual(collidingUntracked.collidingUntracked, [
    "paseo-antigravity-resilient/scripts/sync-safety.mjs",
  ]);

  // 5. Refuse when remote history has diverged
  const diverged = evaluateRemoteSyncSafety({
    branch: "main",
    remoteHead: "2222222222222222222222222222222222222222",
    localHead: "1111111111111111111111111111111111111111",
    isAncestor: false,
    porcelainStatus: "",
    incomingChangedFiles: [],
  });
  assert.equal(diverged.safe, false);
  assert.equal(diverged.action, "refuse");
  assert.ok(diverged.reasons.some((r) => /diverged/.test(r)));

  // 6. Refuse when remote checkout is not on main
  const wrongBranch = evaluateRemoteSyncSafety({
    branch: "feature/experimental",
    remoteHead: "0928fda990e368e015ac5afe4d69f952df5f95ea",
    localHead: "1111111111111111111111111111111111111111",
    isAncestor: true,
    porcelainStatus: "",
    incomingChangedFiles: [],
  });
  assert.equal(wrongBranch.safe, false);
  assert.equal(wrongBranch.action, "refuse");
  assert.ok(wrongBranch.reasons.some((r) => /expected "main"/.test(r)));
});

test("native transcript.jsonl fallback seeds replay when store has no prior snapshot", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-native-"));
  try {
    const storeDir = path.join(tempDir, "store");
    const brainDir = path.join(tempDir, "brain");
    const convId = "conv-native-fallback";
    const logsDir = path.join(brainDir, convId, ".system_generated", "logs");
    mkdirSync(logsDir, { recursive: true });
    writeFileSync(
      path.join(logsDir, "transcript.jsonl"),
      [
        JSON.stringify({
          step_index: 0,
          type: "USER_INPUT",
          status: "DONE",
          created_at: "2026-10-15T01:00:00Z",
          content: "<USER_REQUEST>\nCheck status\n</USER_REQUEST>",
        }),
        JSON.stringify({
          step_index: 1,
          type: "PLANNER_RESPONSE",
          status: "DONE",
          created_at: "2026-10-15T01:00:02Z",
          thinking: "Checking status...",
          content: "All systems operational.",
        }),
      ].join("\n"),
      "utf8",
    );

    const store = new ConversationStore({ storeDir, brainDir });
    const records = store.load(convId);
    assert.equal(records.length, 3);
    assert.equal(records[0]?.item.type, "user_message");
    assert.equal(
      records[0]?.item.type === "user_message" ? records[0].item.text : "",
      "Check status",
    );
    assert.equal(records[1]?.item.type, "reasoning");
    assert.equal(records[2]?.item.type, "assistant_message");
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("catalog discovery filters banner lines, retries transient 503/timeout, and re-probes on unknown model", async () => {
  const parsed = parseModels(
    "Fetching available models...\ngemini-4-argon-medium\tGemini 4 Argon (Medium)\ngemini-4-argon-high\tGemini 4 Argon (High)\n",
  );
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0]?.id, "gemini-4-argon-medium");
  assert.equal(parsed[1]?.id, "gemini-4-argon-high");

  let cachedCatalog: ProviderCatalog | null = null;
  const host: ProviderHost = {
    async readCatalogCache() {
      return cachedCatalog;
    },
    async writeCatalogCache(_key, catalog) {
      cachedCatalog = catalog;
    },
  };

  let probeCalls = 0;
  const catalog = await getCatalog(host, "agy", [], {}, {
    retryDelayMs: 5,
    probeImpl: async () => {
      probeCalls += 1;
      if (probeCalls === 1) {
        return {
          exitCode: 1,
          stdout: "",
          stderr:
            "Eligibility check failed: UNAVAILABLE (code 503): No capacity available",
        };
      }
      return {
        exitCode: 0,
        stdout:
          "Fetching available models...\ngemini-4-argon-medium\tGemini 4 Argon (Medium)\n",
        stderr: "",
      };
    },
  });

  assert.equal(probeCalls, 2);
  assert.equal(catalog.models.length, 1);
  assert.equal(catalog.models[0]?.id, "gemini-4-argon-medium");
  assert.deepEqual(
    catalog.modes.map((m) => m.id),
    ["full-access"],
  );
  assert.equal(antigravityResilientProvider.id, PROVIDER_ID);
});

test("new persistent session emits valid sessionKey persistence on session.opened and resolves bound conversationId on resume", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-sessionkey-"));
  try {
    const initialEvents: ProviderEvent[] = [];
    const spawnTurn: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (_prompt, proc) => {
        proc.sendFrame({
          event: "message",
          conversation_id: "conv-bound-from-key",
          role: "assistant",
          content: "Bound reply",
          timestamp: "2026-10-15T06:00:01.000Z",
        });
        proc.sendFrame({
          event: "result",
          conversation_id: "conv-bound-from-key",
          status: "success",
          response: "Bound reply",
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-bound-from-key",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session1 = new Session(
      "sess-new-1",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (event) => initialEvents.push(event),
      { spawn: spawnTurn, storeDir: tempDir },
    );
    session1.open("req-open-new", ["prompt.message", "session.persistence"], "skip");

    const openedEvent = initialEvents.find(
      (e): e is Extract<ProviderEvent, { type: "session.opened" }> =>
        e.type === "session.opened",
    );
    assert.ok(openedEvent?.persistence, "session.opened must include persistence even before first turn");
    assert.deepEqual(openedEvent?.persistence, {
      version: 1,
      data: { sessionKey: "sess-new-1" },
    });

    // Resuming from the initial sessionKey-only handle BEFORE any turn starts must succeed cleanly
    const preTurnReplayEvents: ProviderEvent[] = [];
    const preTurnResumed = new Session(
      "sess-resumed-pre-turn",
      "agy",
      [],
      {},
      makeConfig(),
      openedEvent.persistence,
      TEST_CATALOG,
      (event) => preTurnReplayEvents.push(event),
      { storeDir: tempDir },
    );
    preTurnResumed.open("req-open-pre", ["prompt.message", "session.persistence"], "replay");
    assert.ok(preTurnReplayEvents.some((e) => e.type === "session.ready"));

    // Now run Turn 1 on session1 so agy emits conv-bound-from-key
    await session1.prompt(userPrompt("Hello via sessionKey", "client-key-1"));
    const turnEnd = await waitForTurnEnd(initialEvents);
    assert.equal(turnEnd.state, "completed");

    // Resuming using ONLY the initial sessionKey persistence handle (as happens if Paseo
    // saved agent.persistence at session creation before Turn 1 finalized) must resolve
    // conv-bound-from-key from disk and replay the full transcript!
    const postTurnReplayEvents: ProviderEvent[] = [];
    const postTurnResumed = new Session(
      "sess-resumed-post-turn",
      "agy",
      [],
      {},
      makeConfig(),
      openedEvent.persistence,
      TEST_CATALOG,
      (event) => postTurnReplayEvents.push(event),
      { storeDir: tempDir },
    );
    postTurnResumed.open("req-open-post", ["prompt.message", "session.persistence"], "replay");

    const replayedItems = postTurnReplayEvents.filter(
      (e): e is Extract<ProviderEvent, { type: "timeline.item" }> =>
        e.type === "timeline.item",
    );
    assert.equal(replayedItems.length, 2);
    assert.equal(replayedItems[0]?.item.type, "user_message");
    assert.equal(replayedItems[1]?.item.type, "assistant_message");
    assert.equal(
      replayedItems[1]?.item.type === "assistant_message"
        ? replayedItems[1].item.text
        : "",
      "Bound reply",
    );
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("pre-cache-expiry auto-compaction triggers 1m before 5m hot cache TTL only when >=150k tokens and not yet compacted", async () => {
  assert.equal(DEFAULT_HOT_CACHE_TTL_MS, 300_000);
  assert.equal(DEFAULT_AUTO_COMPACT_LEAD_MS, 60_000);
  assert.equal(DEFAULT_AUTO_COMPACT_MAX_LATE_MS, 60_000);
  assert.equal(DEFAULT_AUTO_COMPACT_TOKEN_THRESHOLD, 150_000);
  assert.equal(MIN_ENV_AUTO_COMPACT_TOKEN_THRESHOLD, 100_000);
  assert.equal(RAPID_REFILL_REQUEST_WINDOW, 10);
  assert.equal(RAPID_REFILL_BREAKER_LIMIT, 3);
  assert.equal(MAX_CONSECUTIVE_COMPACT_FAILURES, 3);
  assert.equal(isCompactPromptText("/compact"), true);
  assert.equal(isCompactPromptText("  /compact focus on auth"), true);
  assert.equal(isCompactPromptText("/compactness"), false);

  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-autocompact-"));
  try {
    const events: ProviderEvent[] = [];
    const invocations: Array<{ args: readonly string[]; content: string }> = [];
    let spawns = 0;

    const spawn: SpawnFunction = (_cmd, args) => {
      spawns += 1;
      const child = new FakeChildProcess(args, (prompt, proc) => {
        invocations.push({ args: [...args], content: prompt.content });

        if (invocations.length === 1) {
          // Turn 1: 120,000 tokens (below 150k threshold) -> must NOT auto-compact
          proc.sendFrame({
            event: "message",
            conversation_id: "conv-autocompact-1",
            role: "assistant",
            content: "Turn 1 below 150k threshold",
          });
          proc.sendFrame({
            event: "result",
            conversation_id: "conv-autocompact-1",
            status: "success",
            response: "Turn 1 below 150k threshold",
            stats: {
              input_tokens: 10_000,
              cache_read_tokens: 109_000,
              output_tokens: 1_000,
              total_tokens: 120_000,
            },
          });
          return;
        }

        if (invocations.length === 2) {
          // Turn 2: reaches 225,000 tokens (>= 150k threshold) -> schedules auto-compact
          proc.sendFrame({
            event: "message",
            conversation_id: "conv-autocompact-1",
            role: "assistant",
            content: "Turn 2 above 150k threshold",
          });
          proc.sendFrame({
            event: "result",
            conversation_id: "conv-autocompact-1",
            status: "success",
            response: "Turn 2 above 150k threshold",
            stats: {
              input_tokens: 14_000,
              cache_read_tokens: 210_000,
              output_tokens: 1_000,
              total_tokens: 225_000,
            },
          });
          return;
        }

        // Invocation 3: automatic /compact turn on the same resident slash-enabled
        // process. Real agy repeats the conversation's cumulative totals here.
        proc.sendFrame({
          event: "result",
          conversation_id: "conv-autocompact-1",
          status: "success",
          stats: {
            input_tokens: 24_000,
            cache_read_tokens: 319_000,
            output_tokens: 2_000,
            total_tokens: 26_000,
          },
        });
      });

      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-autocompact-1",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const brainDir = path.join(tempDir, "brain");
    writeFirstRequest(brainDir, "conv-autocompact-1", 17_000);
    const session = new Session(
      "sess-autocompact-1",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (event) => events.push(event),
      {
        spawn,
        storeDir: tempDir,
        brainDir,
        hotCacheTtlMs: 80,
        autoCompactLeadMs: 40,
      },
    );

    // 1) Turn 1 finishes at 120,000 tokens (< 150,000): wait 90ms and confirm NO auto-compact runs
    await session.prompt(userPrompt("First turn under 150k", "msg-under"));
    await waitForTurnEnd(events);
    assert.equal(session.getLastContextTokens(), 120_000);
    assert.equal(session.hasCompactedSinceLastTurn(), false);
    assert.equal(session.getLastIdleSkipReason(), "small");
    await new Promise((resolve) => setTimeout(resolve, 90));
    assert.equal(invocations.length, 1);

    // 2) Turn 2 finishes at 225,000 tokens (>= 150,000): auto-compact fires after ~40ms
    events.length = 0;
    await session.prompt(userPrompt("Second turn over 150k", "msg-over"));
    await waitForTurnEnd(events);
    assert.equal(session.getLastContextTokens(), 225_000);
    assert.equal(session.hasCompactedSinceLastTurn(), false);
    assert.equal(
      invocations[1]?.args.includes("--disable-slash-commands"),
      false,
      "Plain-text turns run on the resident slash-enabled process",
    );
    assert.equal(spawns, 1);

    // Clear turn-2 events so we can verify background auto-compaction does not emit session.turn
    events.length = 0;

    // Wait for auto-compaction turn (turn-3) to complete
    await new Promise<void>((resolve, reject) => {
      const deadline = Date.now() + 2_000;
      const poll = () => {
        if (session.hasCompactedSinceLastTurn()) {
          resolve();
          return;
        }
        if (Date.now() > deadline) {
          reject(new Error("Timed out waiting for auto-compaction turn"));
          return;
        }
        setTimeout(poll, 10);
      };
      poll();
    });

    assert.equal(invocations.length, 3);
    assert.equal(invocations[2]?.content, "/compact");
    assert.equal(
      invocations[2]?.args.includes("--disable-slash-commands"),
      false,
      "Auto-compaction must run on agy without --disable-slash-commands so /compact expands into SlashCommandScopeItem",
    );
    assert.equal(spawns, 1, "Auto-compaction must reuse the resident agy process instead of respawning it");
    assert.equal(session.hasCompactedSinceLastTurn(), true);
    const postCompactTokens = 17_000 + POST_COMPACT_SUMMARY_TOKENS;
    assert.equal(
      session.getLastContextTokens(),
      postCompactTokens,
      "Post-compaction context is the first request plus the summary, never the cumulative result totals",
    );
    assert.equal(
      events.some((e) => e.type === "session.turn"),
      false,
      "Background idle auto-compaction must not emit foreground session.turn events",
    );
    const compactUsage = usageSamples(events).at(-1);
    assert.equal(compactUsage?.usage.contextWindowUsedTokens, postCompactTokens,
      "Background compaction must move Paseo's context meter off the pre-compaction size");
    assert.equal(compactUsage?.usage.contextWindowMaxTokens, 262_144);
    assert.equal(compactUsage?.turnId, undefined, "Background compaction has no Paseo turn");

    const compactNotif = events.find(
      (
        e,
      ): e is Extract<ProviderEvent, { type: "timeline.item" }> =>
        e.type === "timeline.item" &&
        e.item.type === "compaction" &&
        e.item.preTokens === 225_000 && e.item.status === "completed",
    );
    assert.ok(compactNotif, "Expected native compaction event, never a replayed startup notification");
    assert.ok(compactNotif.timestamp, "Live compaction must retain its original event time");

    // 3) Because compactedSinceLastTurn is now true, staying idle must NOT trigger a second compaction
    await new Promise((resolve) => setTimeout(resolve, 90));
    assert.equal(invocations.length, 3);
    await session.dispose();
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("new user prompt cancels pending auto-compaction timer and manual /compact marks session compacted", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-cancel-compact-"));
  try {
    const events: ProviderEvent[] = [];
    const invocations: Array<{ args: readonly string[]; content: string }> = [];

    const spawn: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (prompt, proc) => {
        invocations.push({ args: [...args], content: prompt.content });
        proc.sendFrame({
          event: "result",
          conversation_id: "conv-cancel-compact",
          status: "success",
          response: prompt.content.startsWith("/compact") ? "" : "OK",
          stats: {
            input_tokens: 10_000,
            cache_read_tokens: 205_000,
            output_tokens: 500,
            total_tokens: 215_500,
          },
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-cancel-compact",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const brainDir = path.join(tempDir, "brain");
    writeFirstRequest(brainDir, "conv-cancel-compact", 17_000);
    const session = new Session(
      "sess-cancel-compact",
      "agy",
      [],
      {},
      makeConfig({ systemPrompt: "System rules here" }),
      undefined,
      TEST_CATALOG,
      (event) => events.push(event),
      {
        spawn,
        storeDir: tempDir,
        brainDir,
        hotCacheTtlMs: 120,
        autoCompactLeadMs: 40, // 80ms delay
      },
    );

    // Turn 1 reaches 215,500 tokens and schedules an 80ms auto-compact timer
    await session.prompt(userPrompt("Turn 1", "m-1"));
    await waitForTurnEnd(events);
    assert.equal(session.getLastContextTokens(), 215_500);
    assert.equal(session.hasCompactedSinceLastTurn(), false);

    // After 30ms (before the 80ms timer fires), user sends a manual /compact command
    await new Promise((resolve) => setTimeout(resolve, 30));
    events.length = 0;
    await session.prompt({
      clientMessageId: "m-compact",
      delivery: "auto",
      input: {
        type: "command",
        name: "compact",
        arguments: "focus on active task",
      },
    });
    await waitForTurnEnd(events);

    assert.equal(invocations.length, 2);
    assert.equal(invocations[1]?.content, "/compact focus on active task");
    assert.equal(
      invocations[1]?.args.includes("--disable-slash-commands"),
      false,
    );
    assert.equal(session.hasCompactedSinceLastTurn(), true);
    assert.equal(
      session.getLastContextTokens(),
      17_000 + POST_COMPACT_SUMMARY_TOKENS,
      "Manual /compact records the estimated compacted size, not its unchanged result totals",
    );
    const manualUsage = usageSamples(events).at(-1);
    assert.equal(manualUsage?.usage.contextWindowUsedTokens, 17_000 + POST_COMPACT_SUMMARY_TOKENS);
    assert.ok(manualUsage?.turnId, "A foreground compaction reports usage on its own turn");

    // Wait another 120ms to confirm no background auto-compact timer fires after manual /compact
    await new Promise((resolve) => setTimeout(resolve, 120));
    assert.equal(invocations.length, 2);
    await session.dispose();
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("Claude Code idle compaction gates: skips when prompt cache has lapsed, isolates post-compaction tokens across follow-up turns, and honors rapid-refill + failure circuit breakers", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-claude-gates-"));
  try {
    // 1) Cache-lapsed gate ("lapsed"): if clock advances past cacheExpiresAt before timer callback runs, skip compaction
    let fakeNow = 1_000_000;
    const lapsedPrompts: string[] = [];
    const spawnLapsed: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (prompt, proc) => {
        lapsedPrompts.push(prompt.content);
        proc.sendFrame({
          event: "result",
          conversation_id: "conv-lapsed",
          status: "success",
          response: "OK",
          stats: {
            input_tokens: 15_000,
            cache_read_tokens: 210_000,
            output_tokens: 1_000,
            total_tokens: 226_000,
          },
        });
      });
      queueMicrotask(() => {
        child.sendFrame({ event: "init", conversation_id: "conv-lapsed" });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const lapsedEvents: ProviderEvent[] = [];
    const lapsedSession = new Session(
      "sess-lapsed",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (e) => lapsedEvents.push(e),
      {
        spawn: spawnLapsed,
        storeDir: tempDir,
        now: () => fakeNow,
        hotCacheTtlMs: 80,
        autoCompactLeadMs: 40,
        autoCompactMaxLateMs: 30,
      },
    );

    await lapsedSession.prompt(userPrompt("Turn before sleep", "m-sleep"));
    await waitForTurnEnd(lapsedEvents);
    // Simulate machine sleep: clock jumps past cacheExpiresAt (1_000_000 + 80 = 1_000_080) before the 40ms timer fires
    fakeNow = 1_000_200;
    await new Promise((resolve) => setTimeout(resolve, 70));
    assert.deepEqual(lapsedPrompts, [`${CONTEXT_OUTPUT_GUIDANCE}\n\nTurn before sleep`]);
    assert.equal(lapsedSession.getLastIdleSkipReason(), "lapsed");
    assert.equal(lapsedSession.hasCompactedSinceLastTurn(), false);
    await lapsedSession.dispose();

    // 2) Rapid-refill thrashing circuit breaker (3 consecutive refills within
    //    < RAPID_REFILL_REQUEST_WINDOW agy model requests each) AND post-compaction
    //    token isolation when a follow-up turn is small (< 150k). The fake agy
    //    writes its native transcript as agy does; the session counts model
    //    requests from it.
    const brainDir = path.join(tempDir, "brain");
    let nextTurnTokens = 240_000;
    const spawnRefilling = (conv: string, requestsPerTurn: number, prompts: string[]): SpawnFunction => {
      const logs = path.join(brainDir, conv, ".system_generated", "logs");
      mkdirSync(logs, { recursive: true });
      const rows: string[] = [];
      const log = (type: string, extra: Record<string, unknown> = {}) => rows.push(JSON.stringify({
        step_index: rows.length, type, status: "DONE", created_at: new Date().toISOString(), ...extra }));
      return (_cmd, args) => {
        const child = new FakeChildProcess(args, (prompt, proc) => {
          prompts.push(prompt.content);
          log("USER_INPUT", { content: prompt.content });
          if (prompt.content === "/compact") log("CHECKPOINT");
          // The conversation's first request is the system prompt, tools and task.
          else for (let i = 0; i < requestsPerTurn; i++) log("PLANNER_RESPONSE", { input_tokens: rows.length === 1 ? 20_000 : nextTurnTokens });
          writeFileSync(path.join(logs, "transcript.jsonl"), rows.join("\n") + "\n");
          if (prompt.content === "/compact") {
            // Real agy /compact frame: result usage repeats the conversation's
            // cumulative totals and says nothing about the compacted size.
            proc.sendFrame({
              event: "result",
              conversation_id: conv,
              status: "success",
              stats: {
                input_tokens: 2_000,
                cache_read_tokens: 240_000,
                output_tokens: 1_500,
                total_tokens: 243_500,
              },
            });
            return;
          }
          proc.sendFrame({
            event: "result",
            conversation_id: conv,
            status: "success",
            response: "Turn reply",
            stats: {
              input_tokens: 20_000,
              cache_read_tokens: Math.max(0, nextTurnTokens - 21_000),
              output_tokens: 1_000,
              total_tokens: nextTurnTokens,
            },
          });
        });
        queueMicrotask(() => {
          child.sendFrame({ event: "init", conversation_id: conv });
        });
        return child as unknown as ReturnType<SpawnFunction>;
      };
    };

    const thrashEvents: ProviderEvent[] = [];
    const thrashPrompts: string[] = [];
    const thrashSession = new Session(
      "sess-thrash",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (e) => thrashEvents.push(e),
      {
        spawn: spawnRefilling("conv-thrash", 3, thrashPrompts),
        storeDir: tempDir,
        brainDir,
        hotCacheTtlMs: 60,
        autoCompactLeadMs: 30,
      },
    );

    const waitForCompactDone = (session: Session) =>
      new Promise<void>((resolve, reject) => {
        const deadline = Date.now() + 2_000;
        const poll = () => {
          if (session.hasCompactedSinceLastTurn()) {
            resolve();
            return;
          }
          if (Date.now() > deadline) {
            reject(new Error("Timed out waiting for compact"));
            return;
          }
          setTimeout(poll, 10);
        };
        poll();
      });

    // Compaction #1 (initial compaction: consecutiveRapidRefills = 0)
    thrashEvents.length = 0;
    await thrashSession.prompt(userPrompt("Big turn 1", "t-1"));
    await waitForTurnEnd(thrashEvents);
    await waitForCompactDone(thrashSession);
    assert.equal(thrashSession.getLastContextTokens(), 20_000 + POST_COMPACT_SUMMARY_TOKENS);
    assert.equal(thrashSession.getConsecutiveRapidRefills(), 0);

    // Follow-up small turn (e.g. STATUS_POLL at 25,000 tokens < 150k): must NOT re-trigger compaction!
    nextTurnTokens = 25_000;
    thrashEvents.length = 0;
    await thrashSession.prompt(userPrompt("STATUS_POLL", "t-monitor"));
    await waitForTurnEnd(thrashEvents);
    assert.equal(thrashSession.getLastContextTokens(), 25_000);
    assert.equal(thrashSession.getLastIdleSkipReason(), "small");
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(thrashPrompts.filter((p) => p === "/compact").length, 1);

    // Compaction #2 after 6 model requests (< 10 -> consecutiveRapidRefills = 1)
    nextTurnTokens = 240_000;
    thrashEvents.length = 0;
    await thrashSession.prompt(userPrompt("Big turn 2", "t-2"));
    await waitForTurnEnd(thrashEvents);
    await waitForCompactDone(thrashSession);
    assert.equal(thrashSession.getConsecutiveRapidRefills(), 1);

    // Compaction #3 after 3 model requests (consecutiveRapidRefills = 2)
    thrashEvents.length = 0;
    await thrashSession.prompt(userPrompt("Big turn 3", "t-3"));
    await waitForTurnEnd(thrashEvents);
    await waitForCompactDone(thrashSession);
    assert.equal(thrashSession.getConsecutiveRapidRefills(), 2);

    // 4th refill after 3 model requests (consecutiveRapidRefills = 3 -> TRIPS BREAKER!)
    thrashEvents.length = 0;
    await thrashSession.prompt(userPrompt("Big turn 4", "t-4"));
    await waitForTurnEnd(thrashEvents);
    await new Promise((resolve) => setTimeout(resolve, 70));
    assert.equal(thrashSession.isRapidRefillBreakerTripped(), true);
    assert.equal(thrashSession.getConsecutiveRapidRefills(), 3);
    assert.equal(thrashSession.getLastIdleSkipReason(), "rapid_refill_breaker_open");
    assert.equal(
      thrashPrompts.filter((p) => p === "/compact").length,
      3,
      "4th rapid refill must be blocked by the rapid-refill circuit breaker",
    );
    const isThrashNotif = (e: ProviderEvent) =>
      e.type === "timeline.item" &&
      e.item.type === "notification" &&
      e.item.message === AUTOCOMPACT_THRASHING_MESSAGE;
    assert.ok(thrashEvents.some(isThrashNotif), "Expected Claude Code thrashing circuit-breaker warning notification");
    await thrashSession.dispose();

    // The same prompts doing 40 agy requests each are normal long work, even
    // though every refill is a single Paseo prompt: every idle compact runs.
    const longEvents: ProviderEvent[] = [];
    const longPrompts: string[] = [];
    const longSession = new Session(
      "sess-long",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (e) => longEvents.push(e),
      {
        spawn: spawnRefilling("conv-long", 40, longPrompts),
        storeDir: tempDir,
        brainDir,
        hotCacheTtlMs: 60,
        autoCompactLeadMs: 30,
      },
    );
    let longThrashNotices = 0;
    for (let i = 1; i <= 4; i++) {
      longEvents.length = 0;
      await longSession.prompt(userPrompt(`Long turn ${i}`, `l-${i}`));
      await waitForTurnEnd(longEvents);
      await waitForCompactDone(longSession);
      longThrashNotices += longEvents.filter(isThrashNotif).length;
    }
    assert.equal(longPrompts.filter((p) => p === "/compact").length, 4);
    assert.equal(longSession.getConsecutiveRapidRefills(), 0);
    assert.equal(longSession.isRapidRefillBreakerTripped(), false);
    assert.equal(longThrashNotices, 0);
    await longSession.dispose();

    // 3) Consecutive failure circuit breaker (3 consecutive failures trips breaker)
    //    AND incoming foreground user prompt waiting cleanly while background auto-compact is in flight
    const failEvents: ProviderEvent[] = [];
    const failPrompts: string[] = [];
    let holdCompact = false;
    let releaseHeldCompact: (() => void) | null = null;

    const spawnFail: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (prompt, proc) => {
        failPrompts.push(prompt.content);
        if (prompt.content === "/compact") {
          const finishCompact = () => {
            proc.sendFrame({
              event: "result",
              conversation_id: "conv-fail-breaker",
              status: "error",
              errors: ["Compaction backend failure"],
            });
          };
          if (holdCompact) {
            releaseHeldCompact = finishCompact;
          } else {
            finishCompact();
          }
          return;
        }
        proc.sendFrame({
          event: "result",
          conversation_id: "conv-fail-breaker",
          status: "success",
          response: `Reply to ${prompt.content}`,
          stats: {
            input_tokens: 15_000,
            cache_read_tokens: 200_000,
            output_tokens: 1_000,
            total_tokens: 216_000,
          },
        });
      });
      queueMicrotask(() => {
        child.sendFrame({ event: "init", conversation_id: "conv-fail-breaker" });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const failSession = new Session(
      "sess-fail-breaker",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (e) => failEvents.push(e),
      {
        spawn: spawnFail,
        storeDir: tempDir,
        hotCacheTtlMs: 50,
        autoCompactLeadMs: 25,
      },
    );

    // Failures 1 and 2
    for (let i = 1; i <= 2; i += 1) {
      failEvents.length = 0;
      await failSession.prompt(userPrompt(`Turn ${i}`, `f-${i}`));
      await waitForTurnEnd(failEvents);
      await new Promise((resolve) => setTimeout(resolve, 60));
      assert.equal(failSession.getConsecutiveCompactFailures(), i);
      assert.equal(failSession.isFailureBreakerOpen(), false);
    }

    // Turn 3: hold the 3rd background /compact in-flight and send a foreground user prompt ("Turn 4 while compacting")
    holdCompact = true;
    failEvents.length = 0;
    await failSession.prompt(userPrompt("Turn 3", "f-3"));
    await waitForTurnEnd(failEvents);
    await new Promise((resolve) => setTimeout(resolve, 40));
    assert.ok(releaseHeldCompact, "3rd background auto-compact should be in flight");

    failEvents.length = 0;
    const turn4Promise = failSession.prompt(
      userPrompt("Turn 4 while compacting", "f-4"),
    );
    // Release the held background auto-compact after 20ms
    setTimeout(() => {
      holdCompact = false;
      releaseHeldCompact?.();
    }, 20);
    await turn4Promise;
    const turn4End = await waitForTurnEnd(failEvents);
    assert.equal(turn4End.state, "completed");
    assert.equal(failSession.getConsecutiveCompactFailures(), 3);
    assert.equal(failSession.isFailureBreakerOpen(), true);
    assert.equal(failSession.getLastIdleSkipReason(), "failure_breaker_open");

    // Wait another 60ms after Turn 4: because failure breaker is open (3 failures), NO 4th /compact is attempted
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.equal(
      failPrompts.filter((p) => p === "/compact").length,
      3,
      "Failure circuit breaker must prevent any 4th auto-compaction attempt",
    );
    await failSession.dispose();
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("context window resolution maps model families and getCatalog enriches the catalogue", async () => {
  assert.equal(resolveContextWindowMaxTokens("gemini-4-argon-high"), 262_144);
  assert.equal(resolveContextWindowMaxTokens("gemini-3.8-flash-low"), 262_144);
  assert.equal(resolveContextWindowMaxTokens("claude-opus-5-5-high"), 200_000);
  assert.equal(resolveContextWindowMaxTokens("claude-sonnet-5-5-medium"), 200_000);
  assert.equal(resolveContextWindowMaxTokens("gpt-oss-120b-medium"), 131_072);
  assert.equal(resolveContextWindowMaxTokens(undefined), 262_144);
  assert.equal(resolveContextWindowMaxTokens("some-unknown-model"), 262_144);

  const host: ProviderHost = {
    async readCatalogCache() {
      return null;
    },
    async writeCatalogCache() {},
  };
  const catalog = await getCatalog(host, "agy", [], {}, {
    probeImpl: async () => ({
      exitCode: 0,
      stdout:
        "gemini-4-argon-medium\tGemini 4 Argon (Medium)\nclaude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n",
      stderr: "",
    }),
  });
  assert.equal(catalog.models[0]?.contextWindowMaxTokens, 262_144);
  assert.equal(catalog.models[1]?.contextWindowMaxTokens, 200_000);
});

test("session.usage reports a context window max from the nested result.usage frame", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-window-"));
  try {
    const events: ProviderEvent[] = [];
    const spawn: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (_prompt, proc) => {
        proc.sendFrame({
          event: "message",
          conversation_id: "conv-window",
          role: "assistant",
          content: "ok",
        });
        // Real `agy --output-format stream-json` shape: totals live under
        // result.usage, not a top-level stats object.
        proc.sendFrame({
          event: "result",
          result: {
            conversation_id: "conv-window",
            status: "SUCCESS",
            response: "ok\n",
            duration_seconds: 3.5,
            num_turns: 1,
            usage: {
              input_tokens: 14_559,
              output_tokens: 5,
              thinking_tokens: 0,
              cache_read_tokens: 0,
              total_tokens: 14_564,
            },
          },
        });
      });
      queueMicrotask(() => {
        child.sendFrame({ event: "init", conversation_id: "conv-window" });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "sess-window",
      "agy",
      [],
      {},
      makeConfig(),
      undefined,
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir },
    );
    await session.prompt(userPrompt("hi", "msg-window"));
    await waitForTurnEnd(events);

    const usageEvents = events.filter(
      (event): event is Extract<ProviderEvent, { type: "session.usage" }> =>
        event.type === "session.usage",
    );
    assert.equal(usageEvents.length, 1);
    const usage = usageEvents[0]?.usage;
    assert.equal(usage?.contextWindowUsedTokens, 14_564);
    assert.equal(usage?.contextWindowMaxTokens, 262_144);
    await session.dispose();
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("providerOptions.contextWindowMaxTokens overrides the model-family default", async () => {
  const tempDir = mkdtempSync(path.join(tmpdir(), "agy-resilient-window-override-"));
  try {
    const events: ProviderEvent[] = [];
    const spawn: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (_prompt, proc) => {
        proc.sendFrame({
          event: "result",
          result: {
            conversation_id: "conv-window-override",
            status: "SUCCESS",
            response: "ok",
            usage: { input_tokens: 1_000, output_tokens: 10, total_tokens: 1_010 },
          },
        });
      });
      queueMicrotask(() => {
        child.sendFrame({
          event: "init",
          conversation_id: "conv-window-override",
        });
      });
      return child as unknown as ReturnType<SpawnFunction>;
    };

    const session = new Session(
      "sess-window-override",
      "agy",
      [],
      {},
      makeConfig({ providerOptions: { contextWindowMaxTokens: 250_000 } }),
      undefined,
      TEST_CATALOG,
      (event) => events.push(event),
      { spawn, storeDir: tempDir },
    );
    await session.prompt(userPrompt("hi", "msg-window-override"));
    await waitForTurnEnd(events);

    const usage = events.find(
      (event): event is Extract<ProviderEvent, { type: "session.usage" }> =>
        event.type === "session.usage",
    )?.usage;
    assert.equal(usage?.contextWindowMaxTokens, 250_000);
    await session.dispose();
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
});

test("conversation breaker survives reopen and does not block explicit manual compact", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-breaker-reopen-"));
  const sessions: Session[] = [];
  try {
    for (const kind of ["failure", "rapid"] as const) {
      const conv = `conv-reopen-${kind}`;
      const state = emptyCompactionState();
      for (let i = 0; i < (kind === "failure" ? 3 : 4); i++) {
        beginBusinessTurn(state);
        state.modelRequests = i; // one model request between compacts: real thrashing
        beginCompact(state, `turn-${i + 1}:auto-compact`, "idle", Date.now() - 50_000 + i * 1_000);
        // Leave the last failed attempt pending to exercise interrupted recovery.
        if (kind !== "failure" || i < 2) finishCompact(state, `turn-${i + 1}:auto-compact`, kind === "rapid", Date.now() - 40_000 + i, 10_000);
      }
      new ConversationStore({ storeDir: dir, brainDir: dir }).saveCompaction(conv, state);
      writeFirstRequest(dir, conv, 11_000);
      const prompts: string[] = [];
      const events: ProviderEvent[] = [];
      const spawn: SpawnFunction = (_cmd, args) => {
        const child = new FakeChildProcess(args, (prompt, proc) => {
          prompts.push(prompt.content);
          proc.sendFrame({ event: "result", conversation_id: conv, status: "success", response: "OK",
            stats: { input_tokens: 240_000, output_tokens: prompt.content === "/compact" ? 15_000 : 1_000 } });
        });
        queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: conv }));
        return child as unknown as ReturnType<SpawnFunction>;
      };
      const makeSession = () => new Session("reopened", "agy", [], {}, makeConfig(),
        { version: 1, data: { conversationId: conv, sessionKey: conv } }, TEST_CATALOG, (e) => events.push(e),
        { storeDir: dir, brainDir: dir, spawn, hotCacheTtlMs: 80, autoCompactLeadMs: 40 });
      let session = makeSession(); sessions.push(session);
      assert.equal(kind === "failure" ? session.isFailureBreakerOpen() : session.isRapidRefillBreakerTripped(), true);
      session.open("open", [], "replay");
      assert.equal(events.some((e) => e.type === "session.turn"), false);
      events.length = 0;
      await session.prompt(userPrompt("real work", "reopen-work"));
      await waitForTurnEnd(events);
      await new Promise((resolve) => setTimeout(resolve, 90));
      assert.deepEqual(prompts, [`${CONTEXT_OUTPUT_GUIDANCE}\n\nreal work`], "guidance accompanies real work; no automatic compact or synthetic turn");
      await session.dispose();
      events.length = 0;
      session = makeSession(); sessions.push(session);
      assert.equal(kind === "failure" ? session.isFailureBreakerOpen() : session.isRapidRefillBreakerTripped(), true);
      await session.prompt(userPrompt("/compact", "explicit-escape"));
      await waitForTurnEnd(events);
      assert.equal(session.isFailureBreakerOpen(), false);
      assert.equal(session.isRapidRefillBreakerTripped(), false);
      assert.equal(session.getLastContextTokens(), 11_000 + POST_COMPACT_SUMMARY_TOKENS);
      await session.dispose();
      const restored = makeSession(); sessions.push(restored);
      assert.equal(restored.hasCompactedSinceLastTurn(), true);
      assert.equal(restored.getLastContextTokens(), 11_000 + POST_COMPACT_SUMMARY_TOKENS);
    }
  } finally {
    for (const session of sessions) await session.dispose();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("flat native history plus live CHECKPOINT updates context without replaying old compactions or counting manual twice", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-native-compact-"));
  let session: Session | undefined;
  try {
    const conv = "conv-native-mixed";
    const logs = path.join(dir, "brain", conv, ".system_generated", "logs");
    mkdirSync(logs, { recursive: true });
    const file = path.join(logs, "transcript.jsonl");
    let now = Date.parse("2026-10-16T08:00:00Z");
    const rows: Record<string, unknown>[] = [
      { step_index: 0, type: "USER_INPUT", status: "DONE", created_at: new Date(now - 30_000).toISOString(), content: "old task" },
      { step_index: 1, type: "CHECKPOINT", status: "DONE", created_at: new Date(now - 20_000).toISOString(), content: "old summary" },
      { step_index: 2, type: "PLANNER_RESPONSE", status: "DONE", created_at: new Date(now - 10_000).toISOString(), input_tokens: 20_000 },
    ];
    const saveLog = () => writeFileSync(file, rows.map((r) => JSON.stringify(r)).join("\n") + "\n");
    saveLog();
    const events: ProviderEvent[] = [];
    let nextStep = 3;
    const spawn: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (prompt, proc) => {
        // The CLI can replay an old checkpoint at a new timeline position.
        proc.sendFrame({ event: "step_update", conversation_id: conv, timestamp: rows[1].created_at,
          step_update: { step_index: 1, step_type: "CHECKPOINT", state: "DONE" } });
        rows.push({ step_index: nextStep++, type: "USER_INPUT", status: "DONE", created_at: new Date(now).toISOString(), content: prompt.content });
        const step = nextStep++;
        rows.push({ step_index: step, type: "CHECKPOINT", status: "DONE", created_at: new Date(now).toISOString(), content: "new summary" });
        saveLog();
        proc.sendFrame({ event: "step_update", conversation_id: conv, timestamp: new Date(now).toISOString(),
          step_update: { step_index: step, step_type: "CHECKPOINT", state: "DONE" } });
        if (prompt.content !== "/compact") {
          const usageStep = nextStep++;
          rows.push({ step_index: usageStep, type: "PLANNER_RESPONSE", status: "DONE", created_at: new Date(now + 100).toISOString(), input_tokens: 22_000 });
          saveLog();
          proc.sendFrame({ event: "step_update", conversation_id: conv, timestamp: new Date(now + 100).toISOString(),
            step_update: { step_index: usageStep, step_type: "agent_response", state: "DONE", usage: { input_tokens: 22_000 } } });
        }
        proc.sendFrame({ event: "result", conversation_id: conv, status: "success", response: "OK",
          stats: { input_tokens: 300_000, output_tokens: 14_000, total_tokens: 314_000 } });
      });
      queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: conv }));
      return child as unknown as ReturnType<SpawnFunction>;
    };
    const makeSession = () => new Session("mixed", "agy", [], {}, makeConfig(),
      { version: 1, data: { conversationId: conv, sessionKey: conv } }, TEST_CATALOG, (e) => events.push(e),
      { storeDir: dir, brainDir: path.join(dir, "brain"), spawn, now: () => now, hotCacheTtlMs: 80, autoCompactLeadMs: 40 });
    session = makeSession();
    assert.equal(session.getLastContextTokens(), 20_000);
    session.open("open", [], "skip");
    assert.equal(events.some((e) => e.type === "timeline.item" && e.item.type === "compaction"), false);
    events.length = 0;
    await session.prompt(userPrompt("current work", "native-turn"));
    await waitForTurnEnd(events);
    assert.equal(session.getLastContextTokens(), 22_000, "result aggregate must not restore pre-compact input size");
    assert.equal(events.filter((e) => e.type === "timeline.item" && e.item.type === "compaction").length, 1);
    assert.equal(session.hasCompactedSinceLastTurn(), true);
    assert.deepEqual(usageSamples(events).map((e) => e.usage.contextWindowUsedTokens),
      [20_000 + POST_COMPACT_SUMMARY_TOKENS, 22_000, 22_000],
      "the meter drops when the checkpoint lands, then follows real requests");
    now += 5_000;
    events.length = 0;
    await session.prompt(userPrompt("/compact", "manual-turn"));
    await waitForTurnEnd(events);
    assert.equal(events.filter((e) => e.type === "timeline.item" && e.item.type === "compaction").length, 1);
    assert.equal(session.getLastContextTokens(), 20_000 + POST_COMPACT_SUMMARY_TOKENS);
    await session.dispose();
    events.length = 0;
    session = makeSession();
    assert.equal(session.getStore().loadCompaction(conv).events.length, 3, "old native + current native + one manual checkpoint");
    assert.equal(session.getLastContextTokens(), 20_000 + POST_COMPACT_SUMMARY_TOKENS);
    assert.equal(session.isRapidRefillBreakerTripped(), false);
    assert.equal(events.length, 0, "rehydration only loads control state");
  } finally { await session?.dispose(); rmSync(dir, { recursive: true, force: true }); }
});

test("idle native checkpoint shares one event, ignores sticky error with empty response, and guidance never alters persisted user text", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-idle-checkpoint-"));
  let session: Session | undefined;
  try {
    const conv = "conv-idle-native";
    const prompts: string[] = [];
    const events: ProviderEvent[] = [];
    const spawn: SpawnFunction = (_cmd, args) => {
      const child = new FakeChildProcess(args, (prompt, proc) => {
        prompts.push(prompt.content);
        if (prompt.content === "/compact") {
          proc.sendFrame({ event: "step_update", conversation_id: conv, timestamp: new Date().toISOString(),
            step_update: { step_index: 10, step_type: "CHECKPOINT", state: "DONE" } });
          proc.sendFrame({ event: "result", conversation_id: conv, status: "error", errors: [IDLE_TIMEOUT_ERROR], response: "",
            stats: { input_tokens: 250_000, output_tokens: 12_000 } });
          return;
        }
        proc.sendFrame({ event: "result", conversation_id: conv, status: "error", errors: [IDLE_TIMEOUT_ERROR], response: "OK",
          stats: { input_tokens: 250_000, output_tokens: 100 } });
      });
      queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: conv }));
      return child as unknown as ReturnType<SpawnFunction>;
    };
    session = new Session("idle-native", "agy", [], {}, makeConfig(), undefined, TEST_CATALOG,
      (e) => events.push(e), { storeDir: dir, brainDir: dir, spawn, hotCacheTtlMs: 80, autoCompactLeadMs: 40, streamInterruptRetryDelayMs: 5 });
    await session.prompt(userPrompt("Original research request", "original"));
    await waitForTurnEnd(events);
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(prompts.length, 2, "must not retry /compact when native CHECKPOINT succeeded despite sticky result error");
    assert.equal(prompts[0], `${CONTEXT_OUTPUT_GUIDANCE}\n\nOriginal research request`);
    const userItem = session.getStore().load(conv).find((r) => r.item.type === "user_message")?.item;
    assert.equal(userItem?.type === "user_message" ? userItem.text : undefined, "Original research request");
    assert.equal(events.filter((e) => e.type === "timeline.item" && e.item.type === "compaction").length, 1);
    const state = session.getStore().loadCompaction(conv);
    assert.equal(state.events.length, 1);
    assert.equal(state.events[0].source, "idle");
    assert.equal(state.events[0].nativeStep, 10);
    assert.equal(state.lastContextTokens, 0, "without a native transcript the compacted size is unknown, never the result totals");
    const idleUsage = usageSamples(events).at(-1);
    assert.equal(idleUsage?.turnId, undefined);
    assert.equal(idleUsage?.usage.contextWindowUsedTokens, undefined,
      "an unknown size clears the meter instead of keeping the pre-compaction value");
    assert.equal(state.consecutiveCompactFailures, 0);
    assert.equal(state.consecutiveRapidRefills, 0);
  } finally { await session?.dispose(); rmSync(dir, { recursive: true, force: true }); }
});

test("new and restored sessions inject guidance only once on the next real non-command prompt", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-runtime-guidance-"));
  const sessions: Session[] = [];
  try {
    for (const restored of [false, true]) {
      const conv = `guidance-${restored}`;
      const prompts: string[] = [];
      const events: ProviderEvent[] = [];
      const spawn: SpawnFunction = (_cmd, args) => {
        const child = new FakeChildProcess(args, (prompt, proc) => {
          prompts.push(prompt.content);
          proc.sendFrame({ event: "result", conversation_id: conv, status: "success", response: "OK" });
        });
        queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: conv }));
        return child as unknown as ReturnType<SpawnFunction>;
      };
      const session = new Session(`session-${conv}`, "agy", [], {}, makeConfig({ systemPrompt: "Original configured system instructions" }),
        restored ? { version: 1, data: { conversationId: conv, sessionKey: conv } } : undefined,
        TEST_CATALOG, (e) => events.push(e), { storeDir: dir, brainDir: dir, spawn, autoCompactEnabled: false });
      sessions.push(session);
      session.open("open", [], "skip");
      assert.equal(prompts.length, 0, "open does not send an unsolicited prompt");
      assert.equal(events.some((e) => e.type === "session.turn"), false);
      const inputs: ProviderPrompt[] = [
        userPrompt("/compact", "command-compact-first"),
        { clientMessageId: "typed-command", delivery: "auto", input: { type: "command", name: "status", arguments: "" } },
        userPrompt("/help details", "text-command"),
        userPrompt("First real research request", "first-real"),
        userPrompt("Second real research request", "second-real"),
        userPrompt("/compact", "command-compact-last"),
      ];
      for (const prompt of inputs) {
        events.length = 0;
        await session.prompt(prompt);
        assert.equal((await waitForTurnEnd(events)).state, "completed");
      }
      const prefix = restored ? CONTEXT_OUTPUT_GUIDANCE
        : `Original configured system instructions\n\n${CONTEXT_OUTPUT_GUIDANCE}`;
      assert.deepEqual(prompts, ["/compact", "/status", "/help details",
        `${prefix}\n\nFirst real research request`, "Second real research request", "/compact"]);
      const storedUserText = session.getStore().load(conv).flatMap((r) => r.item.type === "user_message" ? [r.item.text] : []);
      assert.deepEqual(storedUserText, ["/compact", "/status", "/help details",
        "First real research request", "Second real research request", "/compact"]);
      assert.equal(prompts.filter((text) => text.includes(CONTEXT_OUTPUT_GUIDANCE)).length, 1);
    }
  } finally {
    for (const session of sessions) await session.dispose();
    rmSync(dir, { recursive: true, force: true });
  }
});



test("one resident agy process serves plain turns and /compact; only literal leading-slash text needs a slash-disabled process", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-resident-driver-"));
  let session: Session | undefined;
  try {
    const conv = "conv-resident";
    const events: ProviderEvent[] = [];
    const children: FakeChildProcess[] = [];
    const sent: Array<{ child: number; content: string }> = [];
    const spawn: SpawnFunction = (_cmd, args) => {
      const index = children.length;
      const child = new FakeChildProcess(args, (prompt, proc) => {
        sent.push({ child: index, content: prompt.content });
        proc.sendFrame({ event: "result", conversation_id: conv, status: "success",
          response: prompt.content.startsWith("/compact") ? "" : "OK",
          stats: { input_tokens: 1_000, output_tokens: 100 } });
      });
      children.push(child);
      queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: conv }));
      return child as unknown as ReturnType<SpawnFunction>;
    };
    session = new Session("resident", "agy", [], {}, makeConfig(), undefined, TEST_CATALOG,
      (e) => events.push(e), { storeDir: dir, brainDir: dir, spawn, autoCompactEnabled: false });
    const turn = async (text: string, id: string) => {
      events.length = 0;
      await session!.prompt(userPrompt(text, id));
      const end = await waitForTurnEnd(events);
      assert.equal(end.state, "completed", text);
    };
    const disabled = (index: number) => children[index]?.args.includes("--disable-slash-commands");

    await turn("hello", "r1");
    await turn("/compact", "r2");
    await turn("plain follow-up", "r3");
    assert.equal(children.length, 1, "plain -> /compact -> plain must not respawn agy");
    assert.equal(disabled(0), false);
    assert.equal(children[0].killed, false);

    await turn("/etc/hosts looks wrong", "r4");
    assert.equal(children.length, 2, "literal leading-slash text needs --disable-slash-commands");
    assert.equal(disabled(1), true);
    assert.equal(children[0].killed, true, "the replaced process is stopped, never leaked");
    assert.ok(children[1].args.includes("--conversation"));
    assert.equal(sent.at(-1)?.content, "/etc/hosts looks wrong", "literal text is sent unmodified");

    await turn("plain again", "r5");
    assert.equal(children.length, 2, "plain text reuses whichever process is resident");
    await turn("/compact", "r6");
    assert.equal(children.length, 3);
    assert.equal(disabled(2), false);
    assert.deepEqual(sent.map((s) => s.child), [0, 0, 0, 1, 1, 2]);
    await session.dispose();
    assert.ok(children.every((c) => c.killed), "dispose stops the resident process");
  } finally { await session?.dispose(); rmSync(dir, { recursive: true, force: true }); }
});

test("idle compact, business, failed and retried manual compact turns converge through sticky errors", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-turn-converge-"));
  let session: Session | undefined;
  try {
    const conv = "conv-converge";
    const events: ProviderEvent[] = [];
    const prompts: string[] = [];
    let spawns = 0;
    let step = 0;
    const result = (proc: FakeChildProcess, status: string, errors: string[], response: string,
      stats: Record<string, number>) => proc.sendFrame({ event: "result", conversation_id: conv, status, errors, response, stats });
    const checkpoint = (proc: FakeChildProcess) => proc.sendFrame({ event: "step_update", conversation_id: conv,
      timestamp: new Date().toISOString(), step_update: { step_index: (step += 10), step_type: "CHECKPOINT", state: "DONE" } });
    const script: Array<(proc: FakeChildProcess) => void> = [
      // 1. business turn fills the context and arms the idle compact.
      (proc) => result(proc, "success", [], "Turn one", { input_tokens: 240_000, output_tokens: 1_000 }),
      // 2. idle /compact: checkpoint lands, then a sticky idle-timeout error with an empty response.
      (proc) => { checkpoint(proc); result(proc, "error", [IDLE_TIMEOUT_ERROR], "", { input_tokens: 240_000, output_tokens: 9_000 }); },
      // 3. business turn answered despite the sticky trajectory error.
      (proc) => result(proc, "error", [IDLE_TIMEOUT_ERROR], "Turn two", { input_tokens: 20_000, output_tokens: 100 }),
      // 4. manual /compact with an unrelated, unrecoverable error.
      (proc) => result(proc, "error", ["Model refused the request"], "", {}),
      // 5. manual /compact interrupted before any checkpoint: retried once.
      (proc) => result(proc, "error", [IDLE_TIMEOUT_ERROR], "", {}),
      // 6. the retry compacts.
      (proc) => { checkpoint(proc); result(proc, "success", [], "", { input_tokens: 30_000, output_tokens: 3_000 }); },
    ];
    const spawn: SpawnFunction = (_cmd, args) => {
      spawns += 1;
      const child = new FakeChildProcess(args, (prompt, proc) => {
        prompts.push(prompt.content);
        script[prompts.length - 1]?.(proc);
      });
      queueMicrotask(() => child.sendFrame({ event: "init", conversation_id: conv }));
      return child as unknown as ReturnType<SpawnFunction>;
    };
    writeFirstRequest(dir, conv, 18_000);
    session = new Session("converge", "agy", [], {}, makeConfig(), undefined, TEST_CATALOG,
      (e) => events.push(e), { storeDir: dir, brainDir: dir, spawn, hotCacheTtlMs: 80, autoCompactLeadMs: 40,
        streamInterruptRetryDelayMs: 5 });
    const terminal = () => events.filter((e): e is Extract<ProviderEvent, { type: "session.turn" }> =>
      e.type === "session.turn" && e.state !== "started").map((e) => e.state);
    const compactionRows = () => events.filter((e) => e.type === "timeline.item" && e.item.type === "compaction");

    await session.prompt(userPrompt("Large research turn", "c1"));
    await waitForTurnEnd(events);
    assert.equal(session.hasCompactedSinceLastTurn(), false);
    events.length = 0;
    const deadline = Date.now() + 2_000;
    while (!session.hasCompactedSinceLastTurn() || prompts.length < 2) {
      assert.ok(Date.now() < deadline, "idle compaction did not complete");
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(prompts[1], "/compact");
    assert.deepEqual(terminal(), [], "background compaction never emits session.turn");
    assert.equal(compactionRows().length, 1);
    assert.equal(session.getLastContextTokens(), 18_000 + POST_COMPACT_SUMMARY_TOKENS);
    assert.equal(session.getConsecutiveCompactFailures(), 0);
    assert.equal(prompts.length, 2, "sticky error after a native checkpoint must not re-send /compact");

    events.length = 0;
    await session.prompt(userPrompt("Follow-up", "c3"));
    await waitForTurnEnd(events);
    assert.deepEqual(terminal(), ["completed"], "sticky error with a response completes the business turn");
    assert.equal(session.hasCompactedSinceLastTurn(), false);
    assert.equal(session.getLastIdleSkipReason(), "small");

    events.length = 0;
    await session.prompt(userPrompt("/compact", "c4"));
    await waitForTurnEnd(events);
    assert.deepEqual(terminal(), ["failed"]);
    assert.equal(session.getConsecutiveCompactFailures(), 1);
    assert.equal(session.isFailureBreakerOpen(), false);

    events.length = 0;
    await session.prompt(userPrompt("/compact", "c5"));
    await waitForTurnEnd(events);
    assert.deepEqual(terminal(), ["completed"]);
    assert.deepEqual(prompts.slice(3), ["/compact", "/compact", "/compact"], "one stream retry, then success");
    assert.equal(compactionRows().length, 1);
    assert.equal(session.getConsecutiveCompactFailures(), 0, "a successful compact closes the failure streak");
    assert.equal(session.getConsecutiveRapidRefills(), 0, "manual compact resets rapid refills");
    assert.equal(session.isRapidRefillBreakerTripped(), false);
    assert.equal(session.hasCompactedSinceLastTurn(), true);
    assert.equal(session.getLastContextTokens(), 18_000 + POST_COMPACT_SUMMARY_TOKENS);
    assert.equal(spawns, 2, "only the stream-interruption retry restarts agy");
  } finally { await session?.dispose(); rmSync(dir, { recursive: true, force: true }); }
});
