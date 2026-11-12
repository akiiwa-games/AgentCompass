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
- `plan` (and `roll`) automatically invoke the System-One classifier (inspect `agentcompass scorer --action show` for the effective scorer and available choices; configure it with `scorer --action set` or AgentCompass settings) to determine the route category (`work_kind`) and `TaskProfile` (including `axis_demand` `0..3` over `divergence`, `coding`, `aesthetics`, `tool_operation`) directly from `task.objective` and `task.context`. If a parent agent supplies `work_kind` or `difficulty`, the classifier's judgment overrides them (`caller_classification_ignored`).
- When the caller leaves `needs.local_only` unset, a classifier profile with `local_only: true` is trusted: the router sets `needs.local_only: true`, copies the profile `reason` into `needs.local_only_reason`, and records `profile_local_only_applied`. This judgment is not rechecked by the caller-reason validator. Explicit caller values take precedence; `needs.local_only: false` in `plan` input keeps work Dot-eligible subject to route and modality constraints. `roll` has no separate negative flag.
- Factual `task` fields the parent agent may provide:
  - `objective` (required): Concrete description of what the worker must accomplish and how it will be verified.
  - `context` / `environment` (optional): Factual files, modules, or runtime context.
  - `expected_hours` (required): Estimated active worker-hours (positive number, or pass `--hours <n>`).
  - `needs` (optional): `{ "local_only": bool, "local_only_reason": "<concrete local dependency: ssh/vps/pytest/daemon/gui/credentials>", "outbound_ok": bool, "fast": bool }`.
  - `chain` (optional): `{ "id": "<chain-id>", "coupling": "low|medium|high", "total_hours": 4, "worker": { "agent_id": "<paseo-id>", "bundle": "sol_high" } }`.
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
[`model-capabilities.md`](model-capabilities.md) are generated views; `agentcompass data refresh [--no-fetch]`
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

The allocator first checks discovered availability, explicit disablement, host policy,
expiry, required modalities (`needs_visual`, `needs_gui`, or `tool_operation >= 3`), configured route restrictions and whether the model has data for every
category the task demands (`>= 1`). It then scores eligible candidates: the fit of the model's four category scores
(`divergence`, `coding`, `aesthetics`, `tool_operation`, in panel standard deviations at that effort, with each demanded
category's level-1 panel-weakest target as zero point) to the task's demand, continuity (`t3 - t2` on `coupling == "high"`),
and the quota economy's price for the spend.
For `tool_operation`, level 1 imposes no ability requirement; level 2 evaluates per-effort `responsiveness` (`aa.ttfa`,
`-ln(seconds)` from Artificial Analysis `timeToFirstAnswerToken`); and level 3 evaluates `min(responsiveness, image_recognition)`
(`aa.mmmu_pro`, `lmarena.vision`) and requires image-input support.
A dynamically discovered bundle starts checked unless public data lists
its model in `superseded` (`{old id: {by: newer id, basis}}`); efforts and harnesses of one
model share the entry, and a model unknown to the data starts checked. The user's
model-management checkbox (`disabled` in accounts-override.json) overrides that default.
Checked metered bundles stay a last resort: explicit selection, `metered_ok`, or no eligible
subscription bundle. A dollar-balance account is one of those metered accounts; the rule is [Metered USD](../../../docs/technical-reference.md#metered-usd). A free pool (a free plan or a declared free allowance) is an ordinary quota pool priced by the quota economy like any other; a model with no data for a demanded category is out of automatic routing there too. Purpose-limited unmetered services (Dot, ChatGPT Web) have no quota to relieve.

A discovered plan supplies its window structure; its capacity comes only from community real-usage data per plan and window
(`five-axis-scores.json`). A plan or window without a cited capacity is unknown, and its account stays out of automatic
routing until the data has it. Antigravity and Grok have no default plan: when their subscription tier cannot be read or is
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
threshold (`rotate_after_carried_tokens`, default 2M, set in the tray), start a fresh
worker with a handoff. The parent can still steer its own running worker under the
communication contract below.

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

`calibration/` builds panel standard scores and per-effort curves from LMArena, `arena.ai` Agent/Alignment, Design Arena and Artificial Analysis; `agentcompass data refresh [--no-fetch]` (`calibration/refresh.py`) rebuilds the calibrated files, generated capability views and `public-data.json` in topological order and validates them. See [`../calibration/README.md`](../calibration/README.md). Raw downloads stay outside git.

### Distribution simulation and ledger replay

`scripts/route_simulation.py` shows where work goes across usage states, and replays past decisions. Usage reaches `allocator.plan` the way `roll` feeds it: a collector cache document (`providers.<account>.windows[]` with `used_pct`, `resets_at`, `fetched_at`, `stale`) goes through `route_judge.usage_snapshot`, which produces `remaining_pct_before_reset`, the reset time and freshness. A metered USD reading follows [Metered USD](../../../docs/technical-reference.md#metered-usd). Then `plan` merges discovery and quota sources itself. Synthetic states are written as cache documents, never as hand-made allocator input. A raw ledger or cache row passed straight to `plan` has no `remaining_pct_before_reset`, so every window reads as unknown and the result ignores usage. The pre-2026-11-04 ad-hoc replays made exactly this mistake.

```bash
python -X utf8 scripts/route_simulation.py grid [--mode machine|fresh|both] [--state NAME ...] [--json out.json]
python -X utf8 scripts/route_simulation.py grid --accounts "claude pro" --accounts "opencode free" [--state NAME ...]
python -X utf8 scripts/route_simulation.py explain [--divergence N] [--coding N] [--aesthetics N] [--tool-operation N] [--local] [--state "claude scarce"] [--bundle grok47_high]
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
- `--accounts NAME` (repeatable) routes in fresh mode for a synthetic account set instead of the accounts discovered here. The sets are `claude pro`, `claude max5`, `codex plus`, `codex pro`, `antigravity pro`, `claude pro + codex plus`, `magpie pool (3x claude pro)` and `opencode free`. Each is a discovery snapshot carrying the models that product's CLI advertises, and only the set's accounts are allowed. Use them to check that routing works for users unlike this machine.
- Set `AGENTCOMPASS_PUBLIC_DATA=off` to evaluate the repository's data. Otherwise a newer fetched public copy in the state dir wins.
- `explain` prints the top of one ranking with score components. Use it to answer "why does X never win".
- `replay` re-plans each ledger `plan` record with the usage that decision saw. The record's compact usage (`fetched_at`, `status`, per-window `used_pct`/`resets_at`) is rebuilt into a cache document and read at the record's own time. Since 2026-11-04 the record keeps a pooled window's `accounts` count (`route_judge.usage_digest`), so a Magpie pool replays as N accounts' worth. Older records lack it; replay fills it from the live cache's pool sizes (`route_simulation.pool_counts`), on the assumption that a pool's size rarely changes. Continuity, reservations and overrides are not replayed.
- The CLI is read-only toward the real profile. It copies the small state files routing reads (top-level JSON, `public-data/`, `grok-bot/config.json`) into a throwaway state dir, so discovery bookkeeping lands there. It reads usage with an empty discovery inventory, so the Claude session-limit overlay is skipped. `test_route_simulation.py` pins the behavior: Claude scarcity must move work off Opus on `claude`, and Codex scarcity must move work off the Codex models.

## 3. Worker Launch, Communication & Permission Contract

**Settings hold state only; never write a reason.** Users choose settings themselves in the UI. Store `disabled` only as `true` or `false`; never attach justification, notes or dated user instructions to a setting. Router decision explanations belong in plan output and ledgers, not settings.

**Questions to the main session must be sent as real messages.** A worker needing clarification/decisions must directly message its parent, never call `request_user_input_async`, `AskUserQuestion`, `ask_user_question`, `request_user_input` or similar UI-only tools that cannot communicate with the parent. In Paseo use `send_agent_prompt` to the actual parent ID with `background: true, notifyOnFinish: false`, then end the turn rather than synchronously waiting on the parent. If messaging is unavailable or fails, return `BLOCKED / needs_input` through the existing result channel and end the turn; the caller must collect/relay it. See the [communication contract](worker-communication.md) injected by `roll`. This does not restrict the main session asking the real user or bypass required approvals. For native/manual launches, include this contract and the actual parent ID, host and message address too. Missing parent information prohibits launch; the result-channel fallback applies only to delivery failures after launch.

**Workers with messaging tools must send a final report to the actual parent before ending the turn**, for done, failed and blocked outcomes. In Paseo send `[Worker final report]` through `send_agent_prompt` with `background: true, notifyOnFinish: false` (use equivalent nonblocking delivery if the host schema omits `background`), and check the send result. Include the full report or a concise result with an accessible location for the full report; keep the complete report in the final reply too. If sending fails, state the delivery failure in the final reply. Workers without messaging tools, such as agy, put the full report in the final reply and rely on the router's short `[AgentCompass worker notification]` and response pointer; CLI callers must collect the output themselves. The state notice does not replace a messaging-capable worker's report. See the injected [worker communication contract](worker-communication.md).

`notifyOnFinish: false` above applies only to worker-to-parent questions and final reports. When the main session uses `send_agent_prompt` to add work, give clearance or resume its own worker, it must set `notifyOnFinish: true` (or omit it): the completion notification and final reply are the result channel for workers without messaging tools, such as agy. `router_gate_hook` blocks an explicit `false` from a parent to its own worker.

Disabling an account/bundle or exhausting its drawn quota window blocks new launches and new turns, while the parent can still control its own currently running worker (steer, scope cut, answering questions, wrap-up/handoff): blocking control messages saves no quota and only leaves a running worker without its wrap-up instruction. The same exception applies at the configured context rotation threshold (`rotate_after_carried_tokens`, set in the tray). Ownership must be proved by consistent locally persisted parent labels and running state by the local Paseo record; missing/unknown/idle state grants no exception, even for a prompt requesting only a commit or report. Collect existing output, cancel if necessary and roll a handoff to a fresh worker, or re-enable a disabled account in AgentCompass settings. Completion-notification checks still apply. See the [gate contract and local-state limitation](parent-address-gate.md#disabled-target-and-context-rotation-gate-contract).

Paseo SDK creation does not implement MCP's `notifyOnFinish`. The router therefore starts its own notifier before SDK creation, deduplicated by dispatch ID and stable message IDs. Permission notices leave observation active; final delivery, parent closure or the seven-day watch deadline ends the process. A two-minute delivery grace follows the deadline; failures remain in the durable receipt. A stopped watcher can be recovered by observing the dispatch; recovery never creates another worker. No daemon settings or installed Paseo files are modified.

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
| **`chatgpt-web`** | [`chatgpt-pro-consult`](../../chatgpt-pro-consult/SKILL.md) | Browser bridge | `gpt-6-pro` advisor for `complex_build_plan` and `direction_reasoning`. |

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
- [`effort-matching.md`](effort-matching.md) — Per-model effort curves and the ledger `decision_snapshot` the tray replays.
- [`provider-usage.md`](provider-usage.md) — Live usage snapshot precedence, `capacity.json` overlay, host policy, and temporary overrides.
- [`public-data.json`](public-data.json) — Manifest of the public reference data (sha256 per file, data schema, publish time); see Public reference data below.
- [`scripts/route_judge.py`](../scripts/route_judge.py) · [`scripts/allocator.py`](../scripts/allocator.py) (with [`account_fleet`](../scripts/account_fleet.py), [`task_intake`](../scripts/task_intake.py), [`axis_scores`](../scripts/axis_scores.py), [`quota_economy`](../scripts/quota_economy.py), [`quota_pools`](../scripts/quota_pools.py), [`quota_overrides`](../scripts/quota_overrides.py), [`candidate_eval`](../scripts/candidate_eval.py), [`candidate_ranking`](../scripts/candidate_ranking.py), [`ledger_snapshot`](../scripts/ledger_snapshot.py), [`worker_sessions`](../scripts/worker_sessions.py)) · [`scripts/host_policy.py`](../scripts/host_policy.py) · [`scripts/check_router_contract.py`](../scripts/check_router_contract.py)

### Antigravity plan tier

`agy -p /usage` reports only bucket fractions, so the tier that sizes them is read from Google the way agy learns it: `scripts/antigravity_plan.py` takes agy's own sign-in from the OS keyring (service `gemini`, user `antigravity`), calls `daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` with `ideType: "ANTIGRAVITY"` metadata, and caches `paidTier` (`tier_id` and `plan`) in `~/.agent-compass/antigravity-plan.json`. Google returns `paidTier.name` `"Google AI Ultra"` for both Ultra variants and distinguishes all three paid plans by `paidTier.id`, which discovery resolves through `plan-templates.json` `tiers` (`g1-pro-tier` -> `pro`, `g1-ultra-lite-tier` -> `ultra5`, `g1-ultra-tier` -> `ultra20`) and counts as a sign-in proof. `roll`, `plan`, `providers` and `select` refresh the cache in the background every 12 hours (30 minutes after a failure); an expired agy token or failed request keeps the last good tier until `PLAN_MAX_AGE_HOURS` (48h). There is no default Antigravity plan and no unsplit `ultra` alias: when the tier cannot be read or is not in `plan-templates.json`, the account enters Mode B (`capacity_unknown` with `plan_unknown_reason`, out of automatic routing and shown on the tray card). Off switch: `AGENTCOMPASS_ANTIGRAVITY_PLAN=off`.

## 5. Public Reference Data (Updates Between Releases)

Prices, new models, category scores, plan capacities, manual maintainer scores and classifier calibration change faster than releases. Every install (git checkout, pip wheel, tray installer) ships `accounts.json`, `plan-templates.json`, `classifier-cases.json`, `five-axis-scores.json` and `user-scores.json` with [`public-data.json`](public-data.json), and keeps them current from the repository's `main` branch ([`scripts/public_data.py`](../scripts/public_data.py)):

- **Fetch**: `roll`, `plan`, `providers` and `select` start a background check at most once every 6 hours (30 minutes after a failed attempt). It reads `public-data.json` from `https://raw.githubusercontent.com/akiiwa-games/AgentCompass/main/skills/agent-compass/references/` over plain HTTPS (no credentials; `HTTPS_PROXY` / `NO_PROXY` and the system proxy apply), then only the files whose sha256 differs from what the install already has. Each request times out after 5 seconds; the run never waits for the check (process exit waits at most 2 seconds) and keeps the set it started with, so a refresh applies to the next run.
- **Verify**: every file must match the published sha256 and parse as JSON (`user-scores.json` is validated as `kind: "maintainer"`), or nothing is applied. A manifest whose `schema_version` is newer than the install reads is ignored with a note until AgentCompass is updated; it is never half-applied. Schema 3 (2026-11-11) switches scoring to the four capability categories (`divergence`, `coding`, `aesthetics`, `tool_operation`), ships `five-axis-scores.json` and `user-scores.json` in the public channel, and retires schema 1..2 (`MIN_SCHEMA_VERSION = 3`).
- **Cache**: `~/.agent-compass/public-data/` (under `ROUTER_STATE_DIR` when set): `state.json` (version, `published_at`, `fetched_at`, `checked_at`, last error, note) and `sets/<version>/` (the manifest and files; the current and previous sets are kept).
- **Precedence**: shipped copy < fetched set < your own settings (`~/.agent-compass/user-scores.json`, `accounts-override.json`, host policy). Manual capability scores follow `local user > maintainer (references/user-scores.json) > measured > estimated/converted`, mapped from the 1–3 demand-level scale (`agentcompass score set <model> <category>[.<facet>] <level> [--public]`). The fetched set is read only when it is complete, verified and published after the shipped copy; shipped references with local edits (they no longer match their manifest, e.g. a checkout being edited) are read as they are. Offline or after a failed check, the last good fetched set is read, otherwise the shipped copy.
- **Visibility**: `agentcompass doctor` prints the `Reference data:` source, version and fetch time; `agentcompass providers` includes it as `public_data`; `agentcompass data` prints the full status and `agentcompass data --refresh` checks now.
- **Off switch**: `AGENTCOMPASS_PUBLIC_DATA=off` (`0`, `false`, `no` also work), or `{"enabled": false}` in `~/.agent-compass/public-data-config.json`; the environment wins. Off means no network call and only the shipped copy is read. The same file accepts `interval_hours` and `url` (a mirror); `AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS` and `AGENTCOMPASS_PUBLIC_DATA_URL` override them.
- **Maintainers**: run `agentcompass data refresh` (or `agentcompass data refresh --no-fetch` from cached downloads; [`calibration/refresh.py`](../calibration/refresh.py)) to rebuild `public-measurements.json`, `five-axis-scores.json`, `model-capabilities.json`, `model-capabilities.md` and `public-data.json` in topological order and validate the `public-data` checks and `user-scores.json` schema. After hand-editing `accounts.json`, `plan-templates.json` or `classifier-cases.json` alone (`agentcompass score set ... --public` updates `public-data.json` automatically), run `python -X utf8 skills/agent-compass/scripts/public_data.py manifest --write` and commit the manifest with them (`published_at` moves only when content changes; also run `capability_catalog.py` when `accounts.json` or `user-scores.json` changes). `manifest --check` and the test suite fail on a stale manifest. A breaking data change bumps `SCHEMA_VERSION` in `public_data.py` and the manifest together, so older installs keep their last compatible data.
