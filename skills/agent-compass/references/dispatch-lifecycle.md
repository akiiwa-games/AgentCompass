# Dispatch Lifecycle & Reservation Ledger

Every Paseo `roll` keeps a reservation row in `~/.agent-compass/dispatch.sqlite3` (`plan` and `record` only register a decision, which reserves nothing) and appends an audit entry to `~/.agent-compass/router-ledger.jsonl`.

## 1. State Machine

```
prepared ──► launching ──► launched ──► started ──► completed
   │             │             │           │
   │             │             └───────────┴──────► failed | canceled
   │             ├──► create_failed
   │             └──► failed (never sent)
   └──► canceled
any open state ──lease lapses──► expired ──a later observation──► the state the host reports
```

| State | Active Reservation? | Lease | Meaning |
| :--- | :---: | :---: | :--- |
| `prepared` | Yes | 5 min | Route bound, nothing sent. |
| `launching` | Yes | 10 min | Single-use claim taken, create sent or being sent; no agent observed yet. |
| `launched` / `started` | Yes | 10 min, renewed by each observation | The host shows the bound agent; `started` once it has run. |
| `completed` | No | Released | The host reports the worker finished. |
| `failed` | No | Released | The worker errored, or the host refused before sending (no parent, no notifier). |
| `create_failed` | No | Released | Paseo created no worker and none can appear later. Rolling the brief again is safe. |
| `canceled` | No | Released | Released before launch, or the host's worker was closed or removed unfinished. |
| `expired` | No | Released | The lease lapsed without a fresh observation. Not an outcome: a launched row stays open until a later observation settles it. |

`roll`, `launch` and `reconcile` observe and settle these; `release` only ever ends what is already over (section 3).

### How a create ends

The bridge's `create` call is cut off after 25 s, and the daemon carries on regardless: it owns the create across socket lifetimes and keeps the outcome in a durable record under the idempotency key (the dispatch ID). While no agent exists, every observation reads that record, so the answer the bridge never received is not lost.

| Daemon's record, no agent exists | Result | Why it is final |
| :--- | :--- | :--- |
| `accepted` ... `prompt_started` | Stays `launching` (`creating: true`), `next_action` says so | The worker can still appear. |
| same, past the create deadline | Stays open, `next_action` says it is overdue, and `release` refuses | The daemon has no cancel, so calling it failed could release a worker about to start. The only way out is restarting the daemon, which settles it (`failed`, outcome unknown). The real records hold no create stuck like this so far (967 agent creates read on 2026-11-07). |
| `failed`, no agent in its record | `create_failed` at once | The run is over and the daemon never reruns a key by itself; only a new request could, and `launching` never sends one. |
| none, before the deadline | Stays `launching` | The request may still be on its way. |
| none, at the deadline | `create_failed` | The bridge refuses to send after the send window (60 s after the claim), so nothing can arrive any more. |
| agent gone (the daemon answers `Previously created agent no longer exists`, not a phase), or `failed` with an agent in its record (its prompt failed), then removed | `canceled`, `next_action` says to check what the worker did before rolling again | Created, then removed before this dispatch observed it. |

The create deadline runs from the claim: 240 s by default, 480 s for `antigravity-resilient` (`CREATE_DEADLINES` in `dispatch_registry.py`), leaving room for the daemon's own provider timeouts. A host that cannot be read settles nothing (`observe_error`).

An outcome-unknown failure (a daemon restart ended the run) still settles `create_failed`, but cools nothing down in `launch_failures`, because it says nothing against the provider. Only the process that ended the row records a cooldown, so two settling one row count once.

Known residual risks: a request the daemon has not admitted by the deadline is settled as `create_failed` on the assumption that it can no longer arrive. Its send window closes at 60 s, so that would take a frame delayed by minutes on loopback, which could not be produced in review. If one did arrive, the settled row is not observed again: the worker would run unbound, seen only by the writer rule on a later `roll --workspace`. A tombstone create under the same key would close this and is not done. Separately, a restart in the milliseconds between the daemon admitting a create and starting its run leaves the record at `accepted` for good.

While `launching`, Paseo's `get_agent_status` answers "not found": the agent does not exist yet, and that is not a failure. Do not roll again; run `next_argv`. The worker notification goes out only at an end: `finished`, `error`, `creation failed`, which says the brief can be rolled again, or `worker removed before it was observed`. A slow create sends nothing. A record that cannot be read keeps the watcher polling, and its watch limit still ends with a notice.

A resume is judged the same way, with no clock. The notification is `worker unavailable` only when Paseo answers that the worker no longer exists or is archived. Until the worker carries this dispatch's labels its state is the previous turn's, so nothing is sent, and if it cannot be read the watcher keeps polling. A resume that never binds therefore stays silent until `release` stops its watcher or the 7-day watch limit ends it with a notice.

A resume that binds but whose turn never lands holds its lease only until it lapses: only a seen run or a user message after the launch renews it, or recovers it once expired. When the lease lapses the watcher sends one `turn not delivered` notice and keeps watching, so a turn that lands later still reports its finish. A worker this dispatch saw under its own labels and later carrying another dispatch's ends the watch with `superseded by a later dispatch`.

### One writer per workspace

`roll --workspace W` refuses a second writer: `status: blocked`, `cause: workspace_has_active_writer`, nothing launched. W is held by any unsettled dispatch recorded against it (`prepared`, `launching`, `launched`, `started`, or `expired` after launch) and by any router-launched agent running there, even after its dispatch completed (a worker can be sent a follow-up). A dispatch recorded before this rule counts as a writer; the caller itself never does, so a coordinator can roll into its own workspace. The check is repeated in the transaction that inserts the row, so of two concurrent rolls one wins. If running agents cannot be read, the recorded dispatches decide and `assumptions` says so: a dispatch that completed and whose worker is running a follow-up can then be missed.

The rule goes by workspace ID. Directories only warn: many normal parallel dispatches have no `--workspace`, and several workspace IDs can sit on one checkout on purpose. A writer roll looks at the directory it will land in (the one Paseo gives for `--workspace W`; otherwise a resumed worker's own directory, or the parent's cwd) and `assumptions` and `warnings` name other unsettled writer dispatches recorded there. If Paseo does not give the directory, nothing is named. A roll records its directory, and an observation corrects it to where the worker is.

A resume stays in its workspace too: with `--workspace W`, a compatible worker living elsewhere is not resumed (`worker_workspace_mismatch`) and the dispatch creates a new worker in W instead.

Read-only work coexists with a writer. The System-One classifier decides `access` (`read-only` or `write`), and `--access read-only|write` (in `--help-all`) overrides it if needed. `interaction_style` does not decide this: it does not separate reading from writing. Titles and task text are never matched in code.

`blocked_by` lists each holder (`dispatch_id`, `state`, `agent_id`, `task_id`, `title`). `next_action` gives the ways out: wait for the current writer to finish and roll again, create a separate worktree for parallel work, or pass `--access read-only` if the brief only reads.

### Repeating a brief

A repeated `roll` of an open brief hands back that dispatch (`pending`, `next_argv`) and starts nothing. `--new-worker` always creates a new dispatch and a new worker, leaving the old one running, subject to the writer rule above.

### Watching

A cooldown is best effort. If its store cannot be written, the dispatch still settles and the failure goes uncounted.

`launch --watch-seconds N` returns the current state (`pending` or `started`) within N seconds: each bridge call is cut at what is left, and it starts no further observation with under 5 s left, so it can return up to 5 s early. Only the first observation can exceed that limit: under 1 s of watch, such as 0, it gets up to 5 s so it still reads the host. `roll` (which passes `watch_seconds=0`) and `launch` on a `prepared` row are the same watch, except that sending a create may take its full 25 s, and the first observation can follow it, so up to about 30 s. `poll-seconds` only spaces the observations.

## 2. In-Flight Quota Deduction

When `allocator.plan()` evaluates an account window from the effective account fleet:
1. Active dispatches (`prepared`, `launching`, `launched` or `started` with `lease_until > now`) whose `at` timestamp is newer than `usage[account].fetched_at` (or where `used_pct` has not yet moved past the dispatch baseline) reserve their `expected_usd` (or `expected_pct`) against remaining window capacity.
2. Once the dispatch ends (`completed`, `failed`, `create_failed`, `canceled`) or `usage-cache.json` reflects the spent quota, the reservation is released so quota is never double-counted.

## 3. CLI Commands

`roll` prepares, launches and observes the dispatch itself; nothing needs to be marked by hand. These commands exist for inspection and recovery:

```bash
# Where is this dispatch?
agentcompass dispatch --dispatch-action status --dispatch-id <dispatch_id>

# Re-observe the host and update every open dispatch (or one, with --dispatch-id)
agentcompass dispatch --dispatch-action reconcile

# Cancel a dispatch you will not use; its reservation is released
agentcompass dispatch --dispatch-action release --dispatch-id <dispatch_id> --parent-id <your_session_id> --basis "<why>"
```

`release` observes first, so a create that is over settles and releases. It refuses a live worker, and a create the daemon is still running, because it cancels neither. While Paseo cannot be read it refuses anything sent and not yet settled, since either could be alive; a dispatch never sent needs no host. A worker Paseo reads that does not carry this dispatch's labels is different: that answer will not change, so `release` waits only for the lease to lapse and its refusal gives the time. A release that ends a dispatch also stops its notification watcher, whose receipt then reads `creation_failed`.

## 4. Outcome, Reuse and Launch Failures

- **Outcome is the finish, not the latest host state.** Paseo keeps changing a worker after it finishes: it closes an idle one when it collects the runtime (about an hour later), fails its session on a plugin reload, and archives it. The bridge's notification watcher records its first terminal sighting (`terminal` in `paseo-notifications/<dispatch_id>.json`); a later observation that sees `closed`, `error` or `archivedAt` after a recorded finish settles the dispatch `completed`. `closed` with `attentionReason: finished` is `completed` too; `closed` without a finish is `canceled`.
- **Which idle workers are resumed.** Only a worker whose last dispatch completed, that is not `closed` (its runtime was collected, so a resume would cold-restore it), that was active within `REUSE_MAX_IDLE_HOURS` (24 h), and whose brief or title shares words with the new brief. Matching the bundle alone is not continuity.
- **Resume binds before it sends.** The bridge writes the new dispatch labels on the worker before sending the turn, so a send cut off by the caller's timeout is still observable; a send the host refused gives the worker its previous labels back and falls back to a fresh create. The bridge call for a resume may take up to 90 s.
- **A refused create cools down.** A definitive Paseo create failure (`host_creation_failed`) is recorded in the event reason with a fixed class (`provider_refresh_timeout`, `provider_unavailable`, `quota`, `model_unavailable`, `unknown`; never the SDK text) and in `launch-failures.json`. Routing excludes the whole account for the first two classes and only that model otherwise, for 15 min, doubling per consecutive failure up to 2 h; a worker that starts on the account clears it. Uncertain outcomes (timeouts, lost replies) never cool anything down. `ROUTER_LAUNCH_FAILURES=off` disables it; a path relocates the file.
- **A model the provider says does not exist is latched out.** When that create failure also carries the provider's own structured error code (`failure_code`, e.g. the Antigravity plugin's `INVALID_MODEL`, which the daemon records as the creation's `errorCode`) and the code is in `model_latch.MODEL_UNAVAILABLE_CODES`, `model-unavailable.json` latches that account's model. Only the code decides; no error text is read. Routing, a named model and prepare exclude it, `plan`/`roll` warn about it first and the tray model list marks it with its next retry. It lifts on the first of: a worker on that account and model starting after the latch, discovery (`model-catalog.json`) listing the model in a probe newer than the latch, `model_latch.RETRY_HOURS` (the same as an account latch, `account_state.LATCH_RETRY_HOURS`) passing so the next roll re-probes it, or `model_latch.py --clear <account>/<model>`. The user's model checkboxes are never changed. `ROUTER_MODEL_LATCH=off` disables it; a path relocates the file.
- **A provider that fails a worker is held out.** Two ways reach it, with the provider's own message verbatim. (1) Stuck retry: some providers retry a failing request forever (opencode 1.x `session.status` retry; Paseo renders it, and pi's `auto_retry_start`, as a timeline error `Provider retry (attempt N): <message>`; omp's as a structured status line). The bridge's per-dispatch watcher reads the running worker's timeline tail; when nothing but retry notices has happened for the whole `AGENTCOMPASS_RETRY_STUCK_SECONDS` window (default 300 s; `AGENTCOMPASS_RETRY_STUCK_ATTEMPTS` sets a minimum attempt count, default 1), it saves a terminal with `failure_code` `provider_retry_stuck`, the message, attempts, the provider's `next` when given, whether the turn had done work before retrying (`partial_work`), then cancels the turn (retried once; `cancelled: false` is told to the parent) and tells the parent. (2) Turn failed: a worker whose turn ends in error with the provider's error text (`agent.lastError`, only when it is this turn's: a resumed worker's error before its turn started is the previous turn's; opencode 2.x fails a free model at once: "Free usage limit reached. ...") gets `failure_code` `provider_turn_failed`. The registry marks the dispatch `failed` (cause `worker_failed:<code>`) and `provider_retry.py` records it once per dispatch in `provider-retry.json` (also from the watcher's receipt at the next plan or tray usage pass). The configured System-One classifier reads the message and decides the scope: `account` holds the quota pool of the failed model (the whole account when it has a single pool) with no retry time; `model`/`transient` hold only that model with the usage-reading backoff (2 min doubling, capped at 30 min; a later provider `next` wins); `none` (not the provider's doing, e.g. a tool error) and `task_input` (this task's own input refused, e.g. a prompt too long for the model) hold nothing, and a failed turn read that way becomes the worker's error (`worker_turn_failed` / `task_input_rejected`, with their own next_action and no provider wording); `unknown` (or no classifier answer) holds the model and widens to its pool only when another model of the same pool is held for an unknown reason too, and that widened hold backs off like a model hold: only the classifier's own `account` answer holds indefinitely. Pools (`provider_retry.pool_of`) come from structure, never names: models drawing a common quota window (window `models`/`models_except` scopes, the usage reading's own `models`, explicit `draws`) are one pool, so Antigravity's Gemini and third-party models are two; where one provider of the account launches under several provider prefixes of its own (`opencode/` Zen free models, `opencode-go/` Go), each prefix is a pool. A pool hold is keyed `ACCOUNT#POOL` and never blocks another pool; widening never crosses pools. An account hold taken while the account had one pool (e.g. a Zen free-usage hold before OpenCode Go was connected) becomes a hold of its failed models' pools once the account has several (`scope_account_holds`, when the accounts load). No code reads the message. Routing never picks a held bundle and plan/roll warn first; a model the user names (`roll --model`) is dispatched anyway as a probe, and a dispatch launched after the hold whose turn the watcher saw end on the model's reply (`replied` on the finished terminal: the last timeline item is an assistant message and nothing back to the prompt is an error; Paseo shows a cancelled turn as idle/finished too, so a completion without it lifts nothing) lifts the account's hold, its pool's and that model's. On an account with a usage reader, a good usage reading newer than the hold, or a credential other than the one it was taken under (re-login), lifts that account or pool hold and its model holds, as it lifts the plan-inactive latch (`provider_inventory`). Otherwise clear with the tray card's Clear link, `agentcompass hold --clear ACCOUNT` (every hold of it), `ACCOUNT#POOL` (that pool's hold and its models') or `agentcompass hold --clear ACCOUNT/MODEL` (that model's hold only). The tray account list and model list show the provider's message, the held pool and next retry; with pools held the card's Clear link names and clears one pool at a time. `ROUTER_PROVIDER_RETRY=off` disables it; a path relocates the file.
- **A worker that ends in error after Paseo created it** (the agent exists, so this is not a create failure) settles `failed` with `error: worker_failed` and a `next_action` to read its error and tell the user. Paseo keeps only text for such a failure (`lastError`; a turn's error code is not kept on the agent), so nothing is latched from it.
