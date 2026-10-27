import { isRecord } from "./wire.ts";

/**
 * Rapid-refill breaker. Distance is counted in agy model requests (native
 * PLANNER_RESPONSE/AGENT_RESPONSE steps), not Paseo prompts: one prompt can run
 * ~100 steps, and refilling over that much work is normal, not thrashing.
 * Local transcripts: median 58 requests between compacts, 90% >= 22; the
 * fast refills (< 10) came from very large tool outputs.
 */
export const RAPID_REFILL_REQUEST_WINDOW = 10;
export const RAPID_REFILL_BREAKER_LIMIT = 3;

export type CompactSource = "native" | "manual" | "idle";
export type CompactEvent = {
  id: string;
  source: CompactSource;
  status: "pending" | "completed" | "failed" | "interrupted";
  at: number;
  finishedAt?: number;
  logicalTurn: number;
  baselineStep: number;
  nativeStep?: number;
  preTokens: number | null;
  postTokens: number | null;
  /** Native model requests before this compact; absent when unknown. */
  requestIndex?: number;
  /** Old timeline records contain completion time, not request start time. */
  legacyCompletedAt?: number;
};

export type CompactionState = {
  version: 1;
  events: CompactEvent[];
  logicalTurn: number;
  nativeStep: number;
  contextStep: number;
  contextAt: number;
  lastContextTokens: number;
  compactedSinceLastTurn: boolean;
  hasCompactedInSession: boolean;
  consecutiveCompactFailures: number;
  consecutiveRapidRefills: number;
  rapidRefillBreakerTripped: boolean;
  turnsSinceLastCompact: number;
  lastCompactId: string | null;
  lastFailureAt: number;
  /** Native model requests seen in the transcript; -1 until one is seen. */
  modelRequests: number;
};

export function emptyCompactionState(): CompactionState {
  return { version: 1, events: [], logicalTurn: 0, nativeStep: -1, contextStep: -1,
    contextAt: 0, lastContextTokens: 0, compactedSinceLastTurn: false,
    hasCompactedInSession: false, consecutiveCompactFailures: 0,
    consecutiveRapidRefills: 0, rapidRefillBreakerTripped: false,
    turnsSinceLastCompact: 0, lastCompactId: null, lastFailureAt: 0, modelRequests: -1 };
}

/** Fields added after version 1 shipped; files from older builds lack them. */
const ADDED_FIELDS: Partial<CompactionState> = { modelRequests: -1 };

export function validCompactionState(value: unknown): value is CompactionState {
  if (!isRecord(value) || value.version !== 1 || !Array.isArray(value.events)) return false;
  const defaults = emptyCompactionState();
  for (const [key, fallback] of Object.entries(defaults)) {
    if (value[key] === undefined && key in ADDED_FIELDS) continue;
    if (typeof fallback === "number" && (typeof value[key] !== "number" || !Number.isFinite(value[key]))) return false;
    if (typeof fallback === "boolean" && typeof value[key] !== "boolean") return false;
  }
  if (value.lastCompactId !== null && typeof value.lastCompactId !== "string") return false;
  return value.events.every((e) => isRecord(e) && typeof e.id === "string" &&
    ["native", "manual", "idle"].includes(String(e.source)) &&
    ["pending", "completed", "failed", "interrupted"].includes(String(e.status)) &&
    [e.at, e.logicalTurn, e.baselineStep].every((n) => typeof n === "number" && Number.isFinite(n)) &&
    [e.preTokens, e.postTokens].every((n) => n === null || (typeof n === "number" && Number.isFinite(n) && n >= 0)) &&
    (e.nativeStep === undefined || (typeof e.nativeStep === "number" && Number.isSafeInteger(e.nativeStep))) &&
    (e.requestIndex === undefined || (typeof e.requestIndex === "number" && Number.isSafeInteger(e.requestIndex) && e.requestIndex >= 0)) &&
    (e.finishedAt === undefined || (typeof e.finishedAt === "number" && Number.isFinite(e.finishedAt))) &&
    (e.legacyCompletedAt === undefined || (typeof e.legacyCompletedAt === "number" && Number.isFinite(e.legacyCompletedAt))));
}

/** A valid stored state with fields older builds did not write filled in. */
export function parseCompactionState(value: unknown): CompactionState | null {
  return validCompactionState(value) ? { ...ADDED_FIELDS, ...value } : null;
}

export type NativeObservation = {
  step: number;
  at: number;
  kind: "user" | "checkpoint" | "usage";
  tokens?: number;
  manual?: boolean;
};

/** Metadata only. Native transcript fields are flat; stream-json wraps them.
 * No content, tool output or research text is retained in telemetry.
 */
export function nativeObservation(value: unknown): NativeObservation | null {
  if (!isRecord(value)) return null;
  const row = isRecord(value.step_update) ? value.step_update : value;
  const step = row.step_index;
  if (typeof step !== "number" || !Number.isSafeInteger(step) || step < 0) return null;
  if (row.status !== "DONE" && row.state !== "DONE") return null;
  const type = String(row.type ?? row.step_type ?? "").toUpperCase();
  const date = Date.parse(String(value.created_at ?? value.timestamp ?? row.created_at ?? ""));
  const at = Number.isFinite(date) ? date : 0;
  if (type === "CHECKPOINT") return { step, at, kind: "checkpoint" };
  if (type === "USER_INPUT") {
    const content = typeof row.content === "string" ? row.content : "";
    return { step, at, kind: "user", manual: /^\s*(?:<USER_REQUEST>\s*)?\/compact(?:\s|$)/i.test(content) };
  }
  if (type !== "PLANNER_RESPONSE" && type !== "AGENT_RESPONSE") return null;
  const usage = isRecord(row.usage) ? row.usage : row;
  const fields = [usage.input_tokens, usage.cache_read_tokens, usage.output_tokens];
  if (!fields.some((n) => typeof n === "number") ||
      fields.some((n) => n !== undefined && (typeof n !== "number" || !Number.isFinite(n) || n < 0))) return null;
  const tokens = fields.reduce<number>((sum, n) => sum + (typeof n === "number" ? n : 0), 0);
  return { step, at, kind: "usage", tokens };
}

/** Applies one transcript line to a step-keyed observation map. Later rows for
 * the same step override earlier ones, exactly as a full in-order reread.
 */
export function applyNativeLogLine(steps: Map<number, NativeObservation>, line: string): boolean {
  try {
    const observation = nativeObservation(JSON.parse(line));
    // A partial/invalid native timestamp cannot establish historical order or
    // associate a request. Retry it when the transcript row is complete.
    if (!observation || observation.at <= 0) return false;
    steps.set(observation.step, observation);
    return true;
  } catch { return false; /* A torn final line is retried on the next boundary read. */ }
}

export function byStep(a: NativeObservation, b: NativeObservation): number {
  return a.step - b.step;
}

export function parseNativeCompactionLog(text: string): NativeObservation[] {
  const steps = new Map<number, NativeObservation>();
  for (const line of text.split(/\r?\n/)) applyNativeLogLine(steps, line);
  return [...steps.values()].sort(byStep);
}

/**
 * agy reports no size for a compacted context: `/compact` emits a bare
 * CHECKPOINT step, and `result.usage` is the conversation's cumulative total,
 * unchanged by the compact. The compacted context is the system prompt, tools
 * and original request (the conversation's first model request) plus a
 * summary. Across 263 local native checkpoints the next request was a median
 * 4,160 tokens above the first (p10 2.4K, p90 9.3K); first + 4,000 is off by a
 * median 1.3K. The next real request replaces the estimate.
 */
export const POST_COMPACT_SUMMARY_TOKENS = 4_000;

/** Estimated context right after a compact; null without a model request. */
export function estimatePostCompactTokens(rows: readonly NativeObservation[]): number | null {
  const first = rows.find((r) => r.kind === "usage" && (r.tokens ?? 0) > 0);
  return first?.tokens ? first.tokens + POST_COMPACT_SUMMARY_TOKENS : null;
}

function refillDistance(from: CompactEvent, to: CompactEvent): number | null {
  return from.requestIndex === undefined || to.requestIndex === undefined ? null
    : Math.max(0, to.requestIndex - from.requestIndex);
}

/** Model requests since the last real compact; null when either is unknown. */
export function requestsSinceLastCompact(state: CompactionState): number | null {
  const last = state.events.find((e) => e.id === state.lastCompactId);
  return last?.requestIndex === undefined || state.modelRequests < 0 ? null
    : Math.max(0, state.modelRequests - last.requestIndex);
}

/** True only for a measured refill shorter than the window. */
export function withinRapidRefillWindow(state: CompactionState): boolean {
  const since = requestsSinceLastCompact(state);
  return since !== null && since < RAPID_REFILL_REQUEST_WINDOW;
}

export function beginBusinessTurn(state: CompactionState): void {
  state.logicalTurn += 1;
  state.compactedSinceLastTurn = false;
  const last = state.events.find((e) => e.id === state.lastCompactId);
  if (last) state.turnsSinceLastCompact = Math.max(0, state.logicalTurn - last.logicalTurn);
  if (!withinRapidRefillWindow(state)) {
    state.consecutiveRapidRefills = 0;
    state.rapidRefillBreakerTripped = false;
  }
}

export function beginCompact(state: CompactionState, id: string, source: "manual" | "idle", at: number): CompactEvent {
  const event: CompactEvent = { id, source, at, status: "pending", logicalTurn: state.logicalTurn,
    baselineStep: state.nativeStep, preTokens: state.lastContextTokens || null, postTokens: null,
    ...(state.modelRequests >= 0 ? { requestIndex: state.modelRequests } : {}) };
  state.events.push(event);
  return event;
}

/**
 * The rapid-refill breaker guards the idle compactor: it trips when idle
 * compacts keep refilling within RAPID_REFILL_REQUEST_WINDOW model requests of
 * the previous compact. agy's own native checkpoints during long work are
 * normal, so they neither count as a refill nor clear one (they still mark the
 * previous compact for distance). An explicit manual compact always resets.
 * `distance` is null for the first compact or when request counts are unknown;
 * an unmeasured refill is not counted.
 */
function rapidRefillsAfter(current: number, source: CompactSource, distance: number | null): number {
  if (source === "manual" || distance === null || distance >= RAPID_REFILL_REQUEST_WINDOW) return 0;
  return source === "native" ? current : current + 1;
}

function applyCompleted(state: CompactionState, event: CompactEvent): void {
  const last = state.events.find((e) => e.id === state.lastCompactId);
  // A delayed old log row is telemetry, not a fresh compact or breaker reset.
  if (last && last.id !== event.id &&
      ((last.nativeStep !== undefined && event.nativeStep !== undefined)
        ? event.nativeStep <= last.nativeStep : event.at < last.at)) return;
  if (state.lastCompactId === event.id) return;
  state.consecutiveRapidRefills = rapidRefillsAfter(state.consecutiveRapidRefills, event.source,
    last ? refillDistance(last, event) : null);
  state.rapidRefillBreakerTripped = state.consecutiveRapidRefills >= RAPID_REFILL_BREAKER_LIMIT;
  if (event.at >= state.lastFailureAt) state.consecutiveCompactFailures = 0;
  state.hasCompactedInSession = true;
  state.lastCompactId = event.id;
  state.turnsSinceLastCompact = Math.max(0, state.logicalTurn - event.logicalTurn);
  state.compactedSinceLastTurn = state.turnsSinceLastCompact === 0;
  if (event.at >= state.contextAt) {
    state.lastContextTokens = event.postTokens ?? 0;
    state.contextAt = event.at;
  }
}

export function finishCompact(state: CompactionState, id: string, success: boolean, at: number, postTokens?: number): CompactEvent | undefined {
  const event = state.events.find((e) => e.id === id);
  if (!event) return undefined;
  event.finishedAt = at;
  // A native checkpoint is real even if a subsequent backend response fails.
  if (event.nativeStep !== undefined || success) {
    event.status = "completed";
    if (event.postTokens === null && postTokens !== undefined && postTokens > 0) event.postTokens = postTokens;
    applyCompleted(state, event);
    if (state.lastCompactId === event.id && state.contextStep <= (event.nativeStep ?? state.contextStep)) {
      state.lastContextTokens = event.postTokens ?? 0;
    }
  } else if (event.status === "pending") {
    event.status = "failed";
    state.lastFailureAt = at;
    state.consecutiveCompactFailures += 1;
    state.compactedSinceLastTurn = true; // suppress retry until a real business turn
  }
  return event;
}

export function recoverInterruptedCompacts(state: CompactionState, now: number): void {
  for (const event of state.events) {
    if (event.status !== "pending") continue;
    finishCompact(state, event.id, false, now);
    event.status = "interrupted";
  }
}

function isSortedByStep(rows: readonly NativeObservation[]): boolean {
  for (let i = 1; i < rows.length; i++) if (rows[i - 1].step > rows[i].step) return false;
  return true;
}

function nextUserIndex(rows: readonly NativeObservation[], from: number): number {
  for (let i = from; i < rows.length; i++) if (rows[i].kind === "user") return i;
  return -1;
}

/** Legacy `at` is completion, often 30-60s AFTER the checkpoint's start.
 * Require an observed /compact input, exactly one checkpoint within that
 * request, and completion before the next input. No nearest-time heuristic,
 * and no relaxed interval for modern recorded attempts.
 */
function pairLegacyCompact(state: CompactionState, rows: readonly NativeObservation[], index: number,
  request: NativeObservation): CompactEvent | undefined {
  const row = rows[index];
  const next = nextUserIndex(rows, index + 1);
  if (next < 0 || rows[next].at <= row.at) return undefined;
  let checkpoints = 0;
  for (const r of rows) {
    if (r.kind === "checkpoint" && r.step > request.step && r.step < rows[next].step && ++checkpoints > 1) return undefined;
  }
  let match: CompactEvent | undefined;
  for (const e of state.events) {
    if (e.nativeStep !== undefined || e.legacyCompletedAt === undefined ||
        e.legacyCompletedAt < row.at || e.legacyCompletedAt >= rows[next].at) continue;
    if (match) return undefined;
    match = e;
  }
  return match;
}

/** Reconcile by native step identity, never ingestion time or replay position.
 * Full history is safe to reread and can heal malformed/partial earlier rows;
 * reapplying the same rows is idempotent. `rows` is never mutated.
 */
export function reconcileNativeCompaction(state: CompactionState, rows: readonly NativeObservation[], history = true): CompactEvent[] {
  const sorted = isSortedByStep(rows) ? rows : [...rows].sort(byStep);
  const byNativeStep = new Map<number, CompactEvent>();
  for (const e of state.events) {
    if (e.nativeStep !== undefined && !byNativeStep.has(e.nativeStep)) byNativeStep.set(e.nativeStep, e);
  }
  const added: CompactEvent[] = [];
  let logicalTurn = history ? 0 : state.logicalTurn;
  let manual = false;
  let request: NativeObservation | undefined;
  let migratedLegacy = false;
  let previousTokens: number | null = null;
  // History without a single model request cannot measure refill distance.
  const countsRequests = history && sorted.some((r) => r.kind === "usage");
  let requests = 0;
  for (let i = 0; i < sorted.length; i++) {
    const row = sorted[i];
    if (row.kind === "user") { request = row; manual = row.manual === true; if (!manual) logicalTurn++; }
    if (row.kind === "usage") { previousTokens = row.tokens ?? null; requests++; }
    if (row.kind !== "checkpoint") continue;
    let event = byNativeStep.get(row.step);
    if (!event) {
      // Pair only a real request's first new checkpoint within its execution
      // interval. Do not merge arbitrary nearby independent native compacts.
      event = state.events.find((e) => e.nativeStep === undefined && e.legacyCompletedAt === undefined && e.source !== "native" &&
        row.step > e.baselineStep && row.at > 0 && row.at >= e.at - 1_000 &&
        (e.finishedAt === undefined || row.at <= e.finishedAt + 1_000));
      if (!event && history && request?.manual && row.at >= request.at) {
        event = pairLegacyCompact(state, sorted, i, request);
        if (event) {
          event.at = row.at;
          event.logicalTurn = logicalTurn;
          event.baselineStep = request.step;
          migratedLegacy = true;
        }
      }
      if (event) {
        event.nativeStep = row.step;
      } else {
        event = { id: `native-checkpoint:${row.step}`, nativeStep: row.step, baselineStep: row.step - 1,
          at: row.at, status: "completed", source: manual ? "manual" : "native", logicalTurn,
          preTokens: previousTokens ?? (history ? null : state.lastContextTokens || null), postTokens: null };
        state.events.push(event);
      }
      byNativeStep.set(row.step, event);
      event.status = "completed";
      added.push(event);
    }
    // Exact from full history; a live checkpoint uses the last known count
    // until the turn's result rereads the transcript.
    if (countsRequests) event.requestIndex = requests;
    else if (event.requestIndex === undefined && state.modelRequests >= 0) event.requestIndex = state.modelRequests;
    if (event.preTokens === null) event.preTokens = previousTokens;
    if (history && event.id.startsWith("native-checkpoint:")) {
      event.logicalTurn = Math.max(event.logicalTurn, logicalTurn);
      if (manual) event.source = "manual";
    }
    for (let j = i + 1; j < sorted.length && sorted[j].kind !== "checkpoint"; j++) {
      if (sorted[j].kind === "usage") { event.postTokens = sorted[j].tokens ?? null; break; }
    }
    state.logicalTurn = Math.max(state.logicalTurn, logicalTurn);
    applyCompleted(state, event);
    manual = false;
  }
  if (countsRequests) state.modelRequests = Math.max(state.modelRequests, requests);
  state.logicalTurn = Math.max(state.logicalTurn, logicalTurn);
  const last = state.events.find((e) => e.id === state.lastCompactId);
  if (last) state.turnsSinceLastCompact = Math.max(0, state.logicalTurn - last.logicalTurn);
  if (last && logicalTurn > last.logicalTurn && sorted.some((r) => r.kind === "user" && !r.manual &&
      r.at > Math.max(last.at, state.lastFailureAt))) state.compactedSinceLastTurn = false;
  // A previously malformed row can arrive behind the high-water mark. Include
  // it in the historical streak without treating ingestion as a new compact.
  const completed = state.events.filter((e) => e.status === "completed").sort((a, b) =>
    a.nativeStep !== undefined && b.nativeStep !== undefined ? a.nativeStep - b.nativeStep : a.at - b.at);
  let streak = 0;
  for (let i = 0; i < completed.length; i++) {
    streak = rapidRefillsAfter(streak, completed[i].source, i === 0 ? null : refillDistance(completed[i - 1], completed[i]));
  }
  const lastIsNewest = last !== undefined && last.id === completed.at(-1)?.id;
  if (lastIsNewest) {
    // Events explain at most `streak` refills, plus one blocked idle attempt
    // (the breaker trips before that attempt creates an event). Anything above
    // that was counted by older builds (from native checkpoints, or with Paseo
    // prompts as the distance) and is cleared, as is any count once the last
    // compact is a full window behind or the distance cannot be measured.
    state.consecutiveRapidRefills = !withinRapidRefillWindow(state) ? 0
      : migratedLegacy ? streak : Math.max(streak, Math.min(state.consecutiveRapidRefills, streak + 1));
    state.rapidRefillBreakerTripped = state.consecutiveRapidRefills >= RAPID_REFILL_BREAKER_LIMIT;
  }
  let latest: NativeObservation | undefined;
  for (let i = sorted.length - 1; i >= 0 && !latest; i--) {
    if (sorted[i].kind !== "user") latest = sorted[i];
  }
  if (latest && latest.step >= state.contextStep &&
      (latest.at >= state.contextAt || (state.contextStep >= 0 && latest.step > state.contextStep))) {
    state.contextStep = latest.step;
    state.contextAt = latest.at;
    state.lastContextTokens = latest.kind === "usage" ? latest.tokens ?? 0
      : byNativeStep.get(latest.step)?.postTokens ?? 0;
  }
  if (sorted.length) state.nativeStep = Math.max(state.nativeStep, sorted[sorted.length - 1].step);
  return added;
}
