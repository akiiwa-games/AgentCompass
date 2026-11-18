# Router internals

Everything beyond the one-line `roll` path in SKILL.md: overview, when to self-execute, plan/record/close-out stages, allocator scoring, worker launch contract and the reference index. Read only what the task needs.

## Overview & Quickstart

- **Purpose**: task-level router for coding agents (Claude Code, Codex, Antigravity, Paseo). It classifies the task (kind, how much it needs of four categories, hours), then allocates the best-fit model and effort across your subscribed accounts using live quota windows.
- **Credential-free demo** (any directory; synthetic quotas, no network, no ledger writes, no worker launched): `agentcompass plan --demo --dry-run`
- **Dispatch**: `agentcompass roll --task "<what the worker should do>"` (`--task -` reads stdin, `--brief task.md` reads a file).
- **Command**: `agentcompass` is the only entry. Installed package: `agentcompass <command>`. Source checkout without an install: `python -X utf8 <checkout>/agentcompass <command>`, the same commands from any directory. `agentcompass -h` lists them, `agentcompass <command> -h` shows one. Do not run files under `scripts/` yourself.
- **Requirements & permissions**: Python 3.11+. Reads local quota telemetry (`~/.agent-compass/usage-cache.json`, written by the router's own collector `agentcompass usage`; the [usage-float](https://github.com/akiiwa-games/usage-float) display plugin is optional) and local policy configuration; writes a local routing ledger under `~/.agent-compass/`; launches locally installed provider CLIs (or Paseo) only when `roll` dispatches a worker.

> **System-Level Skill Loading Requirement (Host Rollout)**: `agent-compass` and its companion skills (`dot-subagent`, `chatgpt-pro-consult`, `gemini-web`) must be installed at the **system level** via `python -X utf8 setup/sync_host_globals.py` so every session loads them regardless of workspace:
> - `~/.codex/skills/<name>`, `~/.claude/skills/<name>` (`~/.claude-*/skills`), and `~/.pi/agent/skills/<name>` via directory junctions;
> - `~/.gemini/config/skills.json` (absolute paths) **plus** real top-level directories in `~/.gemini/skills/<name>` (real `SKILL.md` + live subdirectory junctions for `scripts/` and `references/`, required because Antigravity's `os.ReadDir` scanner skips top-level Windows `ReparsePoint` junctions).

For execution ownership and delegation criteria, see [SKILL.md](../SKILL.md#execution-ownership).

## 1. Standard Routing Workflow

### A2. Plan Only (Diagnosis)

Write a `task.json` file (in a scratch/temp path) describing **only the factual task and runtime constraints**, then run `plan`:

```bash
agentcompass plan --input <task.json>
```

Standard `task.json`:

```json
{
  "task": {
    "objective": "Fix race condition in token refresh lock and run unit tests",
    "expected_hours": 0.5,
    "needs": {
      "local_only": true,
      "local_only_reason": "Runs local unit tests against the workspace"
    }
  }
}
```

**Classifier Authority Rule (No Manual Classification by Parent Agents)**:
- **Do NOT manually fill `work_kind`, `difficulty`, or `profile` in `task.json`, and do NOT read classification examples or rubrics.**
- `plan` (and `roll`) automatically invoke the System-One classifier (inspect `agentcompass scorer --action show` for the effective scorer and available choices; configure it with `scorer --action set` or AgentCompass settings) to determine the atomic `work_kind` (`primary_level` `1..3`, plus optional composite `secondary_kind` / `secondary_level`) directly from `task.objective` and `task.context`, projecting `axis_demand` `0..3` over `creativity`, `coding`, `aesthetics`, and `tool_operation` deterministically in `task_intake`. If a parent agent supplies `work_kind` or `difficulty`, the classifier's judgment overrides them (`caller_classification_ignored`).
- When the caller leaves `needs.local_only` unset, a classifier profile with `local_only: true` is trusted: the router sets `needs.local_only: true`, copies the profile `reason` into `needs.local_only_reason`, and records `profile_local_only_applied`. This judgment is not rechecked by the caller-reason validator. Explicit caller values take precedence; `needs.local_only: false` in `plan` input keeps work Dot-eligible subject to route and modality constraints. `roll` has no separate negative flag.
- Factual `task` fields the parent agent may provide:
  - `objective` (required): Concrete description of what the worker must accomplish and how it will be verified.
  - `context` / `environment` (optional): Factual files, modules, or runtime context.
  - `expected_hours` (required): Estimated active worker-hours (positive number, or pass `--hours <n>`).
  - `needs` (optional): `{ "local_only": bool, "local_only_reason": "<concrete local dependency: ssh/vps/pytest/daemon/gui/credentials>", "outbound_ok": bool, "fast": bool }`.
  - `chain` (optional): `{ "id": "<chain-id>", "coupling": "low|medium|high", "total_hours": 4, "worker": { "agent_id": "<paseo-id>", "bundle": "sol_high" } }`. A quota window must cover the whole chain (`total_hours`, scaled by the bundle's duration) when `coupling` is `high`, only the step (`expected_hours`) otherwise; a window left short of that spend plus the safety margin excludes the bundle.
  - `can_wait_hours` (optional): Hours the task can wait for a short window reset before asking the user.

### B. Record an Explicit User Model/Effort Override

Only when the user explicitly names the target model or effort in the current request:

```bash
agentcompass record \
  --input <task.json> --constraint user_override --basis "<exact user quote>" \
  --provider <provider> --account <account> --model <model> --effort <effort>
```

`--effort` may be omitted (the account's row of that model keeps its level) or written as `--model <model>:<effort>` or `--model <model>-<effort>`. A level the account has no row for inherits the launch fields of its other rows of that model. The record is refused when a window that route draws is exhausted, when the route is cooling down, when the user unchecked that level, or when the user unchecked every level of the model, also for a level with no row of its own. A family name with no configured route is refused with `named_model_unavailable`, never folded to its target. A model that is off only by default (a superseded generation) can be named.

### C. Dispatch lifecycle

`roll` prepares, launches and observes the dispatch; nothing is closed out by hand. To inspect or recover one, use `agentcompass dispatch --dispatch-action status|reconcile|release` ([dispatch lifecycle](dispatch-lifecycle.md)).

---

## 2. How the Allocator Scores Candidates

Provider availability is discovered automatically from native sign-ins, Paseo launchers,
local CC Switch and local Magpie. Account disablement, removed profiles and confirmed
credential rejection automatically pause new allocations; recovery takes effect on
the next allocation. Run `agentcompass providers` to refresh and inspect the inventory.
New bound Claude/Codex launchers inherit the existing model policy; saved profiles
without a launcher are listed without changing the user's active account. See
[provider discovery and quota sources](provider-usage.md).

The sole machine-readable public catalogue is [`accounts.json`](accounts.json).
Its model rows and `capability_policy` define model settings and route scopes.
[`model-capabilities.json`](model-capabilities.json) and
[`model-capabilities.md`](model-capabilities.md) are generated views; `agentcompass data refresh [--no-fetch] [--accept-panel-change]`
rebuilds them alongside the calibrated scores and manifest, or run
`python -X utf8 skills/agent-compass/scripts/capability_catalog.py` (`--check` verifies them).

The shipped template plus discovery is the whole fleet: accounts and second logins come
from discovery, plan capacity from the discovered plan through
[`plan-templates.json`](plan-templates.json), and the calibrated scoring weights, route
order and priority targets ship in `accounts.json`. Sparse `accounts-override.json`
preferences (disabled bundles and harnesses, sign-in vetoes, scoring tweaks) apply after
discovery; its `scoring` merges key by key, so defaults it does not set still apply.
There is no machine-local fleet document: a clean install reaches the same routing from
discovery plus public data alone. Keep explanations in `~/.agent-compass/host-rules/*.md`.

Account on/off preference lives only in host policy (`allowed_accounts`), shared by setup and the tray. `verified` is launch evidence: discovery may establish it from login or usage, and setup records `true` for an accepted CLI whose sign-in cannot be read. Legacy override `verified: false` is rejected by doctor and the loader; remove that field and choose the account in the tray.

The allocator first checks discovered availability, explicit disablement, host policy,
expiry, required modalities (`needs_visual`, `needs_gui`, or `tool_operation >= 3`), configured route restrictions and whether the model has data for every
category the task demands (`>= 1`). It then scores eligible candidates: the fit of the model's four category scores
(`creativity`, `coding`, `aesthetics`, `tool_operation`, in panel standard deviations at that effort, with each demanded
category's level-1 panel-weakest target as zero point) to the task's demand, continuity (`t3 - t2` on `coupling` `medium`/`high` when resuming),
and the quota economy's price for the spend. A category's term is `min(z, c) - t1`: at demand levels 1 and 2 the cap `c` is
the level's target, at level 3 it is the category's `ceiling` in `anchor.targets.<category>.levels.3` (per facet for
`aesthetics` and `tool_operation`), the highest measured or interpolated High value of any catalogued model, so a level-3 task can prefer a
model above the panel's best. Level 3 of a facet's scale (`t3`) is the panel maximum of the measured or interpolated High values, level 1 the minimum and
level 2 the median; the anchor is only the origin of `z` (definition: `docs/technical-reference.md` section 1). `five-axis-scores.json` carries
that meaning from schema `agent-compass.five-axis-scores/6` on, and `axis_scores.require_current_schema` refuses a file of another schema; the
levels of `user-scores.json` (schema 3) are on this scale. Levels written on the earlier scale (3 = the anchor) keep their position: at or
below 2 unchanged, above 2 `2 + (L - 2) * (A - 2)` with `A` the anchor's level on the facet as of v0.7.0, declared in
`references/level-3-conversion.json` (`user_scores.convert_level`, applied once to the maintainer file and by `user_scores.LOCAL_UPGRADES`
to a local file); on `tool_operation.responsiveness`, whose earlier scale had `t2 = t3`, a level in (2, 3] becomes 2 and a level above 3 becomes
`2 + (L - 3) * s` with the declared slope `s = (t2 - t1) / (t3 - t2)` (`above_3_slopes`).
For `aesthetics`, level 1 evaluates `design` (`lmarena.webdev`, `designarena.website`, `designarena.3d`, `designarena.svg`), and
levels 2 and 3 evaluate `min(design, image_recognition)` (sharing `tool_operation.image_recognition` so both visual creation and
fine-grained visual perception depend on visual grounding). For `tool_operation`, level 1 imposes no ability requirement; level 2
evaluates per-effort `responsiveness` (`aa.ttfa`, `-ln(seconds)` from Artificial Analysis `timeToFirstAnswerToken`); and level 3
evaluates `min(responsiveness, image_recognition)` (`aa.mmmu_pro`, `lmarena.vision`) and requires image-input support.
A dynamically discovered bundle starts checked unless public data lists
its model in `superseded` (`{old id: {by: newer id, basis}}`); efforts and harnesses of one
model share the entry, and a model unknown to the data starts checked. The user's
model-management checkbox (`disabled` in accounts-override.json) overrides that default.
Checked metered bundles stay a last resort: explicit selection, `metered_ok`, or no eligible
subscription bundle. A dollar-balance account is one of those metered accounts; the rule is [Metered USD](../../../docs/technical-reference.md#metered-usd). A free pool (a free plan or a declared free allowance) is an ordinary quota pool priced by the quota economy like any other; a model with no data for a demanded category is out of automatic routing there too. Purpose-limited unmetered services (Dot, ChatGPT Web) have no quota to relieve.

A discovered plan supplies its window structure; its capacity comes only from community real-usage data per plan and window
(`five-axis-scores.json`). A plan or window without a cited capacity is unknown, and the bundles that draw it stay out of
automatic routing until the data has it (a named model still launches). A vendor plan multiplier sizes only the windows its
statement covers. A window that no public source measures takes a dated maintainer measurement when there is one (the
Claude / GPT track `3p-5h` and `3p-weekly` of Antigravity Ultra 20x); otherwise it is declared unmeasured
(`capacity_unmeasured` in `capacity_errors`; the same track on Ultra 5x, where only the Gemini bundles are routed
automatically). Antigravity and Grok have no default plan: when their subscription tier cannot be read or is
not in `plan-templates.json`, the account enters Mode B (`capacity_unknown` with `plan_unknown_reason`, surfaced in routing
notes and on the tray quota card). An unrecognized Claude or Codex plan is priced at that family's smallest paid plan with a
warning (`plan_unknown`, `plan_priced_as`). Unknown quota is distinct from unlimited access. Intrinsically unmetered services (OpenCode
free models, Dot, the ChatGPT Pro web bridge) are marked so in the template; observed
telemetry retains its source and freshness. Optional account `cadence` state
(`window_seconds`, `limit`) measures dispatch frequency and never proves remaining quota.
A local `capability_policy.remote_reroute_guard.enabled` may retain a remote adapter's
reroute policy; the public catalogue does not enable a remote-first loop.

Historical uncompressed worker context is `compacted_tokens + context_tokens`, including
all pre-compaction context replaced over the session. At the configured context rotation
threshold (`rotate_after_carried_tokens`, default 4M, set in the tray), start a fresh
worker with a handoff. The parent can still steer its own running worker under the
communication contract below. The continuation verdict in a worker notification (`turn_refusal.py`) says `roll` with the reason for the conditions the gate
enforces on a new turn (account or bundle disabled, confirmed quota exhaustion, a refusing quota window) and advises `roll` for
more: the worker's last turn ended in a provider failure (read from the watcher receipt, so before the hold is recorded), its
account is not ready, or its account, quota pool or model is held. The gate does not enforce the advisory ones. The verdict is
read-only and bounded in its own registry and quota reads, and loads the fleet through `load_accounts`, which may update host policy and holds as for any caller; an unresolvable worker or an unreadable registry is `unknown`.

---

### Model names, efforts and pins

The same rules hold in the other repositories that port this router.

**One resolver.** `model_resolution.resolve_model` is the only place a provider's spelling becomes a model. It returns `{key, source, effort, bare}`:

| `source` | Meaning | Category scores |
|---|---|---|
| `exact` | the calibrated id itself | its own row |
| `alias` | another spelling of it: prefix, date, `-latest`, `-preview`, `[1m]`, letter case, a listed alias | its own row |
| `family` | a different model of a known family (`gemini-2.5-pro`, `claude-opus-4-20250514`) | the same-series target's scores and usd_per_hour (series conversion; a small-size name on a non-small target is not in its series and gets the small row), marked estimated |
| `unknown` | no match | no scores: a demanded category (`>= 1`, except `tool_operation = 1`) is a data error and the model leaves automatic routing |

The `family` patterns are data: `accounts.json` `families`, a dated maintainer declaration whose `rows` is an ordered list of `{pattern, models}` matched against the bare name (a catalog without it is a named error, never an empty table); the first matching pattern decides and the name resolves to the first of its models the catalogue has (none: `unknown`). A name no pattern matches falls back to the longest calibrated id it extends (`<id>-...`).

A trailing `:effort` or `-effort` is an effort, not a vendor tag (`:free` stays a tag). Local telemetry, quota window bindings, health and cooldown, worker continuity, harness factors, the rival for the margin and `superseded` all read the key and also accept the raw name, except that a family guess is never answered from its target's telemetry.

`account_fleet.row_key` says which model a row runs: the key for `exact` and `alias`, the row's own name for `family` and `unknown`. A family guess folds to its target only for its estimated score. Wherever a row, a pin, a cooldown, a worker or a rival is matched, it is its own model, so naming the flagship never takes a family row. A cooldown recorded under any spelling of a model cools all its spellings. The rival for `needs_judgment` is the best row of a different model, never another effort, harness or account of the same model.

**Pins.** `constraints.model`, `constraints.effort`, `pinned_model` and `pinned_effort` hold in every mode and through `issue` and `prepare`:

- Model only: the plan picks among that model's levels by score: every level the launch contract (`dispatch_efforts`, or the row's `efforts` when unlisted) offers competes as a candidate, so the answer does not depend on which rows are written. `prepare` accepts any level that is in the contract, is offered by the row and is not unchecked; it does not rank levels again, since the score needs the task.
- Model and effort: that level stays. A level the model has no row for is carried as `<carrier>@<effort>` when `dispatch_efforts` or the row's own `efforts` offer it. Each level goes to the first row of the model that offers it, so the answer does not depend on the order of the rows.
- A family name never launches the family target; unconfigured means blocked.
- A pin on an exact or alias name matches that model's rows only. A family row is another model: it never stands in for the name, healthy or not, and a named model that is down is blocked. An unnamed task can still run the family row when its flagship is down.
- A level the user unchecked belongs to that row's own model (`row_key`). Unchecking `claude-opus-9` at `high` leaves `claude-opus-5-5` at `high` reachable, and the other way round.
- `disabled_by_default` rows stay out of routing until named; a row the user unchecked stays out even when named.
- A named route that is exhausted, cooling down or otherwise unusable gives `choice: none`, `decision.blocked: named_model_unavailable`, the reasons in `decision.warnings` and no substitute. Tell the user.
- A new pin breaks the hold of a coupled chain.

**Profile validation.** `multi_task` and `task_count` are required. `task_count` takes `1`, `2`, `3`, `>3` or a positive whole number (4 and above become `>3`); `multi_task: false` goes only with `1`. Zero, negatives, fractions, NaN, Infinity, words, null and contradictions are invalid and never repaired. A classifier's invalid answer counts as a failed attempt, ending in the next classifier or `scorer_unavailable`; a caller's invalid profile raises `invalid_caller_profile`. A cached profile is validated again on read, and a bad one is a miss. Validation is idempotent and never raises anything but `ValueError`, whatever the size of a number.

A ledger row with no saved snapshot and no split answer cannot be replayed: its recorded profile is not valid, reclassifying its text would invent a different task, and filling in an answer would invent what the classifier said. The tray marks such a row `旧记录缺字段，无法重放` in the ledger list and shows the same note instead of a replay. Rows with a saved snapshot are unaffected.

### No eligible bundle

When nothing clears the gate, `plan` returns `choice: none` with `decision.action: none`, `needs_user_decision: true` and a `reason`. If a bundle is held back only by its own account's quota windows (`quota_pools.quota_wait_hours`), `decision.next_reset` gives that bundle, the hours until its last blocking window resets, and the time. The parent can wait or decide otherwise.

### Calibration data

`calibration/` builds panel standard scores and per-effort curves from LMArena, `arena.ai` Agent/Alignment, Design Arena and Artificial Analysis; `agentcompass data refresh [--no-fetch] [--accept-panel-change]` (`calibration/refresh.py`) rebuilds the calibrated files and generated capability views in topological order and validates them; the fetching refresh runs once per release ([Reference data](#5-reference-data-shipped-and-published)). See [`../calibration/README.md`](../calibration/README.md). Raw downloads stay outside git.

### Distribution simulation and ledger replay

`scripts/route_simulation.py` shows where work goes across usage states, and replays past decisions. Usage reaches `allocator.plan` the way `roll` feeds it: a collector cache document (`providers.<account>.windows[]` with `used_pct`, `resets_at`, `fetched_at`, `stale`) goes through `usage_reading.usage_snapshot`, which produces `remaining_pct_before_reset`, the reset time and freshness. A metered USD reading follows [Metered USD](../../../docs/technical-reference.md#metered-usd). Then `plan` merges discovery and quota sources itself. Synthetic states are written as cache documents, never as hand-made allocator input. A raw ledger or cache row passed straight to `plan` has no `remaining_pct_before_reset`, so every window reads as unknown and the result ignores usage. The pre-2026-11-04 ad-hoc replays made exactly this mistake.

```bash
python -X utf8 scripts/route_simulation.py grid [--mode machine|fresh|both] [--state NAME ...] [--json out.json]
python -X utf8 scripts/route_simulation.py grid --accounts "claude pro" --accounts "opencode free" [--state NAME ...]
python -X utf8 scripts/route_simulation.py explain [--creativity N] [--coding N] [--aesthetics N] [--tool-operation N] [--local] [--state "claude scarce"] [--bundle grok47_high]
python -X utf8 scripts/route_simulation.py replay [--last N] [--json out.json]
```

- `grid` plans every point of the category demand space (`quota_economy.FIVE_AXIS_DEMAND_GRID`) x local/remote under each usage state, then reports wins per bundle and per `model@account`. It also lists the enabled models that win nowhere, each with its median score gap to the winner and its most common exclusions.
- States:
  - `live`: the current collector cache.
  - `ample`: 10% used everywhere.
  - Scarcity by account family, using discovery's family: `claude`, `codex`, antigravity `gemini` / `3p` windows, `grok` (the Grok Bot mirrors it), and the pairs `claude+codex`, `claude+antigravity` and `claude+codex+antigravity`.
  - `5h-only` scarcity: windows under 24h.
  - `unknown/stale`: readings three days old.
  - `claude tight`, `codex tight` and `antigravity tight`: a fifth left with 60% of the window to run. A cheap model's burn still fits where a strong model's does not.
  - Scarce means 95% used with 70% of the window left; a short window means 98% used with 3h left.
- `--mode machine` routes as this machine does: `accounts-override.json` checkboxes and the host policy apply. `--mode fresh` routes as a fresh install would: public data plus discovered accounts, with no personal choices and every account allowed.
- `--accounts NAME` (repeatable) routes in fresh mode for a synthetic account set instead of the accounts discovered here. The sets are `claude pro`, `claude max5`, `codex plus`, `codex pro`, `antigravity pro`, `claude pro + codex plus`, `magpie pool (3x claude pro)` and `opencode free`. Each is a discovery snapshot carrying the models that product's CLI advertises, and only the set's accounts are allowed. The advertised models are the catalog's launch surface for that harness (`route_simulation.advertised`): the launch ids of the family account's bundles, the other models in `dispatch_efforts[<launcher>]`, and the `aliases` keys prefixed `<launcher>/` (OpenCode's `opencode/<id>` free models); a model the catalog does not declare for a harness is not simulated there. Use them to check that routing works for users unlike this machine.
- `explain` prints the top of one ranking with score components. Use it to answer "why does X never win".
- `replay` re-plans each ledger `plan` record with the usage that decision saw. The record's compact usage (`fetched_at`, `status`, per-window `used_pct`/`resets_at`) is rebuilt into a cache document and read at the record's own time. Since 2026-11-04 the record keeps a pooled window's `accounts` count (`decision_ledger.usage_digest`), so a Magpie pool replays as N accounts' worth. Older records lack it; replay fills it from the live cache's pool sizes (`route_simulation.pool_counts`), on the assumption that a pool's size rarely changes. Continuity, reservations and overrides are not replayed.
- Set `AGENTCOMPASS_PUBLIC_DATA=off` to evaluate the repository's data. Otherwise a newer published set fetched into the state dir wins.
- The CLI is read-only toward the real profile. It copies the small state files routing reads (top-level JSON, `public-data/`, `grok-bot/config.json`) into a throwaway state dir, so discovery bookkeeping lands there. It reads usage with an empty discovery inventory, so the Claude session-limit overlay is skipped. `test_route_simulation.py` pins the behavior: Claude scarcity must move work off Opus on `claude`, and Codex scarcity must move work off the Codex models.

## 3. Worker Launch, Communication & Permission Contract

**Settings hold state only; never write a reason.** Users choose settings themselves in the UI. Store `disabled` only as `true` or `false`; never attach justification, notes or dated user instructions to a setting. Router decision explanations belong in plan output and ledgers, not settings.

**Questions to the main session must be sent as real messages.** A worker needing clarification/decisions must directly message its parent, never call `request_user_input_async`, `AskUserQuestion`, `ask_user_question`, `request_user_input` or similar UI-only tools that cannot communicate with the parent. In Paseo use `send_agent_prompt` to the actual parent ID with `background: true, notifyOnFinish: false`, then end the turn rather than synchronously waiting on the parent. If messaging is unavailable or fails, return `BLOCKED / needs_input` through the existing result channel and end the turn; the caller must collect/relay it. See the [communication contract](worker-communication.md) injected by `roll`. This does not restrict the main session asking the real user or bypass required approvals. For native/manual launches, include this contract and the actual parent ID, host and message address too. Missing parent information prohibits launch; the result-channel fallback applies only to delivery failures after launch.

Which channel carries the report is decided per dispatch and written into the [worker communication contract](worker-communication.md). A watched dispatch puts the full report in the final reply; the completion watcher sends that reply in one notice. A dispatch with no watcher sends `[Worker final report]` itself.

`notifyOnFinish: false` applies to a worker's question to its parent. When the main session uses `send_agent_prompt` to add work, give clearance or resume its own worker, it keeps `notifyOnFinish` true (or omits it): that completion notification is the one end message, and the worker does not also send a report. `router_gate_hook` blocks an explicit `false` from a parent to its own worker.

Disabling an account/bundle or exhausting its drawn quota window blocks new launches and new turns, while the parent can still control its own currently running worker (steer, scope cut, answering questions, wrap-up/handoff): blocking control messages saves no quota and only leaves a running worker without its wrap-up instruction. The same exception applies at the configured context rotation threshold (`rotate_after_carried_tokens`, set in the tray). Ownership must be proved by consistent locally persisted parent labels and running state by the local Paseo record; missing/unknown/idle state grants no exception, even for a prompt requesting only a commit or report. Collect existing output, cancel if necessary and roll a handoff to a fresh worker, or re-enable a disabled account in AgentCompass settings. Completion-notification checks still apply. See the [gate contract and local-state limitation](parent-address-gate.md#disabled-target-quota-and-context-rotation-gate-contract).

Paseo SDK creation does not implement MCP's `notifyOnFinish`. The router therefore starts its own notifier before SDK creation, deduplicated by dispatch ID and stable message IDs. Permission notices leave observation active; final delivery, parent closure or the seven-day watch deadline ends the process. How that exit, a dead watcher, and a follow-up turn release the reservation is defined in [In-flight quota deduction](dispatch-lifecycle.md#2-in-flight-quota-deduction). A two-minute delivery grace follows the deadline; failures remain in the durable receipt. A stopped watcher can be recovered by observing the dispatch; recovery never creates another worker. No daemon settings or installed Paseo files are modified.

If notifier startup fails, the bridge does not create a worker whose result could be lost. Repair the local SDK dependencies or notification state directory and roll again. This startup check applies only to new creation; existing workers remain observable and their replies can be collected manually.

Always pass the explicit permission mode from `selected_bundle.modeId` (never rely on CLI defaults):

| Provider | Launch Surface | Required Permission Flag / Mode | Notes |
| :--- | :--- | :--- | :--- |
| **`codex`** | Paseo / Codex CLI | `--mode full-access` | Engineering & GUI (`cua_repl`) harness for `gpt-6-astra` and `gpt-6.1-sol`. |
| **`pi`** | Paseo (`provider: "pi"`) | No `modeId` (adapter has fixed tool access and advertises no modes) | Exploration & research-packet harness for GPT models (`launch_model: "openai-codex/<model>"`). Passing `full-access` is rejected by Paseo. |
| **`claude`** | Paseo / Claude CLI | `--mode bypassPermissions` | Use the discovered launcher and configured account policy. |
| **`antigravity-resilient`** | Paseo / `agy` CLI | `modeId: "full-access"` | Use a model and effort advertised by the adapter. |
| **`opencode`** | Paseo (`provider: "opencode"`) | `modeId: "build"` | OpenCode free worker (`deepseek-v4.1-flash-free`, `launch_model: "opencode/deepseek-v4.1-flash-free"`). |
| **`dot`** | [`dot-subagent`](../../dot-subagent/SKILL.md) | `run_dot_task.py` | Remote worker (`dot2api`); requires `needs.outbound_ok: true` if sending private material. |
| **`grok-bot`** | [Grok Bot adapter](grok-bot.md) | `run_grok_bot.py` | Automatic remote text candidate; no local file transfer, private material requires outbound authorization. |
| **`chatgpt-web`** | [`chatgpt-pro-consult`](../../chatgpt-pro-consult/SKILL.md) | Browser bridge | `gpt-6-pro` read-only advisor for `architecture_reasoning` and `creative_ideation`. |

**Gemini web is a local tool, not a worker:** [`gemini-web`](../../gemini-web/SKILL.md) drives AgentCompass's signed-in Gemini Web Chrome (`gemini-web`, CDP `127.0.0.1:17849`) for Gemini / AI Studio / Flow pages and Gemini Omni 1.1 video. The calling agent runs it directly, with no routing. A worker given such a task needs `needs.local_only: true` with `local_only_reason: "Gemini Web browser on this machine"`.

---

## 4. Reference Files (Internal Router Specs — Parent Agents Need Not Read)

- [`dispatch-rules.md`](dispatch-rules.md) — The rules a dispatching session follows (the global pointer sends sessions here).
- [`accounts.json`](accounts.json) — Public model catalogue, conservative priors, route scopes, account slots and bundle definitions.
- [`classifier-cases.json`](classifier-cases.json) — System-One classifier route definitions and calibration cases (loaded automatically by the router).
- [`model-capabilities.md`](model-capabilities.md) — Generated catalogue view and prior limitations.
- [`model-capabilities.json`](model-capabilities.json) — Generated policy/model view; runtime derives its policy from effective accounts.
- [`dispatch-lifecycle.md`](dispatch-lifecycle.md) — Reservation state machine (`planned` -> `started` -> `completed`) and in-flight quota deduction.
- [`judge-workflow.md`](judge-workflow.md) — System-One `TaskProfile` schema (`score`) and low-effort judge stages (`delegate`, `select`).
- [`jev-api.md`](jev-api.md) / [`gemini-decisions-api.md`](gemini-decisions-api.md) — The question-API task scorers (TypeSafe Jev, Google Gemini Decision 1): request, key, when each runs.
- [`effort-matching.md`](effort-matching.md) — Per-model effort curves and the ledger `decision_snapshot` the tray replays.
- [`provider-usage.md`](provider-usage.md) — Live usage snapshot precedence, `capacity.json` overlay, host policy, and temporary overrides.
- [`public-data.json`](public-data.json) — The manifest that publishes the reference set (sha256 per file, data schema, publication time); see Reference data below.
- [`scripts/route_judge.py`](../scripts/route_judge.py) (with [`scorer_client`](../scripts/scorer_client.py), [`decision_ledger`](../scripts/decision_ledger.py), [`subscription_judge`](../scripts/subscription_judge.py), [`subscription_cli`](../scripts/subscription_cli.py), [`usage_reading`](../scripts/usage_reading.py), [`env_vars`](../scripts/env_vars.py)) · [`scripts/allocator.py`](../scripts/allocator.py) (with [`account_fleet`](../scripts/account_fleet.py), [`task_intake`](../scripts/task_intake.py), [`axis_scores`](../scripts/axis_scores.py), [`quota_economy`](../scripts/quota_economy.py), [`quota_pools`](../scripts/quota_pools.py), [`quota_overrides`](../scripts/quota_overrides.py), [`candidate_eval`](../scripts/candidate_eval.py), [`candidate_ranking`](../scripts/candidate_ranking.py), [`ledger_snapshot`](../scripts/ledger_snapshot.py), [`worker_sessions`](../scripts/worker_sessions.py)) · [`scripts/host_policy.py`](../scripts/host_policy.py) · [`scripts/check_router_contract.py`](../scripts/check_router_contract.py)

### Antigravity plan tier

`agy -p /usage` reports only bucket fractions, so the tier that sizes them is read from Google the way agy learns it: `scripts/antigravity_plan.py` takes agy's own sign-in from the OS keyring (service `gemini`, user `antigravity`), calls `daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` with `ideType: "ANTIGRAVITY"` metadata, and caches `paidTier` (`tier_id` and `plan`) in `~/.agent-compass/antigravity-plan.json`. Google returns `paidTier.name` `"Google AI Ultra"` for both Ultra variants and distinguishes all three paid plans by `paidTier.id`, which discovery resolves through `plan-templates.json` `tiers` (`g1-pro-tier` -> `pro`, `g1-ultra-lite-tier` -> `ultra5`, `g1-ultra-tier` -> `ultra20`) and counts as a sign-in proof. `roll`, `plan`, `providers` and `select` refresh the cache in the background every 12 hours (30 minutes after a failure); an expired agy token or failed request keeps the last good tier until `PLAN_MAX_AGE_HOURS` (48h). There is no default Antigravity plan and no unsplit `ultra` alias: when the tier cannot be read or is not in `plan-templates.json`, the account enters Mode B (`capacity_unknown` with `plan_unknown_reason`, out of automatic routing and shown on the tray card). Off switch: `AGENTCOMPASS_ANTIGRAVITY_PLAN=off`.

## 5. Reference Data (Shipped and Published)

**Rule: no install, test, workflow or schedule reads the boards (arena, Artificial Analysis, vendor pages) at run time.** The fetching refresh (`agentcompass data refresh` without `--no-fetch`) runs once, when a release is prepared. Between releases the only data that moves is what the maintainer publishes on purpose: a new model (`data refresh --model <id>`), a corrected hand-written score, a corrected launch row. Every install (git checkout, pip or `uv tool` wheel, tray installer) ships the reference set under `references/` with its manifest [`public-data.json`](public-data.json), and reads a newer set the maintainer published for its data schema on the repository's `main` branch ([`scripts/public_data.py`](../scripts/public_data.py)).

- **The set**: what a run-time reader reads, through the one accessor `public_data.document`: `accounts.json` (`account_fleet.fleet_document`, `auto_bundles`, `worker_sessions.shipped_scoring`, the reference fleet's `families`, the tray's model table, tuning defaults, display names and prices), `plan-templates.json` (`provider_inventory.shipped_plan_templates`), `classifier-cases.json` (`scorer_client.load_classifier_cases`), `five-axis-scores.json` (`axis_scores.load_five_axis`) and `user-scores.json` (`user_scores.load_public`). Left out: `level-3-conversion.json` (the frozen conversion of the user-scores upgrade step, owned with that code), `public-measurements.json` (read only by the maintainer's refresh) and the generated `model-capabilities.{json,md}` (documentation views, no run-time reader).
- **One set per process**: a process takes the whole set (every file, read once into memory and verified against its manifest) the first time a reader asks, and keeps it (`public_data.held`); later changes on disk, a newer set fetched by another process or the removal of an old set cannot split it. A reader gets reference content only from `public_data.document`, so it cannot mix two sets; `test_public_data.py` fails on a run-time line that opens a file of the set any other way. The tray moves to a newer set only as a whole, when it rebuilds its accounts view (`router_data._cached_accounts`, `public_data.retake`). Every fall-back is to the shipped set as a whole.
- **Publication**: the published set of data schema N lives in `published/<N>/` at the repository root (the manifest and the five files): one push by the maintainer, a reviewable diff of exactly what installs receive, no second checkout or branch. Only `agentcompass data publish` (in a checkout) writes it: it accepts `references/` (below), copies the files with their manifest (sha256 and size of every file, the schema, `published_at`, moved only when the content changes and always later than the publication it replaces) to `published/<SCHEMA_VERSION>/`, and writes the same manifest to `references/public-data.json`, which a release ships. Editing `references/` on `main` publishes nothing, and release preparation under a new schema writes `published/<new>/`, which installs of the old schema never read. The `public-data` workflow runs `agentcompass data publish --check --since <base>` on every push and pull request that can change either side: the published set must match its manifest, be accepted by the code at that commit, be published later than the set it replaces (an install ignores a set not newer than what it has) and not in the future; `--check-released` runs the acceptance of every released version of the same schema (tags `v*`) against it, which is what those installs will run. The release workflow runs `--check --release` (a release ships `references/` as the latest published set of its schema, so a release publishes its own data first) and `--check-released`. `--since <rev>` with a revision that is not a commit of this repository fails the check, saying so (`public_data.manifest_at`): the comparison with the previous publication never passes by default. A published set of an older schema (`published/<old>/`) is frozen, since installs of that schema keep reading it: the check fails when it no longer matches its own manifest, and with `--since` when it changed since that revision (`public_data.check_frozen`). A wheel or the product ships `references/`, never `published/` (`packaging/check_payload.py`).
- **Acceptance**: a set is accepted only when this code can route with it: `public_data.accept` runs the provenance checks, then, with the candidate held, every run-time reader (`account_fleet.launch_problems` and `load_accounts`, the `scoring` rotation threshold, `load_five_axis`, `load_public`, the classifier cases into the scorer evidence, the plan templates), an offline plan on the reference fleet (what `plan --demo --dry-run` runs) and one on the set's own fleet priced with the reference fixture's plan tiers (`demo_plan.priced_fleet`) and a synthetic quota snapshot, which must have at least one selectable candidate; each plan is serialized as the CLI prints it (`route_judge.render`, strict JSON); no network, no classifier call. Every reference file is parsed strictly wherever it is read (`public_data.parse_document`: `NaN`, `Infinity` and a literal that overflows to infinity are a named `ReferenceDataError`). What the acceptance proves: this code's readers load the set, and both plans complete and serialize with it. What it does not prove: a reader path only the live fleet reaches (discovered accounts, the user's own settings, real quota), which is what the fall-back below is for. One function, used by `data publish`, the workflow and an install before it records a fetched set. It costs about 0.2 to 1 second; an install runs it once per newly published set (and once more after its own code changed), inside the background check, never on a caller's path. A refusal names the step and the cause.
- **What an install cannot run**: a reference row whose account family or bundle harness the launch code does not know (`account_fleet.LAUNCH_FAMILIES`, `LAUNCH_HARNESSES`), or whose effort is not in the set's own `dispatch_efforts` row for its harness, rejects the whole set: complete or absent, a model that needs new code needs a release. The effort table is data in the same set, so this check holds a bundle to its own set, not to what a CLI accepts. A field this code does not read is ignored, like any unknown field; data whose meaning needs newer code under the same schema is what the schema bump is for.
- **Launch values**: the declared patterns (`scripts/launch_values.py`: a model id `[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*`, a bundle id the same plus `#`, every other name one word `[A-Za-z0-9][A-Za-z0-9._-]*`) apply to reference data only, where it enters (`account_fleet.launch_problems` on the source document of `load_accounts`): a reference row that does not match is a named `ReferenceDataError` that refuses the set. They accept every shipped value and every id this code generates (`auto:<account>:<harness>:<model>@<effort>`, its `#<sha8>` collision suffix). Values from the user's machine (account names, model ids a provider lists, ids derived from them) are never dropped by a pattern; they are made safe where they are used: passed as their own argument (`launch_values.argument`, which refuses an empty value, a leading `-`, a line break or NUL), quoted for the syntax they land in (`launch_values.toml_string` for codex `-c model_reasoning_effort=`, `launch_values.shell_word` in the Orca command a parent runs) or reduced to a file-name part (`launch_values.path_part` for a CLI run directory). A value that cannot be expressed safely refuses that one launch with a named `LaunchValueError`, before anything is written for it; the account and its other bundles stay.
- **Never on the failure line**: if a reader fails on the fetched set at run time all the same (a shape the acceptance did not exercise, such as a field only the live fleet reaches), the read-only part of the run is repeated on the shipped set as a whole (`public_data.with_fallback`; the tray falls back the same way when it rebuilds). Three rules bound it:
  - *No effect is repeated.* Every writer marks its effect (`public_data.effect`: a ledger row in `decision_ledger.append_ledger`, a committed change in `dispatch_registry.transaction`, a quota override, a CLI worker launch in `roll.cli`); an error after the first effect is the run's answer and nothing is re-run. Per entry point: a `route_judge` stage (`plan`, `roll`, `select`, `providers`, `record`, ...) re-runs only when it failed before its ledger row, reservation or launch, from freshly parsed arguments; the gate hook falls back around its reference reads only (the rotation threshold, `load_dispatch_settings`, the accounts and window verdict of `check_account_quota_exhausted`), never around the end notices it delivers, and its allow-all degrade applies only when the shipped set fails too.
  - *A refusal is not a data error.* Only a data error re-runs: `public_data.ReferenceDataError` (a reader's explicit shape check) or `LookupError`, `TypeError`, `AttributeError`, `ArithmeticError` (`public_data.is_data_error`). Every other `ValueError` (a refusal such as `named_model_unavailable`, a quota refusal, a `dispatch_registry` refusal, an input error), `OSError` and `sqlite3` errors are answers, never re-run and never blamed on the set.
  - *A run-time blame is not permanent.* When the shipped set succeeds where the fetched one failed, the fetched set is set aside for `SET_ASIDE_HOURS` (24 hours) by this code (`set_aside_version`, `set_aside_by`, `set_aside_until`, `set_aside_error` in `state.json`); `doctor` names it with the error and `plan` warns `reference_data_refused`. It is taken again when the period ends, at once when a newer set is accepted or this install's code changes. A set that is truly bad costs one failed attempt and one re-run per process that tries it, once every 24 hours. Only the acceptance rejects a set for good (`rejected_version`); a failure the shipped set produces as well blames nothing.
- **Data schema**: `public_data.SCHEMA_VERSION` (7 since v0.7.0: `agent-compass.five-axis-scores/6`, `user-scores.json` schema 3) binds the shape of the reference files to the code that reads them: an install fetches only `published/<its schema>/`. Bump it with any change after which a released reader of the previous schema would misread data written for this code (a new required field, a renamed key, a changed meaning). What the check covers: `--check-released` fails a publication that a released reader of the same schema refuses. What stays discipline: a change of meaning an older reader cannot detect (it reads the field and draws a different conclusion); the procedure in [Publishing a model between releases](../calibration/README.md#publishing-a-model-between-releases) says when to bump.
- **When and at what cost**: `roll`, `plan`, `providers` and `select` start a background check at most once every 6 hours (30 minutes after a failed attempt), in its own short-lived process (`public_data.py --refresh --claimed`); the caller pays one state read and, when a check is due, one locked state write and the process start, never the network or the acceptance. The tray updater runs `agentcompass data --refresh` after every update, and `agentcompass data --refresh` checks now. A check reads `<PUBLIC_DATA_URL><schema>/public-data.json` (`agentcompass/origin.py`: `https://raw.githubusercontent.com/akiiwa-games/AgentCompass/main/published/`) over plain HTTPS, with no credentials and only a `User-Agent: AgentCompass-public-data` and `Cache-Control: no-cache` header (`HTTPS_PROXY` / `NO_PROXY` and the system proxy apply), then only the files whose sha256 differs from what the install already has. An unchanged manifest costs one request. Each request times out after 5 seconds (10 for `agentcompass data --refresh`, which a user waits for). A recorded check or attempt time in the future (a clock that ran ahead) counts as no check. A 404 (nothing published yet for this schema) is a completed check with status `unpublished`: no `last_error`, the normal interval. Offline or slow, routing runs on the last accepted set, else the shipped copy; `data --refresh` then exits 0 and reports the outcome, also when another process holds the state (`busy`); exit 1 only when the state directory cannot be written.
- **Cache**: `~/.agent-compass/public-data/` (under `ROUTER_STATE_DIR` when set): `state.json` (version, `published_at`, `accepted_by`, `fetched_at`, `checked_at`, last error, note, a version the acceptance rejected, a version set aside at run time and until when), written under the process-safe lock of `override_store`, and `sets/<version>/` (the manifest and files; the current and previous sets are kept).
- **Precedence**: shipped copy < fetched set < your own settings (`~/.agent-compass/user-scores.json`, `accounts-override.json`, host policy and every other file you own; the channel writes none of them and writes only under `public-data/`). Manual capability scores follow `local user > maintainer (references/user-scores.json, shipped or published) > measured > estimated/converted`, mapped from the facet's demand-level scale (level 1 = panel minimum, 2 = median, 3 = panel maximum, unclamped beyond) (`agentcompass score set <model> <category>[.<facet>] <level> [--public]`; `agentcompass score suggest` prints read-only review suggestions against measured public boards without altering manual scores). Shipped references that differ from their own manifest (a checkout being edited) are read as they are.
- **Visibility**: `agentcompass doctor` prints `Reference data:` (source, version, publication, fetch and check time, note); `agentcompass providers` includes it as `public_data`; `agentcompass data` prints the full status.
- **Off switch**: `AGENTCOMPASS_PUBLIC_DATA=off` (`0`, `false`, `no` also work), or `{"enabled": false}` in `~/.agent-compass/public-data-config.json`; the environment wins. Off means no network call, no read of the channel's state, and only the shipped copy is read. The same file accepts `interval_hours` and `url` (a mirror of the `published/` root); `AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS` and `AGENTCOMPASS_PUBLIC_DATA_URL` override them.
- **What a publication cannot add**: data only. A model whose launch needs code cannot be published this way: a new harness or provider (`account_fleet.LAUNCH_FAMILIES`, the Paseo bridge), a new launch field, a new account family or sign-in that discovery must find, or a new schema of any file (that bumps `SCHEMA_VERSION` and ships with a release). A model on an existing harness (a model row, alias, family row, bundle, `dispatch_efforts` row, its five-axis scores and maintainer scores) can be published, as `gemini-4-hydrogen-high` was added.
- **Provenance checks**: [`scripts/reference_provenance.py`](../scripts/reference_provenance.py) holds the reference files to the data provenance rule: every declaration in `accounts.json` and `plan-templates.json` is a dated public citation or a dated `maintainer` declaration, and `user-scores.json` holds `kind: "maintainer"` scores only. The refresh runs them before it writes and after it rebuilds, `data publish` and every install run them before accepting a set, and the test suite runs them on the shipped files. A score file of another schema is refused by `axis_scores.require_current_schema` with a message naming what to run.
- **Your own state across updates**: what a released version wrote for you is read in the current shape through one table of steps per file, each naming the earlier shape it reads, applied by the file's single loader before strict validation; a read never writes, and the next save writes the current shape. `user_scores.LOCAL_UPGRADES` (earlier score schema), `accounts_override.LOCAL_UPGRADES` (`disabled` reason strings, the `migration_ambiguous_enabled` record of the v0.2.0 to v0.5.0 tray, which those releases never applied, and `allowed_cwd`), `state_upgrade.HOST_RULE_UPGRADES` (renamed tokens in the `shared.md`, `codex.md` and `daemon-append.md` overlays, which stay as written; only the retired `@@ROUTE_JUDGE@@` placeholder is refused, never a name in the user's prose), `provider_inventory.LOCAL_UPGRADES` (the launcher removals kept in `provider-inventory.json`) and the `plugins.load` autostart marker. An `allowed_cwd` becomes a `cwd_regex` only for an entry the regex matches exactly as the v0.5.0 path-component matcher did (`accounts_override._entry_carries_over`); the others are refused with the edit that carries them. Callers read `accounts-override.json` through `accounts_override.load_override` (the tray edit and the usage collector included), never with their own `json.loads`. A shape whose meaning depends on something the file does not say is refused with the file named, not guessed. Caches, ledgers and the dispatch database are rebuilt or skipped, never migrated: `dispatch_registry.prepare` refuses a decision recorded under an older task profile by itself, never the database, and `dispatch_fallback.Chain.run` ends such a dispatch's failure with `older_profile` instead of replanning it; an inventory that cannot be read, or of an unknown schema that holds removals, fails with its message (`provider_inventory.previous_inventory`) instead of being replaced. A refusal that remains prints a fix that works when followed and says what deleting the file discards.
- **Maintainers**: as a step of preparing a release, run `agentcompass data refresh` (or `agentcompass data refresh --no-fetch` from cached downloads; add `--accept-panel-change` to write a rebuild whose panel members changed, which is otherwise refused; [`calibration/refresh.py`](../calibration/refresh.py)) to rebuild `public-measurements.json`, `five-axis-scores.json`, `model-capabilities.json` and `model-capabilities.md` in topological order and validate the provenance checks and the `user-scores.json` schema, then `agentcompass data publish`. Adding a model is the catalogue edit plus `agentcompass data refresh --model <id>`, which merges only that model's measurement entries, conflicts and checks into the shipped `public-measurements.json` (board z-scores on the shipped scale of each source, the tier as built from the fetched board, so with the panel scale unchanged the model's row and five-axis block equal a full refresh's) and rebuilds the rest (a panel model or the anchor is refused and needs the full refresh), then `agentcompass data publish`, commit and push: installs pick it up at their next check ([Publishing a model between releases](../calibration/README.md#publishing-a-model-between-releases)). After hand-editing `accounts.json`, `plan-templates.json`, `classifier-cases.json` or `user-scores.json` (`agentcompass score set ... --public`), run `agentcompass data refresh --no-fetch` and `agentcompass data publish`. A breaking data change bumps `SCHEMA_VERSION` in `public_data.py`: the next `data publish` writes `published/<new>/`, which only the release that carries the change reads, and installs of the older schema keep `published/<old>/`.
