import type {
  ProviderCapability,
  ProviderConfigChanges,
  ProviderEvent,
  ProviderPersistence,
  ProviderPrompt,
  ProviderSessionConfig,
  ProviderTimelineItem,
} from "@getpaseo/plugin/server/provider";
import {
  DEFAULT_MODE,
  MODES,
  resolveContextWindowMaxTokens,
  type ProviderDiscovery,
} from "./catalog.ts";
import {
  spawnDriver,
  type DriverProcess,
  type SpawnFunction,
} from "./process.ts";
import { renderPrompt } from "./prompt.ts";
import { ProviderActionError } from "./signals.ts";
import { ConversationStore } from "./store.ts";
import {
  beginBusinessTurn, beginCompact, emptyCompactionState, estimatePostCompactTokens, finishCompact,
  nativeObservation, RAPID_REFILL_BREAKER_LIMIT, RAPID_REFILL_REQUEST_WINDOW,
  reconcileNativeCompaction, recoverInterruptedCompacts, requestsSinceLastCompact,
  withinRapidRefillWindow, type CompactEvent,
} from "./compaction.ts";
import { replayTimelineItem, toolDetail, toolOutput } from "./timeline.ts";
import {
  diagnostic,
  frameConversationId,
  isRecord,
  isResultSuccess,
  isStreamInterruption,
  isTokenGap,
  parseFrame,
  resultError,
  resultErrors,
  resultResponse,
  type Frame,
  type TokenUsage,
} from "./wire.ts";

export const TOKEN_GAP_RETRY_DELAY_MS = 8_000;
export const STREAM_INTERRUPT_RETRY_DELAY_MS = 2_000;
export const TOKEN_GAP_CONTINUE_PROMPT =
  "Continue the interrupted turn from the current conversation state. Do not repeat completed text or tool actions.";

/**
 * Antigravity (`agy`) uses `CACHE_CONTROL_TYPE_EPHEMERAL`, whose hot prompt
 * cache window is 5 minutes (300,000 ms), verified in `agy.exe` and across
 * 16,700+ local trajectory steps (`~/.gemini/antigravity-cli/brain`).
 *
 * Idle compaction parameters & circuit breakers follow Claude Code's
 * implementation (`BKt` idle compactor + `aX` prompt cache tracker + `vmr`/`Ebn`
 * circuit breakers):
 * - Default minimum token threshold: 150,000 tokens (Claude Code's `bjo = 200_000`
 *   scaled down for agy's 256K window, which checkpoints natively at ~230-250K)
 * - Minimum env-override threshold (`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`): 100,000 (`wjo = 100_000`)
 * - Lead time before cache expiry: 60,000 ms (`Rjo = 60_000`)
 * - Max late tolerance after scheduled fire time: 60,000 ms (`Rjo = 60_000`)
 * - Rapid-refill thrashing circuit breaker: 3 consecutive idle compactions within
 *   < 10 agy model requests each (`hqt = 3` turns in Claude Code; one Paseo prompt
 *   runs many agy requests, so prompts are not the unit here); agy-native
 *   checkpoints are neutral (see `rapidRefillsAfter`)
 * - Consecutive failure circuit breaker: 3 consecutive failures (`Cbn = 3`)
 */
export const DEFAULT_HOT_CACHE_TTL_MS = 5 * 60 * 1_000;
export const DEFAULT_AUTO_COMPACT_LEAD_MS = 60 * 1_000;
export const DEFAULT_AUTO_COMPACT_MAX_LATE_MS = 60 * 1_000;
export const DEFAULT_AUTO_COMPACT_TOKEN_THRESHOLD = 150_000;
export const MIN_ENV_AUTO_COMPACT_TOKEN_THRESHOLD = 100_000;
export { RAPID_REFILL_BREAKER_LIMIT, RAPID_REFILL_REQUEST_WINDOW };
export const MAX_CONSECUTIVE_COMPACT_FAILURES = 3;
export const AUTO_COMPACT_COMMAND = "/compact";
export const CONTEXT_OUTPUT_GUIDANCE =
  "For large CSV, JSON, tables or logs, filter columns/rows and compute summaries in the tool invocation before returning output to this conversation. Keep complete research artifacts on disk and report their paths; do not silently truncate or discard source data or persisted user research.";
export const AUTOCOMPACT_THRASHING_MESSAGE =
  `Autocompact is thrashing: the context refilled to the limit within ${RAPID_REFILL_REQUEST_WINDOW} model requests of the previous compact, ${RAPID_REFILL_BREAKER_LIMIT} times in a row. A file being read or a tool output is likely too large for the context window. Try reading in smaller chunks, or use /clear to start fresh.`;

export type IdleCompactSkipReason =
  | "compaction_off"
  | "stopping"
  | "busy"
  | "no_conversation"
  | "already_compacted"
  | "failure_breaker_open"
  | "rapid_refill_breaker_open"
  | "no_cache_activity"
  | "state_unavailable"
  | "newer_request"
  | "lapsed"
  | "small";

export function isCompactPromptText(text: string): boolean {
  return /^\/compact(?:\s|$)/i.test(text.trim());
}

export type SessionOptions = {
  autoCompactEnabled?: boolean;
  autoCompactLeadMs?: number;
  autoCompactMaxLateMs?: number;
  autoCompactTokenThreshold?: number;
  brainDir?: string;
  hotCacheTtlMs?: number;
  now?: () => number;
  spawn?: SpawnFunction;
  store?: ConversationStore;
  storeDir?: string;
  streamInterruptRetryDelayMs?: number;
  tokenGapRetryDelayMs?: number;
};

/**
 * - `business`: a user conversation turn (foreground).
 * - `manual_compact`: an explicit `/compact` (foreground).
 * - `idle_compact`: the pre-cache-expiry compact (background). It emits no
 *   `session.turn`, stream items or usage; only its compaction outcome.
 */
type TurnKind = "business" | "manual_compact" | "idle_compact";

type ActiveTurn = {
  kind: TurnKind;
  compactEventId: string | null;
  nativeCompacted: boolean;
  nativeStartStep: number;
  activeMessageId: string | null;
  activeReasoningId: string | null;
  clientMessageId: string;
  id: string;
  lastSignal: "assistant_message" | "reasoning" | "tool_call" | null;
  lastStepContextTokens: number;
  messageIndex: number;
  pendingRapidRefills: number;
  preCompactTokens: number;
  prompt: string;
  reasoningIndex: number;
  retriedStreamInterrupt: boolean;
  retriedTokenGap: boolean;
  startedAt: number;
  stderr: string[];
  text: Map<string, string>;
  thinking: Map<string, string>;
  tools: Map<string, { input: unknown; name: string }>;
};

type ResultFrame = Extract<Frame, { event: "result" }>;
type ResultFailureAction = "retry_token_gap" | "sticky_recovered" | "retry_stream" | "fail";

function isCompactionTurn(turn: ActiveTurn): boolean {
  return turn.kind !== "business";
}

function isForegroundTurn(turn: ActiveTurn): boolean {
  return turn.kind !== "idle_compact";
}

function createTurn(
  kind: TurnKind,
  id: string,
  prompt: string,
  fields: Pick<ActiveTurn, "clientMessageId" | "nativeStartStep" | "preCompactTokens" | "startedAt"> &
    Partial<Pick<ActiveTurn, "pendingRapidRefills">>,
): ActiveTurn {
  return {
    kind,
    id,
    prompt,
    compactEventId: kind === "manual_compact" ? `${id}:manual-compact`
      : kind === "idle_compact" ? `${id}:auto-compact` : null,
    nativeCompacted: false,
    pendingRapidRefills: 0,
    lastStepContextTokens: 0,
    retriedTokenGap: false,
    retriedStreamInterrupt: false,
    activeMessageId: null,
    activeReasoningId: null,
    lastSignal: null,
    messageIndex: 0,
    reasoningIndex: 0,
    text: new Map(),
    thinking: new Map(),
    tools: new Map(),
    stderr: [],
    ...fields,
  };
}

/**
 * `agy` expands a leading `/name` into a slash command or skill unless it
 * runs with `--disable-slash-commands` ("pass --disable-slash-commands to send
 * /%s to the model as literal text"). Compaction needs expansion; any other
 * payload with a leading slash must reach the model literally. Every other
 * payload is indifferent (`null`), so one resident process serves ordinary
 * turns and `/compact` alike without being respawned.
 */
export function slashCommandRequirement(payload: string, compaction: boolean): boolean | null {
  if (compaction) return true;
  return /^\s*\//.test(payload) ? false : null;
}

function booleanOption(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function numberOption(
  value: unknown,
  fallback: number,
): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : fallback;
}

function stringOption(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function parseEnvFlagDisabled(value: string | undefined): boolean {
  if (!value) {
    return false;
  }
  const normalized = value.trim().toLowerCase();
  return (
    normalized === "1" ||
    normalized === "true" ||
    normalized === "yes" ||
    normalized === "on"
  );
}

function resolveEnvIdleCompactMinTokens(
  envValue: string | undefined,
): number | undefined {
  if (!envValue || !envValue.trim()) {
    return undefined;
  }
  const parsed = Number(envValue.trim());
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return undefined;
  }
  return Math.max(MIN_ENV_AUTO_COMPACT_TOKEN_THRESHOLD, Math.round(parsed));
}

export class Session {
  private compaction = emptyCompactionState();
  private compactionStateWritable = true;
  private readonly sessionId: string;
  private readonly sessionKey: string;
  private readonly command: string;
  private readonly baseArgs: string[];
  private readonly baseEnv: Record<string, string> | undefined;
  private readonly config: ProviderSessionConfig;
  private readonly catalog: ProviderDiscovery;
  private readonly emit: (event: ProviderEvent) => void;
  private readonly autoCompactEnabled: boolean;
  private readonly autoCompactLeadMs: number;
  private readonly autoCompactMaxLateMs: number;
  private readonly autoCompactTokenThreshold: number;
  private autoCompactPromise: Promise<void> | null = null;
  private autoCompactResolve: (() => void) | null = null;
  private autoCompactTimer: unknown = null;
  private cacheExpiresAt: number | null = null;
  private closed = false;
  private readonly contextWindowOverride: number | undefined;
  private conversationId?: string;
  private driver: DriverProcess | null = null;
  private driverReady: Promise<void> | null = null;
  private driverResolve: (() => void) | null = null;
  private driverSlashCommandsEnabled = false;
  private readonly hotCacheTtlMs: number;
  private lastCacheActivityAt: number | null = null;
  private lastHandledRequestAt: number | null = null;
  private lastIdleSkipReason: IdleCompactSkipReason | null = null;
  private lastRequestAt: number | null = null;
  private model: string | undefined;
  private mode: string;
  private readonly now: () => number;
  private readonly pendingItems: Array<{
    item: ProviderTimelineItem;
    timestamp: string;
  }> = [];
  private readonly seenResultErrors = new Set<string>();
  private readonly spawnImpl?: SpawnFunction;
  private readonly store: ConversationStore;
  private readonly streamInterruptRetryDelayMs: number;
  private systemPromptInjected = false;
  private contextGuidanceInjected = false;
  private readonly tokenGapRetryDelayMs: number;
  private turn: ActiveTurn | null = null;
  private turnIndex = 0;

  constructor(
    sessionId: string,
    command: string,
    baseArgs: string[],
    baseEnv: Record<string, string> | undefined,
    config: ProviderSessionConfig,
    persistence: ProviderPersistence | undefined,
    catalog: ProviderDiscovery,
    emit: (event: ProviderEvent) => void,
    options: SessionOptions = {},
  ) {
    this.sessionId = sessionId;
    this.command = command;
    this.baseArgs = baseArgs;
    this.baseEnv = baseEnv;
    this.config = config;
    this.catalog = catalog;
    this.emit = emit;
    this.now = options.now ?? Date.now;
    this.model = config.model ?? catalog.defaultModel;
    const requestedMode = (config.mode ?? catalog.defaultMode ?? DEFAULT_MODE).trim();
    if (!MODES.some((entry) => entry.id === requestedMode)) {
      throw new ProviderActionError(
        "INVALID_MODE",
        `Unknown Antigravity mode: ${requestedMode}`,
      );
    }
    this.mode = requestedMode;
    this.spawnImpl = options.spawn;

    const providerOpts = isRecord(config.providerOptions)
      ? config.providerOptions
      : {};
    const contextWindowOption = numberOption(
      providerOpts.contextWindowMaxTokens,
      0,
    );
    this.contextWindowOverride =
      contextWindowOption > 0 ? contextWindowOption : undefined;
    const storeDir =
      options.storeDir ?? stringOption(providerOpts.storeDir);
    const brainDir =
      options.brainDir ?? stringOption(providerOpts.brainDir);
    this.store =
      options.store ??
      new ConversationStore({
        ...(storeDir ? { storeDir } : {}),
        ...(brainDir ? { brainDir } : {}),
      });
    this.tokenGapRetryDelayMs =
      options.tokenGapRetryDelayMs ??
      numberOption(
        providerOpts.tokenGapRetryDelayMs,
        TOKEN_GAP_RETRY_DELAY_MS,
      );
    this.streamInterruptRetryDelayMs =
      options.streamInterruptRetryDelayMs ??
      numberOption(
        providerOpts.streamInterruptRetryDelayMs,
        STREAM_INTERRUPT_RETRY_DELAY_MS,
      );
    const disableCompactEnv =
      config.env?.DISABLE_COMPACT ??
      baseEnv?.DISABLE_COMPACT ??
      process.env.DISABLE_COMPACT;
    this.autoCompactEnabled =
      parseEnvFlagDisabled(disableCompactEnv)
        ? false
        : (options.autoCompactEnabled ??
          booleanOption(providerOpts.autoCompactEnabled, true));
    const envMinTokens = resolveEnvIdleCompactMinTokens(
      config.env?.CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS ??
        baseEnv?.CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS ??
        process.env.CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS,
    );
    this.autoCompactTokenThreshold =
      options.autoCompactTokenThreshold ??
      numberOption(
        providerOpts.autoCompactTokenThreshold,
        envMinTokens ?? DEFAULT_AUTO_COMPACT_TOKEN_THRESHOLD,
      );
    this.hotCacheTtlMs =
      options.hotCacheTtlMs ??
      numberOption(providerOpts.hotCacheTtlMs, DEFAULT_HOT_CACHE_TTL_MS);
    this.autoCompactLeadMs =
      options.autoCompactLeadMs ??
      numberOption(
        providerOpts.autoCompactLeadMs,
        DEFAULT_AUTO_COMPACT_LEAD_MS,
      );
    this.autoCompactMaxLateMs =
      options.autoCompactMaxLateMs ??
      numberOption(
        providerOpts.autoCompactMaxLateMs,
        DEFAULT_AUTO_COMPACT_MAX_LATE_MS,
      );

    const persistedData =
      persistence && isRecord(persistence.data) ? persistence.data : undefined;
    const persistedSessionKey =
      persistedData &&
      typeof persistedData.sessionKey === "string" &&
      persistedData.sessionKey.trim()
        ? persistedData.sessionKey.trim()
        : undefined;
    this.sessionKey = persistedSessionKey ?? sessionId;

    const persistedConversationId =
      persistedData &&
      typeof persistedData.conversationId === "string" &&
      persistedData.conversationId.trim()
        ? persistedData.conversationId.trim()
        : undefined;
    const resolvedConversationId =
      persistedConversationId ??
      (persistedSessionKey
        ? this.store.resolveConversationId(persistedSessionKey)
        : undefined);

    if (resolvedConversationId) {
      this.conversationId = resolvedConversationId;
      this.store.bindSessionKey(this.sessionKey, resolvedConversationId);
      this.systemPromptInjected = true;
      this.turnIndex = Math.max(
        this.turnIndex,
        this.store.maxTurnIndex(this.conversationId),
      );
      this.restoreCompaction();
    }
  }

  getStore(): ConversationStore {
    return this.store;
  }

  private persistCompaction(): boolean {
    if (!this.conversationId) return true;
    this.compactionStateWritable = this.store.saveCompaction(this.conversationId, this.compaction);
    return this.compactionStateWritable;
  }

  private restoreCompaction(): void {
    if (!this.conversationId) return;
    const activeLogicalTurn = this.compaction.logicalTurn;
    const inFlight = this.turn?.compactEventId
      ? this.compaction.events.find((e) => e.id === this.turn!.compactEventId) : undefined;
    this.compaction = this.store.loadCompaction(this.conversationId);
    reconcileNativeCompaction(this.compaction, this.store.readNativeCompaction(this.conversationId));
    recoverInterruptedCompacts(this.compaction, this.now());
    if (this.turn && !isCompactionTurn(this.turn)) {
      this.compaction.logicalTurn = Math.max(this.compaction.logicalTurn, activeLogicalTurn);
    }
    if (inFlight && !this.compaction.events.some((e) => e.id === inFlight.id)) {
      inFlight.baselineStep = this.compaction.nativeStep;
      this.compaction.events.push(inFlight);
    }
    for (const event of this.compaction.events) {
      const index = /turn-(\d+)/.exec(event.id)?.[1];
      if (index) this.turnIndex = Math.max(this.turnIndex, Number(index));
    }
    // Rehydration never emits a new turn, usage sample, or compaction timeline row.
    this.persistCompaction();
  }

  private reconcileCompactionHistory(): void {
    if (!this.conversationId) return;
    // `added` holds only checkpoints that no event carried before this call.
    const added = reconcileNativeCompaction(this.compaction, this.store.readNativeCompaction(this.conversationId));
    const turn = this.turn;
    if (turn) this.claimNativeCompacts(turn, added.filter((e) => e.nativeStep !== undefined &&
      this.isWithinTurn(turn, e.nativeStep, e.at)));
    this.persistCompaction();
  }

  /** A native checkpoint belongs to the active turn when it is newer than the
   * turn's starting step and did not happen before the turn began. */
  private isWithinTurn(turn: ActiveTurn, step: number, at: number): boolean {
    return step > turn.nativeStartStep && at >= turn.startedAt - 1_000;
  }

  /** Native checkpoints inside a business turn are announced as their own
   * compaction rows; compaction turns announce their single event on completion. */
  private claimNativeCompacts(turn: ActiveTurn, events: CompactEvent[]): void {
    if (events.length === 0) return;
    turn.nativeCompacted = true;
    if (!isCompactionTurn(turn)) for (const event of events) this.emitCompactEvent(event);
  }

  private emitCompactEvent(event: CompactEvent): void {
    this.emitItem({ id: event.id, type: "compaction", status: "completed",
      trigger: event.source === "manual" ? "manual" : "auto",
      ...(event.preTokens !== null ? { preTokens: event.preTokens } : {}),
    }, new Date(event.at || this.now()).toISOString());
  }

  getLastContextTokens(): number {
    return this.compaction.lastContextTokens;
  }

  hasCompactedSinceLastTurn(): boolean {
    return this.compaction.compactedSinceLastTurn;
  }

  getLastIdleSkipReason(): IdleCompactSkipReason | null {
    return this.lastIdleSkipReason;
  }

  isRapidRefillBreakerTripped(): boolean {
    return this.compaction.rapidRefillBreakerTripped;
  }

  isFailureBreakerOpen(): boolean {
    return this.compaction.consecutiveCompactFailures >= MAX_CONSECUTIVE_COMPACT_FAILURES;
  }

  getConsecutiveCompactFailures(): number {
    return this.compaction.consecutiveCompactFailures;
  }

  getConsecutiveRapidRefills(): number {
    return this.compaction.consecutiveRapidRefills;
  }

  private currentPersistence(): ProviderPersistence | undefined {
    if (!this.config.persist) {
      return undefined;
    }
    return {
      version: 1,
      data: {
        ...(this.conversationId ? { conversationId: this.conversationId } : {}),
        sessionKey: this.sessionKey,
      },
    };
  }

  open(
    requestId: string,
    capabilities: ProviderCapability[],
    history: "replay" | "skip" = "skip",
  ): void {
    this.emit({
      type: "session.opened",
      requestId,
      sessionId: this.sessionId,
      capabilities,
      restoration: "core",
      persistence: this.currentPersistence(),
      cwd: this.config.cwd,
      title: this.config.title,
    });
    this.emitConfig();
    this.emitModeNotice();
    this.emit({
      type: "session.commands",
      sessionId: this.sessionId,
      commands: [
        {
          name: "compact",
          description: "Compact conversation context into a checkpoint summary",
        },
      ],
    });

    if (history === "replay" && this.conversationId) {
      const records = this.store.load(this.conversationId);
      for (const record of records) {
        this.emit({
          type: "timeline.item",
          sessionId: this.sessionId,
          item: replayTimelineItem(record.item),
          ...(record.timestamp ? { timestamp: record.timestamp } : {}),
        });
      }
      this.turnIndex = Math.max(
        this.turnIndex,
        this.store.maxTurnIndex(this.conversationId),
      );
      console.info(
        `[antigravity-resilient] Replayed ${records.length} timeline items for conversation ${this.conversationId} (snapshot=${this.store.snapshotPath(this.conversationId)})`,
      );
    }

    this.emit({
      type: "session.ready",
      requestId,
      sessionId: this.sessionId,
    });
  }

  async configure(
    requestId: string,
    changes: ProviderConfigChanges,
    validateSelection: (model: string) => Promise<string | undefined>,
  ): Promise<void> {
    let restartDriver = false;
    if (changes.model !== undefined) {
      const nextModel = await validateSelection(changes.model ?? "");
      if (nextModel !== this.model) {
        this.model = nextModel;
        restartDriver = true;
      }
    }
    if (changes.mode !== undefined && changes.mode !== null) {
      const nextMode = changes.mode.trim();
      if (!MODES.some((entry) => entry.id === nextMode)) {
        throw new ProviderActionError(
          "INVALID_MODE",
          `Unknown Antigravity mode: ${nextMode}`,
        );
      }
      if (nextMode !== this.mode) {
        this.mode = nextMode;
        restartDriver = true;
      }
    }

    if (restartDriver && !this.turn) {
      await this.stopDriver();
    }

    this.emitConfig();
    this.emitModeNotice();
    this.emit({ type: "request.completed", requestId });
  }

  async prompt(prompt: ProviderPrompt): Promise<void> {
    this.clearAutoCompactTimer();

    if (this.turn && !isForegroundTurn(this.turn) && this.autoCompactPromise) {
      await this.autoCompactPromise;
    }

    if (this.closed) {
      return;
    }

    if (this.turn) {
      this.emit({
        type: "session.prompt_result",
        sessionId: this.sessionId,
        clientMessageId: prompt.clientMessageId,
        result: {
          type: "failed",
          error: {
            code: "TURN_ACTIVE",
            message:
              "Antigravity is already processing a turn in this session.",
          },
        },
      });
      return;
    }

    let prepared: { cliText: string; userText: string };
    let isManualCompact = false;
    try {
      const rawPreview = renderPrompt(prompt, undefined);
      isManualCompact = isCompactPromptText(rawPreview.userText);
      const isCommand = prompt.input.type === "command" || /^\/\S+/.test(rawPreview.userText.trim());
      if (isCommand) {
        prepared = rawPreview;
      } else {
        prepared = renderPrompt(
          prompt,
          [this.systemPromptInjected ? undefined : this.config.systemPrompt,
            this.contextGuidanceInjected ? undefined : CONTEXT_OUTPUT_GUIDANCE].filter(Boolean).join("\n\n"),
        );
        this.systemPromptInjected = true;
        this.contextGuidanceInjected = true;
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.emit({
        type: "session.prompt_result",
        sessionId: this.sessionId,
        clientMessageId: prompt.clientMessageId,
        result: {
          type: "failed",
          error: { code: "INVALID_PROMPT", message },
        },
      });
      return;
    }

    const turnId = `turn-${++this.turnIndex}`;
    const turn = createTurn(isManualCompact ? "manual_compact" : "business", turnId, prepared.cliText, {
      clientMessageId: prompt.clientMessageId,
      nativeStartStep: this.compaction.nativeStep,
      preCompactTokens: this.compaction.lastContextTokens,
      startedAt: this.now(),
    });
    this.turn = turn;
    if (turn.compactEventId) beginCompact(this.compaction, turn.compactEventId, "manual", turn.startedAt);
    else beginBusinessTurn(this.compaction);
    this.persistCompaction();

    this.emit({
      type: "session.prompt_result",
      sessionId: this.sessionId,
      clientMessageId: prompt.clientMessageId,
      result: { type: "turn", turnId },
    });
    this.emit({
      type: "session.turn",
      sessionId: this.sessionId,
      turnId,
      state: "started",
    });
    this.emitItem({
      id: prompt.clientMessageId
        ? `user:${prompt.clientMessageId}`
        : `user:${turnId}`,
      type: "user_message",
      text: prepared.userText,
      clientMessageId: prompt.clientMessageId,
    });

    await this.sendTurnPrompt(turn);
  }

  private recordCacheActivity(contextTokens?: number): void {
    const now = this.now();
    this.lastRequestAt = now;
    this.lastCacheActivityAt = now;
    this.cacheExpiresAt = now + this.hotCacheTtlMs;
    if (typeof contextTokens === "number" && contextTokens > 0) {
      this.compaction.lastContextTokens = contextTokens;
      this.compaction.contextAt = now;
    }
  }

  /**
   * Mirrors Claude Code's `I1t(postTokens)`: after compaction completes, clear
   * the prompt-cache request timestamps (`lastMainThreadRequest(null, null)`)
   * and update `lastContextTokens` to the post-compaction token estimate so the
   * pre-compaction cache read of the summarization call itself can never
   * re-trigger another compaction.
   */
  private recordPostCompaction(postCompactTokens: number): void {
    this.compaction.compactedSinceLastTurn = true;
    this.compaction.lastContextTokens = Math.max(0, postCompactTokens);
    this.lastRequestAt = null;
    this.lastCacheActivityAt = null;
    this.cacheExpiresAt = null;
  }

  private clearAutoCompactTimer(): void {
    if (this.autoCompactTimer !== null) {
      clearTimeout(this.autoCompactTimer);
      this.autoCompactTimer = null;
    }
  }

  /**
   * Evaluates Claude Code's `xjo` / `BKt` idle compaction gates.
   */
  private evaluateIdleCompactGate(
    armedRequestAt: number | null,
    armedFireAt: number | null,
    now: number,
  ): IdleCompactSkipReason | null {
    if (!this.autoCompactEnabled) {
      return "compaction_off";
    }
    if (this.closed) {
      return "stopping";
    }
    if (this.turn !== null) {
      return "busy";
    }
    if (!this.conversationId) {
      return "no_conversation";
    }
    if (this.compaction.compactedSinceLastTurn) {
      return "already_compacted";
    }
    if (this.compaction.consecutiveCompactFailures >= MAX_CONSECUTIVE_COMPACT_FAILURES) {
      return "failure_breaker_open";
    }
    if (this.compaction.rapidRefillBreakerTripped) {
      return "rapid_refill_breaker_open";
    }
    if (
      this.lastRequestAt === null ||
      this.lastCacheActivityAt === null ||
      this.cacheExpiresAt === null
    ) {
      return "no_cache_activity";
    }
    if (armedRequestAt !== null) {
      if (
        this.lastRequestAt > armedRequestAt ||
        this.lastHandledRequestAt === armedRequestAt
      ) {
        return "newer_request";
      }
    } else if (this.lastHandledRequestAt === this.lastRequestAt) {
      return "newer_request";
    }
    const lateMs =
      armedFireAt !== null ? Math.max(0, now - armedFireAt) : 0;
    const cacheWarm = now < this.cacheExpiresAt;
    if (!cacheWarm || lateMs > this.autoCompactMaxLateMs) {
      return "lapsed";
    }
    if (this.compaction.lastContextTokens < this.autoCompactTokenThreshold) {
      return "small";
    }
    return null;
  }

  private scheduleAutoCompactIfNeeded(): void {
    this.clearAutoCompactTimer();
    const now = this.now();
    const gateReason = this.evaluateIdleCompactGate(null, null, now);
    if (gateReason !== null) {
      this.lastIdleSkipReason = gateReason;
      return;
    }

    const armedRequestAt = this.lastRequestAt!;
    const expiresAt = this.cacheExpiresAt!;
    const fireAt = expiresAt - this.autoCompactLeadMs;
    const delayMs = Math.max(0, fireAt - now);
    const timer = setTimeout(() => {
      this.autoCompactTimer = null;
      void this.runAutoCompaction(armedRequestAt, fireAt);
    }, delayMs);
    if (
      delayMs >= 1_000 &&
      typeof timer === "object" &&
      timer !== null &&
      "unref" in timer &&
      typeof (timer as { unref?: () => void }).unref === "function"
    ) {
      (timer as { unref: () => void }).unref();
    }
    this.autoCompactTimer = timer;
  }

  private async runAutoCompaction(
    armedRequestAt: number,
    armedFireAt: number,
  ): Promise<void> {
    const now = this.now();
    const gateReason = this.evaluateIdleCompactGate(
      armedRequestAt,
      armedFireAt,
      now,
    );
    if (gateReason !== null) {
      this.lastIdleSkipReason = gateReason;
      if (gateReason !== "busy" && gateReason !== "newer_request") {
        this.lastHandledRequestAt = armedRequestAt;
      }
      if (gateReason === "lapsed") {
        console.info(
          `[antigravity-resilient] Skipping idle auto-compaction on conversation ${this.conversationId ?? "unknown"}: prompt cache lapsed (lateMs=${Math.max(0, now - armedFireAt)}, cacheWarm=${this.cacheExpiresAt !== null && now < this.cacheExpiresAt})`,
        );
      }
      return;
    }

    this.lastHandledRequestAt = armedRequestAt;
    this.lastIdleSkipReason = null;

    // Claude Code's rapid-refill thrashing circuit breaker (`vmr` / `_Bo`),
    // measured in agy model requests since the last compact.
    const nextRapidRefills =
      this.compaction.hasCompactedInSession &&
      withinRapidRefillWindow(this.compaction)
        ? this.compaction.consecutiveRapidRefills + 1
        : 0;
    if (nextRapidRefills >= RAPID_REFILL_BREAKER_LIMIT) {
      this.compaction.consecutiveRapidRefills = nextRapidRefills;
      this.compaction.rapidRefillBreakerTripped = true;
      this.persistCompaction();
      this.lastIdleSkipReason = "rapid_refill_breaker_open";
      console.warn(
        `[antigravity-resilient] autocompact: rapid-refill breaker tripped on conversation ${this.conversationId} — ${nextRapidRefills} consecutive refills within <${RAPID_REFILL_REQUEST_WINDOW} model requests each (last was ${requestsSinceLastCompact(this.compaction)} requests)`,
      );
      this.emitItem({
        id: `turn-${this.turnIndex}:autocompact-thrashing:${nextRapidRefills}`,
        type: "notification",
        level: "warning",
        message: AUTOCOMPACT_THRASHING_MESSAGE,
      });
      this.flushStore();
      return;
    }

    const turnId = `turn-${++this.turnIndex}`;
    const preCompactTokens = this.compaction.lastContextTokens;
    const turn = createTurn("idle_compact", turnId, AUTO_COMPACT_COMMAND, {
      clientMessageId: "",
      nativeStartStep: this.compaction.nativeStep,
      pendingRapidRefills: nextRapidRefills,
      preCompactTokens,
      startedAt: this.now(),
    });
    this.turn = turn;
    beginCompact(this.compaction, turn.compactEventId!, "idle", turn.startedAt);
    // Never launch an automatic repeat if its in-flight marker could not be
    // written. Ordinary work and explicit /compact remain available.
    if (!this.persistCompaction()) {
      this.compaction.events = this.compaction.events.filter((e) => e.id !== turn.compactEventId);
      this.turn = null;
      this.lastIdleSkipReason = "state_unavailable";
      return;
    }

    let resolveCompact: (() => void) | null = null;
    this.autoCompactPromise = new Promise<void>((resolve) => {
      resolveCompact = resolve;
    });
    this.autoCompactResolve = () => {
      resolveCompact?.();
      resolveCompact = null;
      this.autoCompactPromise = null;
      this.autoCompactResolve = null;
    };

    console.info(
      `[antigravity-resilient] Starting pre-cache-expiry auto-compaction on conversation ${this.conversationId} (contextTokens=${preCompactTokens}, threshold=${this.autoCompactTokenThreshold})`,
    );

    await this.sendTurnPrompt(turn);
  }

  private async sendTurnPrompt(
    turn: ActiveTurn,
    promptOverride?: string,
  ): Promise<void> {
    try {
      const payload = promptOverride ?? turn.prompt;
      await this.ensureDriver(slashCommandRequirement(payload, isCompactionTurn(turn)));
      if (!this.driver) {
        throw new Error("Antigravity CLI driver did not start");
      }
      await this.driver.sendLine(
        JSON.stringify({
          event: "user",
          message: { content: payload },
          role: "user",
          content: payload,
        }),
      );
      await this.driverReady;
    } catch (error) {
      if (this.turn === turn) {
        const message = diagnostic(
          error instanceof Error ? error.message : String(error),
        );
        this.failTurn(message);
      }
    }
  }

  private async retryTurnAfterRecovery(
    turn: ActiveTurn,
    hadProgress: boolean,
    delayMs: number,
    reason: string,
  ): Promise<void> {
    const retryPrompt =
      isCompactionTurn(turn) || !hadProgress
        ? turn.prompt
        : TOKEN_GAP_CONTINUE_PROMPT;
    turn.activeMessageId = null;
    turn.activeReasoningId = null;
    turn.lastSignal = null;
    turn.stderr = [];
    this.flushStore();

    console.warn(
      `[antigravity-resilient] ${reason} on conversation ${this.conversationId ?? "unknown"}; retrying once in ${delayMs}ms (hadProgress=${hadProgress})`,
    );

    await this.stopDriver();
    if (this.turn !== turn || this.closed) {
      return;
    }
    if (delayMs > 0) {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, delayMs);
      });
    }
    if (this.turn !== turn || this.closed) {
      return;
    }
    await this.sendTurnPrompt(turn, retryPrompt);
  }

  async interrupt(requestId: string): Promise<void> {
    this.clearAutoCompactTimer();
    const activeTurn = this.turn;
    recoverInterruptedCompacts(this.compaction, this.now());
    this.turn = null;
    this.autoCompactResolve?.();
    this.flushStore();
    await this.stopDriver();
    if (activeTurn && isForegroundTurn(activeTurn)) {
      this.emit({
        type: "session.turn",
        sessionId: this.sessionId,
        turnId: activeTurn.id,
        state: "canceled",
      });
    }
    this.emit({ type: "request.completed", requestId });
  }

  async close(requestId: string): Promise<void> {
    await this.dispose();
    this.emit({
      type: "session.closed",
      sessionId: this.sessionId,
    });
    this.emit({ type: "request.completed", requestId });
  }

  async dispose(): Promise<void> {
    this.closed = true;
    this.clearAutoCompactTimer();
    recoverInterruptedCompacts(this.compaction, this.now());
    this.turn = null;
    this.autoCompactResolve?.();
    this.flushStore();
    await this.stopDriver();
  }

  private buildDriverArgs(enableSlashCommands = false): string[] {
    const args = [
      ...this.baseArgs,
      "--input-format",
      "stream-json",
      "--output-format",
      "stream-json",
      "--add-dir",
      this.config.cwd,
    ];
    if (!enableSlashCommands) {
      args.push("--disable-slash-commands");
    }
    if (this.mode === "full-access") {
      args.push("--dangerously-skip-permissions");
    }
    if (this.model) {
      args.push("--model", this.model);
    }
    if (this.conversationId) {
      args.push("--conversation", this.conversationId);
    }
    return args;
  }

  /**
   * Reuses the resident process unless the payload needs the other slash
   * mode (see `slashCommandRequirement`). A fresh process starts with slash
   * commands enabled, so the next `/compact` reuses it.
   */
  private async ensureDriver(slashCommands: boolean | null = null): Promise<void> {
    if (this.driver && slashCommands !== null && this.driverSlashCommandsEnabled !== slashCommands) {
      await this.stopDriver();
    }
    if (this.driver) {
      return;
    }
    const enableSlashCommands = slashCommands ?? true;

    let resolveInit: (() => void) | null = null;
    this.driverReady = new Promise<void>((resolve) => {
      resolveInit = resolve;
    });
    this.driverResolve = () => {
      resolveInit?.();
      resolveInit = null;
    };

    const driver = spawnDriver({
      command: this.command,
      args: this.buildDriverArgs(enableSlashCommands),
      cwd: this.config.cwd,
      env: {
        ...this.baseEnv,
        ...this.config.env,
      },
      spawn: this.spawnImpl,
    });
    this.driver = driver;
    this.driverSlashCommandsEnabled = enableSlashCommands;

    driver.onStdoutLine((line) => {
      this.handleStdoutLine(line);
    });
    driver.onStderrLine((line) => {
      const trimmed = line.trim();
      if (trimmed) {
        this.turn?.stderr.push(trimmed);
      }
    });
    driver.onError((error) => {
      if (this.driver !== driver) {
        return;
      }
      this.driver = null;
      this.driverResolve?.();
      const message = diagnostic(error.message);
      if (this.turn) {
        this.failTurn(message);
      } else if (!this.closed) {
        this.emit({
          type: "session.runtime_failed",
          sessionId: this.sessionId,
          error: { message },
        });
      }
    });
    driver.onClose((code, signal) => {
      if (this.driver !== driver) {
        return;
      }
      this.driver = null;
      this.driverResolve?.();
      if (this.turn && !this.closed) {
        const stderr = this.turn.stderr.join("\n").trim();
        const reason =
          stderr ||
          `Antigravity CLI exited unexpectedly (${
            code !== null ? `code ${code}` : `signal ${signal ?? "unknown"}`
          })`;
        this.failTurn(diagnostic(reason));
      }
    });
  }

  private async stopDriver(): Promise<void> {
    const current = this.driver;
    this.driver = null;
    this.driverSlashCommandsEnabled = false;
    this.driverResolve?.();
    this.driverResolve = null;
    this.driverReady = null;
    if (current) {
      await current.stop();
    }
  }

  private isStickyRecoveredResultError(
    errors: string[],
    responseText: string | undefined,
    nativeCompacted = false,
  ): boolean {
    if (!nativeCompacted && (!responseText || !responseText.trim())) {
      return false;
    }
    if (errors.length === 0) {
      return false;
    }
    return errors.every(
      (entry) =>
        isStreamInterruption(entry) || this.seenResultErrors.has(entry),
    );
  }

  private handleStdoutLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) {
      return;
    }

    const deniedMatch = /^Action denied:\s*(.+)$/i.exec(trimmed);
    if (deniedMatch && this.turn) {
      this.emitItem({
        id: `${this.turn.id}:denied:${this.turn.tools.size + 1}`,
        type: "notification",
        level: "warning",
        message: `Antigravity blocked action in ${this.mode} mode: ${deniedMatch[1]}`,
      });
      return;
    }

    const frame = parseFrame(trimmed);
    if (!frame) {
      return;
    }

    const nextId = frameConversationId(frame);
    if (nextId) {
      const changed = nextId !== this.conversationId;
      this.conversationId = nextId;
      this.store.bindSessionKey(this.sessionKey, nextId);
      this.flushPendingItemsToStore();
      if (changed) {
        this.restoreCompaction();
        this.turnIndex = Math.max(
          this.turnIndex,
          this.store.maxTurnIndex(nextId),
        );
        const persistence = this.currentPersistence();
        if (persistence) {
          this.emit({
            type: "session.persistence",
            sessionId: this.sessionId,
            persistence,
          });
          this.emitConfig();
        }
      }
    }

    if (frame.event === "init") {
      this.driverResolve?.();
      return;
    }

    const turn = this.turn;
    if (!turn) {
      return;
    }

    if (frame.event === "result") {
      this.handleResult(turn, frame);
      return;
    }
    if (frame.event === "step_update" && this.handleNativeCheckpoint(turn, frame)) {
      return;
    }
    // A background compaction is silent: only its checkpoint and result matter.
    if (!isForegroundTurn(turn)) {
      return;
    }

    switch (frame.event) {
      case "step_update": {
        const step = frame.step_update;
        if (!step) {
          return;
        }
        const id = `${turn.id}:step:${step.step_index}`;
        const timestamp =
          typeof frame.timestamp === "string" ? frame.timestamp : undefined;
        if (step.step_type === "agent_response") {
          const delta =
            typeof step.text_delta === "string" ? step.text_delta : "";
          const merged = `${turn.text.get(id) ?? ""}${delta}`;
          if (delta) {
            turn.text.set(id, merged);
            this.emitItem(
              {
                id,
                messageId: id,
                type: "assistant_message",
                text: merged,
              },
              timestamp,
            );
          }
          if (step.state === "DONE" && step.usage) {
            const stepTokens =
              (step.usage.input_tokens ?? 0) +
              (step.usage.cache_read_tokens ?? 0) +
              (step.usage.output_tokens ?? 0);
            if (!isCompactionTurn(turn)) {
              if (stepTokens > 0) {
                turn.lastStepContextTokens = stepTokens;
              }
              this.recordCacheActivity(stepTokens);
              this.compaction.contextStep = Math.max(this.compaction.contextStep, step.step_index);
            }
            this.emit({
              type: "session.usage",
              sessionId: this.sessionId,
              turnId: turn.id,
              usage: {
                inputTokens: step.usage.input_tokens,
                outputTokens: step.usage.output_tokens,
                cachedInputTokens: step.usage.cache_read_tokens ?? 0,
                ...(stepTokens > 0
                  ? {
                      contextWindowUsedTokens: stepTokens,
                      contextWindowMaxTokens: this.contextWindowMaxTokens(),
                    }
                  : {}),
              },
            });
          }
          return;
        }
        if (step.step_type === "thinking" || step.step_type === "reasoning") {
          const delta =
            typeof step.text_delta === "string" ? step.text_delta : "";
          if (!delta) {
            return;
          }
          const merged = `${turn.thinking.get(id) ?? ""}${delta}`;
          turn.thinking.set(id, merged);
          this.emitItem(
            {
              id,
              type: "reasoning",
              text: merged,
            },
            timestamp,
          );
          return;
        }
        if (step.step_type === "tool" || step.step_type === "subagent") {
          const name =
            step.tool_name || step.tool_info?.name || "Antigravity tool";
          const params = step.tool_info?.parameters ?? {};
          const output = step.tool_info?.output;
          const detail = toolDetail(name, params, output);
          turn.tools.set(id, { name, input: params });
          if (step.state === "ERROR") {
            const errText =
              step.tool_info?.error?.message || "Antigravity tool failed";
            this.emitItem(
              {
                id,
                type: "tool_call",
                callId: id,
                name,
                detail,
                status: "failed",
                error: errText,
              },
              timestamp,
            );
          } else {
            this.emitItem(
              {
                id,
                type: "tool_call",
                callId: id,
                name,
                detail,
                status: step.state === "ACTIVE" ? "running" : "completed",
                error: null,
              },
              timestamp,
            );
          }
        }
        return;
      }

      case "thinking": {
        const content = typeof frame.content === "string" ? frame.content : "";
        if (!content) {
          return;
        }
        const id =
          turn.lastSignal === "reasoning" && turn.activeReasoningId
            ? turn.activeReasoningId
            : `${turn.id}:reasoning:${++turn.reasoningIndex}`;
        turn.activeReasoningId = id;
        turn.activeMessageId = null;
        turn.lastSignal = "reasoning";
        const merged = `${turn.thinking.get(id) ?? ""}${content}`;
        turn.thinking.set(id, merged);
        this.emitItem(
          {
            id,
            type: "reasoning",
            text: merged,
          },
          typeof frame.timestamp === "string" ? frame.timestamp : undefined,
        );
        return;
      }

      case "message": {
        if (frame.role && frame.role !== "assistant") {
          return;
        }
        const content = typeof frame.content === "string" ? frame.content : "";
        if (!content) {
          return;
        }
        const id =
          turn.lastSignal === "assistant_message" && turn.activeMessageId
            ? turn.activeMessageId
            : `${turn.id}:message:${++turn.messageIndex}`;
        turn.activeMessageId = id;
        turn.activeReasoningId = null;
        turn.lastSignal = "assistant_message";
        const merged = `${turn.text.get(id) ?? ""}${content}`;
        turn.text.set(id, merged);
        this.emitItem(
          {
            id,
            type: "assistant_message",
            text: merged,
          },
          typeof frame.timestamp === "string" ? frame.timestamp : undefined,
        );
        return;
      }

      case "tool_call": {
        const callId =
          typeof frame.id === "string" && frame.id.trim()
            ? frame.id.trim()
            : `${turn.id}:tool:${turn.tools.size + 1}`;
        const name =
          typeof frame.name === "string" && frame.name.trim()
            ? frame.name.trim()
            : "tool";
        turn.activeMessageId = null;
        turn.activeReasoningId = null;
        turn.lastSignal = "tool_call";
        turn.tools.set(callId, { name, input: frame.arguments });
        this.emitItem(
          {
            id: `tool:${callId}`,
            type: "tool_call",
            callId,
            name,
            status: "running",
            detail: toolDetail(name, frame.arguments),
            error: null,
          },
          typeof frame.timestamp === "string" ? frame.timestamp : undefined,
        );
        return;
      }

      case "tool_result": {
        const callId =
          typeof frame.id === "string" && frame.id.trim()
            ? frame.id.trim()
            : `${turn.id}:tool:${turn.tools.size}`;
        const existing = turn.tools.get(callId);
        const name =
          (typeof frame.name === "string" && frame.name.trim()) ||
          existing?.name ||
          "tool";
        const output = toolOutput(frame);
        const detail = toolDetail(name, existing?.input, output);
        const failed = frame.is_error === true || Boolean(frame.error);
        const timestamp =
          typeof frame.timestamp === "string" ? frame.timestamp : undefined;
        if (failed) {
          this.emitItem(
            {
              id: `tool:${callId}`,
              type: "tool_call",
              callId,
              name,
              status: "failed",
              detail,
              error: frame.error ?? output ?? "Tool execution failed",
            },
            timestamp,
          );
        } else {
          this.emitItem(
            {
              id: `tool:${callId}`,
              type: "tool_call",
              callId,
              name,
              status: "completed",
              detail,
              error: null,
            },
            timestamp,
          );
        }
        return;
      }
    }
  }

  /** Handles a native CHECKPOINT for every turn kind; false for other steps. */
  private handleNativeCheckpoint(
    turn: ActiveTurn,
    frame: Extract<Frame, { event: "step_update" }>,
  ): boolean {
    const observation = nativeObservation(frame);
    if (observation?.kind !== "checkpoint") {
      return false;
    }
    if (!observation.at) observation.at = this.now();
    const added = reconcileNativeCompaction(this.compaction, [observation], false);
    if (added.length && this.isWithinTurn(turn, observation.step, observation.at)) {
      turn.lastStepContextTokens = this.postCompactContextTokens();
      this.recordPostCompaction(turn.lastStepContextTokens);
      this.claimNativeCompacts(turn, added);
      this.persistCompaction();
      this.emitContextUsage(turn, turn.lastStepContextTokens);
    }
    return true;
  }

  private handleResult(turn: ActiveTurn, frame: ResultFrame): void {
    this.reconcileCompactionHistory();
    if (!isResultSuccess(frame) && !this.recoverFailedResult(turn, frame)) {
      return;
    }
    const responseText = resultResponse(frame);
    if (isForegroundTurn(turn) && responseText?.trim() && turn.text.size === 0) {
      const id = `${turn.id}:message:${++turn.messageIndex}`;
      turn.text.set(id, responseText);
      this.emitItem(
        { id, type: "assistant_message", text: responseText },
        typeof frame.timestamp === "string" ? frame.timestamp : undefined,
      );
    }
    this.emitUsage(turn, frame);
    this.completeTurn();
  }

  /**
   * Order matters: an OAuth refresh race retries first; a stream error that
   * is sticky (the turn produced a response, or its native checkpoint already
   * landed) completes the turn; an unrecovered stream interruption retries
   * once, but never re-sends `/compact` after a native checkpoint.
   */
  private classifyResultFailure(
    turn: ActiveTurn,
    errors: string[],
    rawError: string,
    responseText: string | undefined,
  ): ResultFailureAction {
    const resumable = Boolean(this.conversationId);
    if (!turn.retriedTokenGap && resumable && isTokenGap(rawError)) {
      return "retry_token_gap";
    }
    if (this.isStickyRecoveredResultError(errors, responseText, turn.nativeCompacted)) {
      return "sticky_recovered";
    }
    if (!turn.nativeCompacted && !turn.retriedStreamInterrupt && resumable && isStreamInterruption(rawError)) {
      return "retry_stream";
    }
    return "fail";
  }

  /** Only the recoverable class may become historical noise. Unrelated errors
   * in the same result must still fail a later turn. */
  private markRecoveredErrors(errors: string[], recoverable: (entry: string) => boolean): void {
    for (const entry of errors) {
      if (recoverable(entry)) this.seenResultErrors.add(entry);
    }
  }

  /** Returns true when the failed result still completes the turn; otherwise
   * a retry has been scheduled or the turn has failed. */
  private recoverFailedResult(turn: ActiveTurn, frame: ResultFrame): boolean {
    const errors = resultErrors(frame);
    const rawError = resultError(frame);
    const hadProgress = turn.text.size > 0 || turn.tools.size > 0;
    switch (this.classifyResultFailure(turn, errors, rawError, resultResponse(frame))) {
      case "retry_token_gap":
        turn.retriedTokenGap = true;
        this.markRecoveredErrors(errors, isTokenGap);
        void this.retryTurnAfterRecovery(turn, hadProgress, this.tokenGapRetryDelayMs, "OAuth 401 token refresh race");
        return false;
      case "sticky_recovered":
        this.markRecoveredErrors(errors, isStreamInterruption);
        console.info(
          `[antigravity-resilient] Ignoring sticky/recovered trajectory error on completed turn for conversation ${this.conversationId ?? "new"}: ${rawError}`,
        );
        return true;
      case "retry_stream":
        turn.retriedStreamInterrupt = true;
        this.markRecoveredErrors(errors, isStreamInterruption);
        void this.retryTurnAfterRecovery(turn, hadProgress, this.streamInterruptRetryDelayMs, "Transient stream interruption");
        return false;
      case "fail":
        this.failTurn(diagnostic(rawError));
        return false;
    }
  }

  private emitUsage(
    turn: ActiveTurn,
    frame: Extract<Frame, { event: "result" }>,
  ): void {
    // Current `agy` builds report the conversation's cumulative totals under
    // `result.usage`; `stats` is a legacy top-level shape kept for replayed
    // transcripts. Neither is a context size once step usage is known.
    const stats = frame.result?.usage ?? frame.stats;

    if (isCompactionTurn(turn)) {
      // `/compact` leaves the totals unchanged. Settled into the compaction
      // event by `completeCompactionTurn`.
      turn.lastStepContextTokens = this.postCompactContextTokens();
      this.emitContextUsage(turn, turn.lastStepContextTokens, stats);
      return;
    }
    if (!stats) {
      return;
    }

    const statsTokens =
      typeof stats.total_tokens === "number" && stats.total_tokens > 0
        ? stats.total_tokens
        : (stats.input_tokens ?? 0) +
          (stats.cache_read_tokens ?? 0) +
          (stats.output_tokens ?? 0);
    // After a native checkpoint the reconciled transcript is exact once a
    // later request lands; until then the turn holds the post-compact estimate.
    const contextTokens =
      turn.nativeCompacted ? this.compaction.lastContextTokens || turn.lastStepContextTokens
        : turn.lastStepContextTokens > 0 ? turn.lastStepContextTokens : statsTokens;
    if (!turn.nativeCompacted || contextTokens > 0) this.recordCacheActivity(contextTokens);
    this.emitContextUsage(turn, contextTokens, stats);
  }

  /** Estimated context right after a compaction; 0 when the native
   * transcript has no model request to anchor it. */
  private postCompactContextTokens(): number {
    if (!this.conversationId) return 0;
    return estimatePostCompactTokens(this.store.readNativeCompaction(this.conversationId)) ?? 0;
  }

  /**
   * Paseo's context meter shows the latest `session.usage`, whatever its turn.
   * Background compactions have no Paseo turn, so their sample has no
   * `turnId`. An unknown size (0) clears the meter rather than leaving the
   * pre-compaction value on it.
   */
  private emitContextUsage(turn: ActiveTurn, contextTokens: number, stats?: TokenUsage): void {
    this.emit({
      type: "session.usage",
      sessionId: this.sessionId,
      ...(isForegroundTurn(turn) ? { turnId: turn.id } : {}),
      usage: {
        ...(stats ? {
          inputTokens: stats.input_tokens,
          cachedInputTokens: stats.cache_read_tokens,
          outputTokens: stats.output_tokens,
        } : {}),
        ...(contextTokens > 0 ? {
          contextWindowUsedTokens: contextTokens,
          contextWindowMaxTokens: this.contextWindowMaxTokens(),
        } : {}),
      },
    });
  }

  /**
   * Context window advertised to the client for the usage meter. Prefers an
   * explicit `providerOptions.contextWindowMaxTokens`, then the catalogue
   * value (filled in by `getCatalog`), then the model-family default.
   */
  private contextWindowMaxTokens(): number {
    if (this.contextWindowOverride !== undefined) {
      return this.contextWindowOverride;
    }
    const catalogMax = this.catalog.models.find(
      (model) => model.id === this.model,
    )?.contextWindowMaxTokens;
    if (typeof catalogMax === "number" && catalogMax > 0) {
      return catalogMax;
    }
    return resolveContextWindowMaxTokens(this.model);
  }

  private flushPendingItemsToStore(): void {
    if (!this.conversationId || this.pendingItems.length === 0) {
      return;
    }
    const items = this.pendingItems.splice(0, this.pendingItems.length);
    for (const entry of items) {
      this.store.upsert(this.conversationId, entry.item, entry.timestamp);
    }
  }

  private flushStore(): void {
    if (!this.conversationId) {
      return;
    }
    this.flushPendingItemsToStore();
    this.persistCompaction();
    this.store.flush(this.conversationId);
  }

  private emitItem(item: ProviderTimelineItem, timestamp?: string): void {
    const ts = timestamp ?? new Date().toISOString();
    if (this.conversationId) {
      this.flushPendingItemsToStore();
      this.store.upsert(this.conversationId, item, ts);
    } else {
      const existingIdx = this.pendingItems.findIndex(
        (entry) => entry.item.id === item.id,
      );
      if (existingIdx >= 0) {
        this.pendingItems[existingIdx] = {
          item,
          timestamp: this.pendingItems[existingIdx]?.timestamp ?? ts,
        };
      } else {
        this.pendingItems.push({ item, timestamp: ts });
      }
    }

    this.emit({
      type: "timeline.item",
      sessionId: this.sessionId,
      item,
      timestamp: ts,
    });
  }

  private emitConfig(): void {
    this.emit({
      type: "session.config",
      sessionId: this.sessionId,
      config: {
        model: this.model,
        mode: this.mode,
        thinkingOption: undefined,
        models: this.catalog.models,
        modes: this.catalog.modes,
        thinkingOptions: [],
        settings: [],
      },
    });
  }

  private emitModeNotice(): void {
    this.emit({
      type: "session.notice",
      sessionId: this.sessionId,
      notice: {
        id: "antigravity-mode",
        severity: "warning",
        title: "Full Access enabled",
        description:
          "Antigravity is running with --dangerously-skip-permissions.",
      },
    });
  }

  /**
   * Settles a compaction turn's event. Success records the post-compact size
   * and announces the one compaction row; failure counts toward the failure
   * breaker, and a failed idle attempt is not repeated before a real turn.
   */
  private completeCompactionTurn(turn: ActiveTurn, success: boolean): void {
    if (!turn.compactEventId) return;
    if (!success) {
      finishCompact(this.compaction, turn.compactEventId, false, this.now());
      if (turn.kind === "idle_compact") this.compaction.compactedSinceLastTurn = true;
      return;
    }
    const event = finishCompact(this.compaction, turn.compactEventId, true, this.now(), turn.lastStepContextTokens);
    if (event) this.emitCompactEvent(event);
    this.recordPostCompaction(event?.postTokens ?? turn.lastStepContextTokens);
  }

  /** Detaches the active turn, settles its compaction, persists, and reports
   * the outcome to the client. Background compacts never emit `session.turn`. */
  private endTurn(outcome: "completed" | "failed", message?: string): ActiveTurn | null {
    const turn = this.turn;
    if (!turn) return null;
    this.clearAutoCompactTimer();
    this.completeCompactionTurn(turn, outcome === "completed");
    this.turn = null;
    this.flushStore();
    if (isForegroundTurn(turn)) {
      this.emit({
        type: "session.turn",
        sessionId: this.sessionId,
        turnId: turn.id,
        state: outcome,
        ...(message !== undefined ? { error: { message } } : {}),
      });
    }
    return turn;
  }

  private completeTurn(): void {
    const turn = this.endTurn("completed");
    if (!turn) return;
    if (isCompactionTurn(turn)) {
      this.autoCompactResolve?.();
      return;
    }
    this.compaction.compactedSinceLastTurn = turn.nativeCompacted;
    this.persistCompaction();
    this.scheduleAutoCompactIfNeeded();
  }

  private failTurn(message: string): void {
    const turn = this.endTurn("failed", message);
    if (!turn || isForegroundTurn(turn)) return;
    if (this.compaction.consecutiveCompactFailures >= MAX_CONSECUTIVE_COMPACT_FAILURES) {
      console.warn(
        `[antigravity-resilient] autocompact: circuit breaker tripped after ${this.compaction.consecutiveCompactFailures} consecutive failures — skipping automatic attempts until a successful compact`,
      );
    } else {
      console.warn(
        `[antigravity-resilient] Auto-compaction failed on conversation ${this.conversationId ?? "unknown"} (${this.compaction.consecutiveCompactFailures}/${MAX_CONSECUTIVE_COMPACT_FAILURES}): ${message}`,
      );
    }
    this.autoCompactResolve?.();
  }
}
