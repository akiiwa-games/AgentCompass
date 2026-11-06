# Dispatch Lifecycle & Reservation Ledger

Every `agentcompass plan`, `roll` or `record` call registers a reservation row in `~/.agent-compass/dispatch-registry.sqlite3` and appends an audit entry to `~/.agent-compass/router-ledger.jsonl`.

## 1. State Machine

```
planned ──► started ──► heartbeat ──► completed
   │           │            │
   └───────────┴────────────┴──────► failed | cancelled | timed_out
```

| State | Active Reservation? | Lease TTL | Transition Command |
| :--- | :---: | :---: | :--- |
| `planned` | Yes | `3 min` (`180s`) | Created by `plan` / `roll` / `record`; expires automatically if never started. |
| `started` | Yes | `15 min` (`900s`) | `roll`, once the worker is created and observed on the host. |
| `heartbeat` | Yes | `15 min` (`900s`) | The host observation that `roll` and `reconcile` perform. |
| `completed` | No | Released | `reconcile`, when the host reports the worker finished. |
| `failed` / `cancelled` / `timed_out` | No | Released | `reconcile`, or `release` with a reason (section 3). |

## 2. In-Flight Quota Deduction

When `allocator.plan()` evaluates an account window from the effective account fleet:
1. Active dispatches (`planned` or `started`/`heartbeat` with `lease_until > now`) whose `at` timestamp is newer than `usage[account].fetched_at` (or where `used_pct` has not yet moved past the dispatch baseline) reserve their `expected_usd` (or `expected_pct`) against remaining window capacity.
2. Once the worker finishes (`completed`/`failed`/`cancelled`) or `usage-cache.json` reflects the spent quota, the reservation is released so quota is never double-counted.

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

## 4. Outcome, Reuse and Launch Failures

- **Outcome is the finish, not the latest host state.** Paseo keeps changing a worker after it finishes: it closes an idle one when it collects the runtime (about an hour later), fails its session on a plugin reload, and archives it. The bridge's notification watcher records its first terminal sighting (`terminal` in `paseo-notifications/<dispatch_id>.json`); a later observation that sees `closed`, `error` or `archivedAt` after a recorded finish settles the dispatch `completed`. `closed` with `attentionReason: finished` is `completed` too; `closed` without a finish is `canceled`.
- **Which idle workers are resumed.** Only a worker whose last dispatch completed, that is not `closed` (its runtime was collected, so a resume would cold-restore it), that was active within `REUSE_MAX_IDLE_HOURS` (24 h), and whose brief or title shares words with the new brief. Matching the bundle alone is not continuity.
- **Resume binds before it sends.** The bridge writes the new dispatch labels on the worker before sending the turn, so a send cut off by the caller's timeout is still observable; a send the host refused gives the worker its previous labels back and falls back to a fresh create. The bridge call for a resume may take up to 90 s.
- **A refused create cools down.** A definitive Paseo create failure (`host_creation_failed`) is recorded in the event reason with a fixed class (`provider_refresh_timeout`, `provider_unavailable`, `quota`, `model_unavailable`, `unknown`; never the SDK text) and in `launch-failures.json`. Routing excludes the whole account for the first two classes and only that model otherwise, for 15 min, doubling per consecutive failure up to 2 h; a worker that starts on the account clears it. Uncertain outcomes (timeouts, lost replies) never cool anything down. `ROUTER_LAUNCH_FAILURES=off` disables it; a path relocates the file.
