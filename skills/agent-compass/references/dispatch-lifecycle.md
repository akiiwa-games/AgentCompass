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
| same, past the create deadline | Stays open, `next_action` says it is overdue | The daemon has no cancel, so calling it failed could release a worker about to start. A daemon restart settles it (`failed`, outcome unknown). |
| `failed` | `create_failed` at once | The run is over and the daemon never reruns a key by itself; only a new request could, and `launching` never sends one. |
| none, before the deadline | Stays `launching` | The request may still be on its way. |
| none, at the deadline | `create_failed` | The bridge refuses to send after the send window (60 s after the claim), so nothing can arrive any more. |
| `completed` | `canceled` | Created, then removed. |

The create deadline runs from the claim: 240 s by default, 480 s for `antigravity-resilient` (`CREATE_DEADLINES` in `dispatch_registry.py`), leaving room for the daemon's own provider timeouts. A host that cannot be read settles nothing (`observe_error`).

While `launching`, Paseo's `get_agent_status` answers "not found": the agent does not exist yet, and that is not a failure. Do not roll again; run `next_argv`. The worker notification goes out only at an end: `finished`, `error`, or `creation failed`, which says the brief can be rolled again. A slow create sends nothing.

### One writer per workspace

`roll --workspace W` refuses a second writer: `status: blocked`, `cause: workspace_has_active_writer`, nothing launched. W is held by any unsettled dispatch recorded against it (`prepared`, `launching`, `launched`, `started`, or `expired` after launch) and by any router-launched agent running there, even after its dispatch completed (a worker can be sent a follow-up). A dispatch recorded before this rule counts as a writer; the caller itself never does. The check is repeated in the transaction that inserts the row, so of two concurrent rolls one wins. If running agents cannot be read, the recorded dispatches decide and `assumptions` says so. A roll without `--workspace` is not checked.

Read-only work coexists with a writer. It is read-only when `--access read-only` says so, otherwise when the classifier's `interaction_style` is not `engineering`; `--access write` overrides the classifier the other way. Titles and task text are never matched. Nothing enforces read-only on the worker, so `--access read-only` is the caller's statement.

`blocked_by` lists each holder (`dispatch_id`, `state`, `agent_id`, `task_id`, `title`). `next_action` gives the three ways out: wait for it and roll again; `--access read-only` if the brief only reads; `--allow-concurrent "<why>"` to run both anyway, which writes a ledger row (`stage: allow_concurrent`, with the reason and the holders) and keeps the reason on the dispatch.

### Repeating a brief

A repeated `roll` of an open brief hands back that dispatch (`pending`, `next_argv`) and starts nothing. `--new-worker` always creates a new dispatch and a new worker, leaving the old one running, subject to the writer rule above.

### Watching

`launch --watch-seconds N` returns the current state (`pending` or `started`) within N seconds: each bridge call is cut at what is left, and it starts no further observation with under 5 s left, so it can return up to 5 s early. A watch under 1 s, such as 0, gets one observation of up to 5 s so it still reads the host. `roll --wait` is the same watch, except that sending a create may take its full 25 s whatever the wait. `poll-seconds` only spaces the observations.

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

`release` observes first, so a create that is over settles and releases. It refuses a live worker, and a create the daemon is still running, because it cancels neither.

## 4. Outcome, Reuse and Launch Failures

- **Outcome is the finish, not the latest host state.** Paseo keeps changing a worker after it finishes: it closes an idle one when it collects the runtime (about an hour later), fails its session on a plugin reload, and archives it. The bridge's notification watcher records its first terminal sighting (`terminal` in `paseo-notifications/<dispatch_id>.json`); a later observation that sees `closed`, `error` or `archivedAt` after a recorded finish settles the dispatch `completed`. `closed` with `attentionReason: finished` is `completed` too; `closed` without a finish is `canceled`.
- **Which idle workers are resumed.** Only a worker whose last dispatch completed, that is not `closed` (its runtime was collected, so a resume would cold-restore it), that was active within `REUSE_MAX_IDLE_HOURS` (24 h), and whose brief or title shares words with the new brief. Matching the bundle alone is not continuity.
- **Resume binds before it sends.** The bridge writes the new dispatch labels on the worker before sending the turn, so a send cut off by the caller's timeout is still observable; a send the host refused gives the worker its previous labels back and falls back to a fresh create. The bridge call for a resume may take up to 90 s.
- **A refused create cools down.** A definitive Paseo create failure (`host_creation_failed`) is recorded in the event reason with a fixed class (`provider_refresh_timeout`, `provider_unavailable`, `quota`, `model_unavailable`, `unknown`; never the SDK text) and in `launch-failures.json`. Routing excludes the whole account for the first two classes and only that model otherwise, for 15 min, doubling per consecutive failure up to 2 h; a worker that starts on the account clears it. Uncertain outcomes (timeouts, lost replies) never cool anything down. `ROUTER_LAUNCH_FAILURES=off` disables it; a path relocates the file.
