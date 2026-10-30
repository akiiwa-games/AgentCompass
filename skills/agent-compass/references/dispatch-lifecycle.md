# Dispatch Lifecycle & Reservation Ledger

Every `route_judge.py plan` or `record` call registers a reservation row in `~/.agent-compass/dispatch-registry.sqlite3` and appends an audit entry to `~/.agent-compass/router-ledger.jsonl`.

## 1. State Machine

```
planned ──► started ──► heartbeat ──► completed
   │           │            │
   └───────────┴────────────┴──────► failed | cancelled | timed_out
```

| State | Active Reservation? | Lease TTL | Transition Command |
| :--- | :---: | :---: | :--- |
| `planned` | Yes | `3 min` (`180s`) | Created by `route_judge.py plan`; expires automatically if never started. |
| `started` | Yes | `15 min` (`900s`) | `dispatch-state --id <id> --state started --worker-id <id>` |
| `heartbeat` | Yes | `15 min` (`900s`) | `dispatch-state --id <id> --state heartbeat` |
| `completed` | No | Released | `dispatch-state --id <id> --state completed` |
| `failed` / `cancelled` / `timed_out` | No | Released | `dispatch-state --id <id> --state failed --note "<reason>"` |

## 2. In-Flight Quota Deduction

When `allocator.plan()` evaluates an account window from the effective account fleet:
1. Active dispatches (`planned` or `started`/`heartbeat` with `lease_until > now`) whose `at` timestamp is newer than `usage[account].fetched_at` (or where `used_pct` has not yet moved past the dispatch baseline) reserve their `expected_usd` (or `expected_pct`) against remaining window capacity.
2. Once the worker finishes (`completed`/`failed`/`cancelled`) or `usage-cache.json` reflects the spent quota, the reservation is released so quota is never double-counted.

## 3. CLI Commands

```bash
# Mark a planned dispatch as started once the child agent is launched
python -X utf8 ~/.codex/skills/agent-compass/scripts/route_judge.py dispatch-state \
  --id <dispatch_id> --state started --worker-id <agent_or_task_id>

# Close the dispatch when the child finishes
python -X utf8 ~/.codex/skills/agent-compass/scripts/route_judge.py dispatch-state \
  --id <dispatch_id> --state completed
```
