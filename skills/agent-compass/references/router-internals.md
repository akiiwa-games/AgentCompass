# Router internals

Everything beyond the one-line `roll` path in SKILL.md: overview, when to self-execute, plan/record/close-out stages, allocator scoring, worker launch contract and the reference index. Read only what the task needs.

## Overview & Quickstart

- **Purpose**: task-level router for coding agents (Claude Code, Codex, Antigravity, Paseo). It scores a task's difficulty and kind, then allocates the best-fit model and effort across your subscribed accounts using live quota windows.
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
- `plan` (and `roll`) automatically invoke the System-One classifier (inspect `agentcompass scorer --action show` for the effective scorer and available choices; configure it with `scorer --action set` or AgentCompass settings) to determine the route category (`work_kind`), difficulty floor, and 10D `TaskProfile` (including `divergence` `1..3`) directly from `task.objective` and `task.context`. If a parent agent supplies `work_kind` or `difficulty`, the classifier's judgment overrides them (`caller_classification_ignored`).
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
Its model rows and `capability_policy` define public capability priors and route scopes.
[`model-capabilities.json`](model-capabilities.json) and
[`model-capabilities.md`](model-capabilities.md) are generated views; regenerate
with `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` (`--check` verifies them).

The shipped template plus discovery is the whole fleet: accounts and second logins come
from discovery, plan capacity from the discovered plan through
[`plan-templates.json`](plan-templates.json), and the calibrated scoring weights, route
order and priority targets ship in `accounts.json`. Sparse `accounts-override.json`
preferences (disabled bundles and harnesses, sign-in vetoes, scoring tweaks) apply after
discovery; its `scoring` merges key by key, so defaults it does not set still apply.
There is no machine-local fleet document: a clean install reaches the same routing from
discovery plus public data alone. Keep explanations in `~/.agent-compass/host-rules/*.md`.

The allocator first checks discovered availability, explicit disablement, host policy,
expiry, required tools and modalities, configured route restrictions and ability floors.
It then scores eligible candidates using the effective scoring weights, model capability
priors, optional personal preference vectors, continuity and available quota evidence.
Public routes ship a calibrated model order and public scoring gives preference vectors
weight 0.49. A route restricts models only with `restrict_models: true` and a nonempty
`preferred` list. Account `model_defaults[model].disabled` supplies an explicit default
for dynamically discovered bundles; individual saved bundle choices still take precedence.

A discovered plan supplies capacity and burn priors; a family whose product does not
report its tier locally uses the documented `<family>.default` plan (Antigravity: Ultra
20x), never no capacity. Claude/Codex without a discovered plan stay unknown. Unknown
quota is distinct from unlimited access. Intrinsically unmetered services (OpenCode
free models, Dot, the ChatGPT Pro web bridge) are marked so in the template; observed
telemetry retains its source and freshness. Optional account `cadence` state
(`window_seconds`, `limit`) measures dispatch frequency and never proves remaining quota.
A local `capability_policy.remote_reroute_guard.enabled` may retain a remote adapter's
reroute policy; the public catalogue does not enable a remote-first loop.

Historical uncompressed worker context is `compacted_tokens + context_tokens`, including
all pre-compaction context replaced over the session. At the configured rotation limit,
start a fresh worker with a handoff. The parent can still steer its own running worker
under the communication contract below.

---

## 3. Worker Launch, Communication & Permission Contract

**Settings hold state only; never write a reason.** Users choose settings themselves in the UI. Store `disabled` only as `true` or `false`; never attach justification, notes or dated user instructions to a setting. Legacy string values load as `true` and must be saved as booleans. Router decision explanations belong in plan output and ledgers, not settings.

**Questions to the main session must be sent as real messages.** A worker needing clarification/decisions must directly message its parent, never call `request_user_input_async`, `AskUserQuestion`, `ask_user_question`, `request_user_input` or similar UI-only tools that cannot communicate with the parent. In Paseo use `send_agent_prompt` to the actual parent ID with `background: true, notifyOnFinish: false`, then end the turn rather than synchronously waiting on the parent. If messaging is unavailable or fails, return `BLOCKED / needs_input` through the existing result channel and end the turn; the caller must collect/relay it. See the [communication contract](worker-communication.md) injected by `roll`. This does not restrict the main session asking the real user or bypass required approvals. For native/manual launches, include this contract and the actual parent ID, host and message address too. Missing parent information prohibits launch; the result-channel fallback applies only to delivery failures after launch.

**Workers with messaging tools must send a final report to the actual parent before ending the turn**, for done, failed and blocked outcomes. In Paseo send `[Worker final report]` through `send_agent_prompt` with `background: true, notifyOnFinish: false` (use equivalent nonblocking delivery if the host schema omits `background`), and check the send result. Include the full report or a concise result with an accessible location for the full report; keep the complete report in the final reply too. If sending fails, state the delivery failure in the final reply. Workers without messaging tools, such as agy, put the full report in the final reply and rely on the router's short `[AgentCompass worker notification]` and response pointer; CLI callers must collect the output themselves. The state notice does not replace a messaging-capable worker's report. See the injected [worker communication contract](worker-communication.md).

`notifyOnFinish: false` above applies only to worker-to-parent questions and final reports. When the main session uses `send_agent_prompt` to add work, give clearance or resume its own worker, it must set `notifyOnFinish: true` (or omit it): the completion notification and final reply are the result channel for workers without messaging tools, such as agy. `router_gate_hook` blocks an explicit `false` from a parent to its own worker.

Disabling an account/bundle blocks new launches and new turns, while the parent can still control its own currently running worker (steer, scope cut, answering questions, wrap-up/handoff). The same exception applies at the 2M historical-context limit. Ownership must be proved by consistent locally persisted parent labels and running state by the local Paseo record; missing/unknown/idle state grants no exception, even for a prompt requesting only a commit or report. Collect existing output, cancel if necessary and roll a handoff to a fresh worker, or re-enable a disabled account in AgentCompass settings. Quota and completion-notification checks still apply. See the [gate contract and local-state limitation](parent-address-gate.md#disabled-target-and-context-rotation-gate-contract).

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

- [`accounts.json`](accounts.json) — Public model catalogue, conservative priors, route scopes, account slots and bundle definitions.
- [`classifier-cases.json`](classifier-cases.json) — System-One classifier route definitions and calibration cases (loaded automatically by the router).
- [`model-capabilities.md`](model-capabilities.md) — Generated catalogue view and prior limitations.
- [`model-capabilities.json`](model-capabilities.json) — Generated policy/model view; runtime derives its policy from effective accounts.
- [`dispatch-lifecycle.md`](dispatch-lifecycle.md) — Reservation state machine (`planned` -> `started` -> `completed`) and in-flight quota deduction.
- [`judge-workflow.md`](judge-workflow.md) — System-One `TaskProfile` schema (`score`) and low-effort judge stages (`delegate`, `select`).
- [`effort-matching.md`](effort-matching.md) — Task demand (depth x property difficulty) vs model ability x per-model effort curve, and the ledger `decision_snapshot` the tray replays.
- [`provider-usage.md`](provider-usage.md) — Live usage snapshot precedence, `capacity.json` overlay, host policy, and temporary overrides.
- [`public-data.json`](public-data.json) — Manifest of the public reference data (sha256 per file, data schema, publish time); see Public reference data below.
- [`scripts/route_judge.py`](../scripts/route_judge.py) · [`scripts/allocator.py`](../scripts/allocator.py) · [`scripts/host_policy.py`](../scripts/host_policy.py) · [`scripts/check_router_contract.py`](../scripts/check_router_contract.py)

### Antigravity plan tier

`agy -p /usage` reports only bucket fractions, so the tier that sizes them is read from Google the way agy learns it: `scripts/antigravity_plan.py` takes agy's own sign-in from the OS keyring (service `gemini`, user `antigravity`), calls cloudcode `loadCodeAssist` and keeps `paidTier` (for example `g1-ultra-tier`, "Google AI Ultra") in `~/.agent-compass/antigravity-plan.json`. Discovery reads only that file, sets the account `plan` (which picks the plan template) and counts the read as a sign-in proof. `roll`, `plan`, `providers` and `select` refresh it in the background every 12 hours (30 minutes after a failure); an expired agy token or failed request keeps the last good tier, and with no tier yet the `antigravity.default` template (Ultra 20x) applies. Off switch: `AGENTCOMPASS_ANTIGRAVITY_PLAN=off`. Google reports Ultra without a 5x/20x split, so Ultra routes on the Ultra 20x prior unless the account document names `Google AI Ultra (5x)`.

## 5. Public Reference Data (Updates Between Releases)

Prices, new models, plan capacities and classifier calibration change faster than releases. Every install (git checkout, pip wheel, tray installer) ships `accounts.json`, `plan-templates.json` and `classifier-cases.json` with [`public-data.json`](public-data.json), and keeps them current from the repository's `main` branch ([`scripts/public_data.py`](../scripts/public_data.py)):

- **Fetch**: `roll`, `plan`, `providers` and `select` start a background check at most once every 6 hours (30 minutes after a failed attempt). It reads `public-data.json` from `https://raw.githubusercontent.com/akiiwa-games/AgentCompass/main/skills/agent-compass/references/` over plain HTTPS (no credentials; `HTTPS_PROXY` / `NO_PROXY` and the system proxy apply), then only the files whose sha256 differs from what the install already has. Each request times out after 5 seconds; the run never waits for the check (process exit waits at most 2 seconds) and keeps the set it started with, so a refresh applies to the next run.
- **Verify**: every file must match the published sha256 and parse as JSON, or nothing is applied. A manifest whose `schema_version` is newer than the install reads is ignored with a note until AgentCompass is updated; it is never half-applied. Schema 2 (2026-11-04) declares window scope as data (`models` / `models_except`, see [`window_scope.py`](../scripts/window_scope.py)); schema-1 sets used a `"3p"` pseudo-tag that only retired code understood, so this install no longer reads them (`MIN_SCHEMA_VERSION`).
- **Cache**: `~/.agent-compass/public-data/` (under `ROUTER_STATE_DIR` when set): `state.json` (version, `published_at`, `fetched_at`, `checked_at`, last error, note) and `sets/<version>/` (the manifest and files; the current and previous sets are kept).
- **Precedence**: shipped copy < fetched set < your own settings. The fetched set is read only when it is complete, verified and published after the shipped copy; shipped references with local edits (they no longer match their manifest, e.g. a checkout being edited) are read as they are. `accounts-override.json` and host policy still apply on top, and the channel never writes any of them. Offline or after a failed check, the last good fetched set is read, otherwise the shipped copy.
- **Visibility**: `agentcompass doctor` prints the `Reference data:` source, version and fetch time; `agentcompass providers` includes it as `public_data`; `agentcompass data` prints the full status and `agentcompass data --refresh` checks now.
- **Off switch**: `AGENTCOMPASS_PUBLIC_DATA=off` (`0`, `false`, `no` also work), or `{"enabled": false}` in `~/.agent-compass/public-data-config.json`; the environment wins. Off means no network call and only the shipped copy is read. The same file accepts `interval_hours` and `url` (a mirror); `AGENTCOMPASS_PUBLIC_DATA_INTERVAL_HOURS` and `AGENTCOMPASS_PUBLIC_DATA_URL` override them.
- **Maintainers**: after editing any of the three files run `python -X utf8 skills/agent-compass/scripts/public_data.py manifest --write` and commit the manifest with them (`published_at` moves only when content changes). `manifest --check` and the test suite fail on a stale manifest. A breaking data change bumps `SCHEMA_VERSION` in `public_data.py` and the manifest together, so older installs keep their last compatible data.
