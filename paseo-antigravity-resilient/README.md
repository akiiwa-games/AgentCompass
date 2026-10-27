# Paseo Antigravity Resilient Provider (`antigravity-resilient-provider`)

Maintainable Paseo provider plugin (`providerId: "antigravity-resilient"`, `pluginId: "antigravity-resilient-provider"`, `requirements.paseo: ">=0.11.2"`) that:

1. **Preserves and replays Antigravity (`agy`) timeline history across Paseo daemon restarts** (`history: "replay"` before `session.ready`), while keeping native `--conversation <conversationId>` continuation intact.
2. **Replaces brittle installed-source patching** with a first-class Paseo plugin installed via `installPluginSource` / `reloadPlugin` (survives Paseo upgrades without editing `%LOCALAPPDATA%/Programs/Paseo/resources/builtin-plugins/antigravity-provider/`).
3. **Fixes recurring Antigravity failures backed by local Paseo daemon and `agy` CLI log evidence** with bounded, safe recovery.
4. **Exposes only the verified `full-access` permission mode** (`--dangerously-skip-permissions`), matching public Paseo documentation and the built-in Antigravity provider because `agy` cannot interactively prompt for permissions when driven externally.

---

## 1. Why a Separate Provider ID (`antigravity-resilient`)

In Paseo `0.11.2`, bundled providers and plugin providers share the `ProviderRegistry` namespace, and `PluginManager` rejects any plugin whose provider ID collides with a built-in provider ID (`antigravity`). Registering `antigravity-resilient`:

- Avoids mutating files under `%LOCALAPPDATA%/Programs/Paseo/resources/builtin-plugins/antigravity-provider/`.
- Survives Paseo binary updates and can be hot-reloaded (`reloadPlugin`) without restarting the Paseo daemon or killing active `antigravity` sessions.
- Coexists safely alongside the built-in `antigravity` provider during migration.

---

## 2. Durable Timeline Persistence, Bounded Journal Compaction & Replay Architecture

The bundled Paseo `antigravity` provider stores `{ conversationId }` in `session.persistence`, which lets `agy --conversation <id>` resume model context on the next prompt, **but emits zero `timeline.item` events when `session.open` is called with `history: "replay"`**. After a daemon restart or plugin reload, Paseo's chat timeline appears empty until new turns occur.

`antigravity-resilient` adds `ConversationStore` (`server/internal/store.ts`):

- **Per-conversation storage directory**: `~/.paseo/antigravity-resilient/conversations/` (overridable via `config.providerOptions.storeDir` or `SessionOptions.storeDir` in tests).
- **Bounded append-only journal + turn-boundary atomic snapshot compaction**:
  - **Live streaming (`upsert`)**: Appends only the single updated `StoredTimelineRecord` line to `<conversationKey>.jsonl` (`O(1)` per delta) without rewriting `<conversationKey>.json` on every streaming text or tool update. If a single long turn exceeds `maxJournalEntriesBeforeCompact` (default `128`), `ConversationStore` automatically checkpoints `.json` and compacts `.jsonl`.
  - **Turn & session boundaries (`flush`)**: When a turn completes, fails, is interrupted, retries after recovery, or when the session closes/disposes, `Session.flushStore()` calls `store.flush(conversationId)`.
  - **Windows-safe atomic replacement (`atomicReplaceFile` / `writeSnapshotAtomically`)**:
    - Writes the new snapshot to `<key>.<pid>.<counter>.tmp`.
    - Stages a verified copy of the existing valid `<key>.json` to `<key>.<pid>.<counter>.bak.tmp` and atomically replaces `<key>.json.bak`.
    - Replaces `<key>.json` with `<key>.<pid>.<counter>.tmp` via direct `renameSync`, or—if Windows blocks overwrite-rename on an existing destination—moves `<key>.json` to a temporary displaced path `<key>.<pid>.<counter>.snap.prev.tmp`, renames `<key>.<pid>.<counter>.tmp` -> `<key>.json`, and rolls back `<key>.<pid>.<counter>.snap.prev.tmp` -> `<key>.json` if the replacement fails. It **never** overwrites `<key>.json` or `<key>.json.bak` in place.
  - **Crash-safe journal compaction (`compactJournalAtomically`)**:
    - Executed **only** after `<key>.json` has been atomically replaced with all current records. Atomically replaces `<key>.jsonl` with an empty file so the journal remains bounded (0 bytes after turn completion) while guaranteeing zero data loss if a crash occurs before, during, or after compaction.
  - **Recovery order on `load()`**: Primary `.json` snapshot merged with any uncompacted `.jsonl` tail -> `.json.bak` backup merged with `.jsonl` -> `.jsonl` alone -> fallback parser for native `~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/transcript.jsonl`.
- **Pre-`init` buffering**: On Turn 1 of a new conversation, `user_message` is emitted before `agy` outputs `{ event: "init", conversation_id }`. `Session` buffers pre-`init` timeline items in memory and flushes them to `ConversationStore` immediately upon receiving `conversation_id`.
- **Idempotent updates & monotonic turn IDs**:
  - Within a turn, streaming deltas (`assistant_message`, `reasoning`, `tool_call` status transitions from `running` to `completed`/`failed`) share stable item IDs (`turn-<N>:message:<M>`, `turn-<N>:reasoning:<M>`, `tool:<callId>`) and update in place without changing insertion order.
  - When reopening an existing conversation, `Session` initializes `turnIndex = Math.max(turnIndex, store.maxTurnIndex(conversationId))` so post-restart turns continue at `turn-(N+1)` and never collide with or overwrite earlier turns.
- **Replay before `session.ready`**:
  - On `session.open` with `history: "replay"` and `persistence.data.conversationId`, `Session.open()` loads all stored items and emits `{ type: "timeline.item", sessionId, item, timestamp }` for each item **before** `session.ready`.
- **Credential & environment sanitization**:
  - `sanitizeTimelineItem()` strips any keys matching `env|credentials|accessToken|refreshToken|apiKey|password|secret` and redacts `ya29.*` OAuth access tokens, `1//*` refresh tokens, `AIza*` API keys, and `Bearer ...` headers before persisting to disk.

---

## 3. Evidence-Backed Failure Mitigations

Forensic inspection of `~/.paseo/*daemon.log*` and 3,052 `agy` logs in `~/.gemini/antigravity-cli/log/*.log` identified four recurring failure modes:

| # | Failure Pattern | Local Evidence (`~/.paseo` & `~/.gemini/antigravity-cli/log`) | Exact Trigger | Bounded Mitigation |
|---|---|---|---|---|
| **1** | **Hourly OAuth 401 token refresh race & token endpoint timeout** | Daemon and `agy` CLI logs (`pre-invocation hook: context summarization: UNAUTHENTICATED (code 401)`), and 52 occurrences of `GetToken failed: oauth2: cannot fetch token: Post "https://oauth2.googleapis.com/token": net/http: TLS handshake timeout`. | `isTokenGap(errorText)` matches `UNAUTHENTICATED`, `code 401`, `invalid authentication credentials`, `expected OAuth 2 access token`, `oauth2: cannot fetch token`, or `oauth2.googleapis.com/token` when `conversationId` is known and `!turn.retriedTokenGap`. | Set `turn.retriedTokenGap = true` (at most **1 retry per turn**), flush partial progress, stop current `agy` child process, wait **`8,000`ms** (`TOKEN_GAP_RETRY_DELAY_MS`), respawn `agy --conversation <id>`, and resend `turn.prompt` if `turn.text.size === 0 && turn.tools.size === 0`, or `TOKEN_GAP_CONTINUE_PROMPT` if partial output/tool calls already occurred. Genuine expired auth fails on the retry without looping. |
| **2** | **Over-broad `auth` substring rewrite in `diagnostic()`** | Bundled `wire.ts` checked `/auth\|login\|logged in\|credentials/i.test(message)`, rewriting any error containing `"author"` or `"authority"` into `"Antigravity authentication failed. Run agy and sign in, then retry."` | `diagnostic(message)` in `server/internal/wire.ts`. | Narrowed via `isAuthDiagnosticError(message)` to match only `isTokenGap(message)` or explicit login phrases (`you are not logged in`, `not authenticated`, `\bsign[\s_-]?in\b`, `\blog[\s_-]?in\b`). |
| **3** | **Sticky trajectory `ERROR_MESSAGE` & mid-turn stream interruption** | 8 consecutive false `turn_failed` events on conversation `<conversation-uuid>`. Root cause verified in `brain/<conversation-uuid>/.system_generated/logs/transcript.jsonl` step `516`: `agy` recorded `type: "ERROR_MESSAGE", error: "The stream was interrupted. Please continue the task you were working on."` during an internal 500 retry, recovered on attempt 2, and completed 8 subsequent turns (`steps 533..934`), but `agy --output-format stream-json` continued emitting `result.status = "error"` with that historical `ERROR_MESSAGE` in `result.errors` alongside each turn's valid `result.response`. | `frame.event === "result"` with `frame.status !== "success"` where `resultErrors(frame)` matches `isStreamInterruption(msg)` (`The stream was interrupted. Please continue the task you were working on.`). | **Case A (completed turn with sticky trajectory error)**: If `frame.response` is non-empty and every error in `resultErrors(frame)` is a stream-interruption or already-seen trajectory error, record the error signatures in `seenResultErrors` and call `completeTurn()` instead of failing a completed turn. **Case B (genuine mid-turn interruption before final response)**: If `frame.response` is empty and `!turn.retriedStreamInterrupt`, retry at most **once per turn** after **`2,000`ms** (`STREAM_INTERRUPT_RETRY_DELAY_MS`) on `--conversation <id>`. |
| **4** | **Stale model catalog cache (`INVALID_MODEL`) & transient `agy models` timeout/503** | `20261010-1632-01-daemon.log:534` (`code: "INVALID_MODEL", message: "Unknown Antigravity model: gemini-4-argon-high"`), `20261013-0211-01-daemon.log:4` (`code: "PROBE_TIMEOUT", message: "Timed out discovering Antigravity models"`), and 9 `agy models` logs with `503 UNAVAILABLE` (`cli-20261009_182157.log`, `cli-20261014_150449.log`, etc.). | `getCatalogCacheKey()`, `getCatalog()`, and `validateSelection()` in `server/internal/catalog.ts` and `server/provider.ts`. | 1) `getCatalogCacheKey()` hashes resolved `agy` binary `mtimeMs` + `size` so CLI upgrades automatically invalidate Paseo's disk catalog cache. 2) `getCatalog()` uses a `20,000`ms probe timeout, retries once after `1,500`ms on timeout or transient 503/network failure, falls back to the last valid catalog on background refresh timeout, and ignores non-tab banner lines (`Fetching available models...`). 3) `validateSelection()` re-probes once with `bypassCache: true` before throwing `INVALID_MODEL`. |

### Historical compaction display (2026-10-16)

Paseo 0.11.2 classifies replayed `notification` items as opening notices and appends them to an already loaded durable timeline. This made six historical idle compactions in a long-running session appear as 28 notifications, with morning timestamps at the latest sequence positions. New compactions use the native `compaction` timeline type. Old `turn-N:auto-compact` notifications are converted on replay without modifying the snapshot, event time or order. Existing duplicated daemon timelines can be rebuilt with the user-facing Reload agent action while the agent is idle, after backing up canonical history; reloading the plugin alone preserves the existing daemon timeline.

### Stream idle timeout recovery (2026-10-16)

The stream-interruption classifier also recognizes `request failed: stream idle timeout (5m0s): context deadline exceeded`. Local evidence: one failure followed by a retry after 4 seconds on conversation `<conversation-uuid>` recorded that error at step 674, then successful model/tool steps 675–677. Later Paseo turns repeatedly surfaced this historical error as `turn_failed`.

Reuse the existing recovery policy: a non-empty final response with only recognized recovered errors completes normally; an interruption without a final response gets at most one continuation retry. Bare `context deadline exceeded` and unrelated tool errors are not classified as stream interruptions. Unrelated errors accompanying a stream interruption are not added to the recovered-error set. Tests cover legacy/nested result frames, partial progress, the retry limit, and mixed errors.

### Pre-Cache-Expiry Automatic Context Compaction (`>= 150,000` Tokens, Claude Code Idle Compactor & Circuit Breakers)

- **Empirical & Binary Evidence for Antigravity's 5-Minute Hot Cache TTL**:
  - `agy.exe` (`google3/third_party/jetski/prompt/cumulative_prompt_handler.setLastCacheControlToEphemeral`) uses `CACHE_CONTROL_TYPE_EPHEMERAL`, which sets a **5-minute (`300,000` ms)** ephemeral prompt cache window on the underlying model backend.
  - Across **16,760+ `PLANNER_RESPONSE` steps in 531 local conversations** (`~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl`, filtered for `input_tokens + cache_read_tokens > 30,000`), prompt cache hit rate is **99.1%** at `0–1m`, **95.8%** at `1–2m`, **93.4%** at `2–3m`, **92.8%** at `3–4m`, and **87.1%** at `4–5m` (e.g. `294.0s` / `4.90m` hitting `206,159` cached tokens, `297.0s` / `4.95m` hitting `211,721` cached tokens), then drops sharply at **5.00 minutes (`300s`)** (`302.0s`, `303.0s`, `304.0s` all drop to `cache_read_tokens = 0` with `168k–225k` uncached `input_tokens`).
- **Claude Code Idle Compaction Architecture (`server/internal/session.ts`)**:
  - **Hot Cache TTL & Lead Time**: `DEFAULT_HOT_CACHE_TTL_MS = 300_000` (5 minutes), `DEFAULT_AUTO_COMPACT_LEAD_MS = 60_000` (1 minute before expiry), `DEFAULT_AUTO_COMPACT_MAX_LATE_MS = 60_000` (`Rjo = 60_000` in Claude Code).
  - **Context Threshold**: `DEFAULT_AUTO_COMPACT_TOKEN_THRESHOLD = 150_000` (`150K` tokens: Claude Code's `bjo = 200_000` scaled down for agy's 256K window, which checkpoints natively at ~230-250K, so the idle compactor gets to run first; with `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` env override clamped to `>= 100,000` / `wjo = 100_000` and `DISABLE_COMPACT` support).
  - **Prompt Cache Tracker & Lapsed Gate (`aX` / `xjo`)**: Tracks `lastRequestAt`, `lastCacheActivityAt`, `cacheExpiresAt`, and `lastHandledRequestAt`. When the idle timer wakes up (`fireAt = cacheExpiresAt - autoCompactLeadMs`), if the prompt cache has already expired (`now >= cacheExpiresAt`) or the timer woke up late (`lateMs > autoCompactMaxLateMs`, e.g. after machine sleep), it skips compaction as `"lapsed"` instead of running a cold-cache full-context summarization.
  - **Post-Compaction Token Isolation (`I1t`)**: When `/compact` completes, `recordPostCompaction()` clears `lastRequestAt` / `cacheExpiresAt` and updates `lastContextTokens` to the estimated compacted size (see §6; never the `/compact` result totals), preventing small follow-up turns (such as `STATUS_POLL`) from re-triggering compaction.
  - **Rapid-Refill Thrashing & Failure Circuit Breakers (`vmr` / `Ebn`)**:
    - **Rapid-Refill Breaker (`RAPID_REFILL_REQUEST_WINDOW = 10`, `RAPID_REFILL_BREAKER_LIMIT = 3`)**: If idle auto-compaction keeps refilling the context to `>= 150,000` tokens within `< 10` agy model requests of the previous compaction 3 times in a row, the breaker trips, emits Claude Code's warning notification (`AUTOCOMPACT_THRASHING_MESSAGE`), and skips further auto-compaction until `>= 10` model requests have run since the last compaction or manual `/compact` runs. agy's own native checkpoints during long work are neutral: they mark the previous compaction for distance but never count as a refill (counting them made normal long tasks trip the breaker and block the next idle compact).
    - **Distance unit (2026-10-21)**: model requests are the native `PLANNER_RESPONSE`/`AGENT_RESPONSE` steps in agy's transcript (`modelRequests` in state, `requestIndex` per compaction). Paseo prompts were the unit before, and one prompt runs ~100 agy steps: three conversations tripped on 2026-10-20/21 although every refill behind each trip took 32-68 model requests (e.g. steps 852→943 and 20K→162K tokens within one prompt). Across 204 local compaction pairs the median gap is 58 requests and 90% are `>= 22`; the fast refills (5-10 requests) came from very large tool outputs. Without a transcript showing model requests the distance is unknown and is not counted as a refill, so a moved transcript cannot lock the compactor; older state files load with the count unknown and are reconciled from the transcript.
    - **Consecutive Failure Breaker (`MAX_CONSECUTIVE_COMPACT_FAILURES = 3`)**: Trips after 3 consecutive compaction failures, survives reopen, and skips plugin idle attempts until a successful compact. Explicit manual `/compact` remains available.
  - **Silent Background Execution**: Idle auto-compaction runs without emitting foreground `session.turn` (`started`/`completed`) events so Paseo never mistakes background idle compaction for an active foreground turn or rejects incoming prompts with `Provider does not support prompt.steer`; incoming prompts await `autoCompactPromise` and execute cleanly.

### Conversation-level compaction state

`server/internal/compaction.ts` reconciles native `CHECKPOINT`, explicit `/compact`, and plugin idle attempts. Native transcript rows are flat (`type`, `status`, `step_index`, `created_at`, `input_tokens`, `cache_read_tokens`, `output_tokens`); stream updates wrap fields in `step_update`. Completed native checkpoints use conversation-scoped step identities, not arrival time or timeline position. Repeated and reordered history is idempotent. Malformed lines and incomplete timestamps are retried on later boundary reads. No transcript body is stored in compaction telemetry.

`<conversationId>.compaction.json` stores attempt source/status, native checkpoint identity, logical turn, before/after token observations, last real compact, current context, and failure/refill counters. Writes stage a temporary file and atomically replace the primary, with a verified `.bak` and Windows rollback. The plugin writes an in-flight marker before an idle `/compact`; if this write fails, only that automatic attempt is skipped (`state_unavailable`). A subsequent business turn can retry after storage recovers. Reopening reconciles native history first, then marks an unresolved in-flight attempt interrupted once. Cache warmth is not invented on reopen: the next live request supplies it.

Native and plugin/manual events are paired by the native step boundary and the recorded attempt interval, so a streamed checkpoint and a later transcript/result do not add two compactions. Historical reconstruction restores control state without emitting new turns, live usage samples or new compaction timeline rows. Successful explicit `/compact` resets the breaker; ordinary work remains available. Native checkpoints contribute to the refill breaker, which prevents additional **plugin idle** compactions; it cannot disable the CLI's independent native compactor. Native post-context comes from the first later usage observation; unknown values remain `null` in telemetry (the existing session getter uses `0`). For explicit compacts without such a sample, result summary output remains an estimate.

Legacy timeline-only snapshots migrate without rewriting user messages. Their event time marks completion, not request start: association requires an explicit native `/compact` input, exactly one checkpoint within that request, and exactly one legacy completion before the next native user input. Ambiguous intervals remain unmatched rather than using nearest timestamps. Modern attempts retain their strict recorded start/end bounds. Old failure counts were never recorded and cannot be recovered. Stores assume one active provider session per conversation; atomic files do not provide a multi-writer dispatch lock. Tests cover reload after failure/rapid refill, interrupted attempts, mixed native/manual/idle events, flat native fields, delayed repaired rows, real legacy timing offsets, time ordering and rename failure.

The provider adds brief CSV/JSON filtering and summarization guidance once per Session runtime, on the **next real non-command prompt**, using the existing `renderPrompt` separation between CLI text and visible/stored user text. New conversations receive configured system instructions plus guidance once; restored conversations receive guidance only, without replaying `config.systemPrompt`. `/compact` and other commands remain exact and do not consume the pending guidance. Complete research artifacts and stored user messages are preserved; there is no automatic truncation, unsolicited turn or project edit. Existing conversations receive guidance only after the updated provider is safely reopened and the user sends the next real prompt.

---

## 4. Development & Verification Commands

Run typecheck and unit tests locally:

```bash
cd paseo-antigravity-resilient
npm run check
```

- `npm run typecheck`: Runs `scripts/typecheck.mjs` (TypeScript compiler check with `noEmit: true` and `strict: true`).
- `npm test`: Runs `test/provider.test.ts` (covering 401 refresh-race retry with/without progress, single-retry bound on persistent 401, narrowed `diagnostic()`, sticky stream-interruption completion, mid-turn stream-interruption retry, durable timeline persistence + replay + corruption recovery, streaming journal efficiency + pre-compaction recovery + post-turn compaction, Windows-safe atomic replacement fault-injection recovery, `full-access`-only mode enforcement, fail-closed remote git sync safety decisions, native `transcript.jsonl` fallback replay, catalog discovery resilience, model-family context-window mapping + catalog enrichment, `result.usage`-driven context max/used, post-compaction meter samples for idle, manual and native compactions, and `providerOptions.contextWindowMaxTokens` override) and `test/usage.test.ts` (covering credential blob/JWT parsing, quota-group to window/detail mapping, `fetchUsage` request shape and `available`/`no_quota`/`expired`/`rejected`/`error` outcomes, `discover` scoping and key format, the input schema defaults, and usage-source registration).

---

## 5. Installation, Non-Destructive Sync & Live Verification (`scripts/deploy-and-verify.mjs`)

`scripts/deploy-and-verify.mjs` and `scripts/sync-safety.mjs` implement a **fail-closed, non-destructive** deployment and verification workflow:

- **Remote git sync safety (`scripts/sync-safety.mjs`)**:
  - Fetches the git bundle into a temporary ref `refs/remotes/origin/bundle-candidate` without modifying `HEAD`, `main`, the index, or the working tree.
  - Inspects `git symbolic-ref --short HEAD`, `git rev-parse HEAD`, `git status --porcelain=v1 -uall`, `git merge-base --is-ancestor HEAD <localHead>`, and `git diff --name-only HEAD..<localHead>`.
  - `evaluateRemoteSyncSafety()` **refuses** to sync if:
    1. Remote checkout is not on `main`.
    2. Remote `HEAD` has diverged from or is ahead of `localHead` (`isAncestor === false`).
    3. Remote checkout has any uncommitted tracked changes (`trackedDirty.length > 0`).
    4. Remote checkout has any untracked files that would be overwritten by incoming changes (`collidingUntracked.length > 0`).
  - Only when all checks pass does it advance the remote checkout via `git merge --ff-only refs/remotes/origin/bundle-candidate`. It **never** uses `git checkout -f`, `git reset --hard`, or `git clean`.

```bash
# Dry-run remote sync safety check only (no git merge, no plugin changes)
node paseo-antigravity-resilient/scripts/deploy-and-verify.mjs --target remote --dry-run-sync

# Local reload + lightweight replay verification on an existing persisted conversation (zero LLM quota)
node paseo-antigravity-resilient/scripts/deploy-and-verify.mjs --target local --replay-conversation <conversation-uuid>

# Remote fast-forward sync + reload + lightweight replay verification (zero LLM quota)
node paseo-antigravity-resilient/scripts/deploy-and-verify.mjs --target remote --replay-conversation <conversation-uuid>
```

### Operational Diagnostics

- Check plugin status via Paseo CLI (outside `agy` sandbox) or SDK:
  - `client.listPlugins()` -> confirms `antigravity-resilient-provider` is `status: "running"`.
  - `client.getPluginLogs("antigravity-resilient-provider")` -> inspects runtime retry and replay logs (`[antigravity-resilient] ...`).
- Inspect persisted conversation snapshots on the daemon host:
  - `~/.paseo/antigravity-resilient/conversations/<conversationId>.json` (atomically updated snapshot)
  - `~/.paseo/antigravity-resilient/conversations/<conversationId>.jsonl` (live write-ahead journal, compacted to `0` bytes at turn completion/close)
- **Windows Mandatory Integrity Control (`Paseo.exe` SACL check)**:
  - External Paseo plugins run in a forked `Paseo.exe` worker (`plugin-process.js`). If a previous sandboxed agent modified `%LOCALAPPDATA%\Programs\Paseo` in place, Windows sandbox ACL injection may leave `Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)` on `%LOCALAPPDATA%\Programs\Paseo`, causing Windows MIC to downgrade forked `plugin-process.js` workers to Low Integrity (`S-1-16-4096`) and trigger `EPERM` when writing to `~/.paseo`.
  - Verify and repair without restarting the daemon:
    ```powershell
    icacls "$env:LOCALAPPDATA\Programs\Paseo" /setintegritylevel "(OI)(CI)M"
    icacls "$env:LOCALAPPDATA\Programs\Paseo\Paseo.exe" /setintegritylevel Medium
    ```

---

## 6. Usage Display: Context-Window Meter & Subscription Quota Source

### Session context-window meter (`server/internal/session.ts`, `server/internal/catalog.ts`)

Paseo's context meter requires a `session.usage` event that carries **both** `contextWindowUsedTokens` and `contextWindowMaxTokens`; the daemon's `mapModel()` only forwards `contextWindowMaxTokens` from the provider catalog and never back-fills it, so the meter previously rendered `noData`.

- The used size comes from each model request: a finished `agent_response` step carries `usage`, and `input_tokens + cache_read_tokens + output_tokens` is the context of that request. `agy`'s terminal frame is `{"event":"result","result":{"status":"SUCCESS","response":"...","duration_seconds":n,"num_turns":n,"usage":{"input_tokens":14559,"output_tokens":5,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":14564}}}`, but `result.usage` is the **conversation's cumulative total** (verified 2026-10-21: a resumed process's first turn reported 22,622 + 5,633 = 28,255 input tokens and `num_turns` kept counting). It is a context size only for a single-request turn with no step usage; `emitUsage()` reads it (`frame.result?.usage ?? frame.stats`, the top-level `stats` shape is legacy) only as that fallback.
- **After a compaction** agy reports no size: `/compact` emits a bare `checkpoint` step with no usage and a `result.usage` identical to the previous turn, so the plugin estimates the compacted context as the conversation's first model request (system prompt, tools and original request, kept by the checkpoint) plus `POST_COMPACT_SUMMARY_TOKENS = 4_000`. Across 263 local native checkpoints the next request was a median 4,160 tokens above the first (p10 2.4K, p90 9.3K); the estimate is off by a median 1.3K, under 1% of the 256K window, and the next real request replaces it. The estimate is sent as `session.usage` when the checkpoint lands (agy's own mid-turn checkpoints included) and when the compaction finishes. Background idle compactions have no Paseo turn, so their sample carries no `turnId`; the daemon still applies it to the agent's `lastUsage`. Without a native transcript the size is unknown and the sample omits the context fields, which clears the meter instead of leaving the pre-compaction value on it. Before 2026-10-21 idle compactions sent no sample, so the meter stayed at the pre-compaction size until the next request, and manual `/compact` showed the cumulative total.
- `agy models` prints only `id<TAB>label` with no context limit, so `resolveContextWindowMaxTokens()` uses a per-family estimate: `gemini-*` → `262_144`, `claude-*` → `200_000`, `gpt-oss-*` → `131_072`, otherwise `DEFAULT_CONTEXT_WINDOW_MAX_TOKENS = 262_144`. Gemini itself takes 1M, but agy runs conversations in its default `256K` context-window preset (`512K`/`900K` are paid upgrades) and checkpoints natively at ~230-250K, so `AGY_DEFAULT_CONTEXT_WINDOW_TOKENS = 262_144` is what the meter measures against. The catalogue cache key is `antigravity-resilient-v2`, so catalogues cached with the old 1M value are rediscovered. `getCatalog()` enriches every model (cached, refreshed, or fallback) before it is written to the disk cache, so the estimate reaches `listProviderModels`.
- Precedence is `providerOptions.contextWindowMaxTokens` (non-zero only) → catalog value → family default.

### `Antigravity` usage source (`server/usage.ts`, `server/credentials.ts`, `shared/input.ts`)

- `contribute()` calls `registerUsageSource({ id: "antigravity-resilient", label: "Antigravity", icon: "icon.svg", input, discover, fetch })`. The `input` schema is never serialized to the client (only `id`/`label`/`icon` reach the UI), so `shared/input.ts` uses a dependency-free `{ parseAsync }` structural schema that defaults to the keyring target `gemini:antigravity`.
- `discover()` lists the machine login as account key `hashAccountKey(credential target + identity)` with the email from the `id_token` JWT as the label, and returns `[]` when no credential is readable. `fetchUsage()` returns typed problems: `no_quota` (missing/unsupported store), `expired` (with `refreshedBy: "agy"`), `rejected` (401/403), and `error` otherwise.
- **Quota endpoint**: `POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` with `{}` and `Content-Type: application/json`. It **requires** a `User-Agent` containing `antigravity/` (`antigravity/1.0` was verified; `google-api-nodejs-client/9.15.1` returns 403) and a non-`daily-` host (the `daily-` host only returns the third-party group).
- **Verified response shape** → four windows (`usedPct = (1 - remainingFraction) * 100`): `Gemini Models · Weekly` (`gemini-weekly`), `Gemini Models · 5-hour` (`gemini-5h`, `summary: true`), `Claude and GPT models · Weekly` (`3p-weekly`), `Claude and GPT models · 5-hour` (`3p-5h`, `summary: true`), plus one `details` row per group with the `Models within this group: ` prefix stripped.
- **Credential handling**: reads the account blob from the OS credential store only. Windows uses an inlined PowerShell `CredRead` P/Invoke (via `-EncodedCommand`, exit code `3` = absent); macOS uses `security find-generic-password -s gemini:antigravity -w`; other platforms return `unsupported`. The token is **never logged, persisted, or refreshed** — rotating the hourly token is left to `agy`, so a stale token surfaces as `expired` instead of invalidating the user's login.

### Live verification

```bash
# Reload the plugin into the running local daemon and re-probe models/modes (zero LLM quota)
node paseo-antigravity-resilient/scripts/deploy-and-verify.mjs --target local --skip-smoke
```

Then confirm the daemon serves the source: `client.listUsageReports({ forceRefresh: true })` returns `antigravity-resilient:available` alongside `claude`, `grok`, and `codex` (verified locally on 2026-10-16: 4 windows, 2 detail rows, plugin log free of errors).

---

## 7. Plugin Installation & Upgrade Flow

In-place patching of `%LOCALAPPDATA%/Programs/Paseo/resources/builtin-plugins/antigravity-provider/server/internal/session.ts` required a full Paseo daemon restart after every Paseo upgrade, did not persist/replay timeline history on `history: "replay"`, and risked stamping `Low Mandatory Level` onto `%LOCALAPPDATA%\Programs\Paseo` when executed from a sandboxed worker.

With `paseo-antigravity-resilient`:
1. No in-place modification of `%LOCALAPPDATA%/Programs/Paseo` is needed; the built-in `antigravity` provider remains untouched.
2. Install or hot-reload `paseo-antigravity-resilient` via `scripts/deploy-and-verify.mjs` (which backs up `~/.paseo/config.json` to `~/.paseo/config.json.pre-antigravity-resilient.bak`, ensures `pluginsEnabled: true`, and calls `installPluginSource` / `reloadPlugin` with zero daemon restart).
3. Use `provider: "antigravity-resilient"` for resilient Antigravity sessions with full timeline replay across daemon restarts and plugin reloads.
