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
