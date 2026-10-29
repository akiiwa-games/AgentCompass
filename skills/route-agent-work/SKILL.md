---
name: route-agent-work
description: Choose owner, model and effort, outbound authorization, advisor use, and continuation for delegated work. Reads local subscription usage, profiles the task automatically with a System-One classifier (Gemini 3.8 Flash low via agy, deterministic fallback), then picks the worker with a deterministic allocator (ability, task fit and capability vectors, minimum-sufficient ability, remaining capacity with reset and expiry, expected consumption, continuity). Prefers the local OpenAI Dot (free, its own computer, code and data handed over) for any work that need not run on this machine, and sends only simple coding-plus-tool-call work to Gemini 3.8 Flash (huge quota, not the preferred coder). Use when a concrete task may benefit from an independent worker or advisor; ordinary local execution needs no routing.
---

# Route Agent Work

Before starting any subagent, worker, or advisor session (Claude `Agent`, Codex `spawn_agent`, Antigravity `invoke_subagent`, Paseo `create_agent`, `run_dot_task.py`, or direct CLI), write the task brief and `roll` it; the router picks and launches the worker.

> **System-Level Skill Loading Requirement (Host Rollout)**: `route-agent-work` and its companion skills (`dot-subagent`, `chatgpt-pro-consult`, `gemini-web`) must be installed at the **system level** via `python -X utf8 setup/sync_host_globals.py` so every session loads them regardless of workspace:
> - `~/.codex/skills/<name>`, `~/.claude/skills/<name>` (`~/.claude-*/skills`), and `~/.pi/agent/skills/<name>` via directory junctions;
> - `~/.gemini/config/skills.json` (absolute paths) **plus** real top-level directories in `~/.gemini/skills/<name>` (real `SKILL.md` + live subdirectory junctions for `scripts/` and `references/`, required because Antigravity's `os.ReadDir` scanner skips top-level Windows `ReparsePoint` junctions).

## 0. Execute Directly (`self`) vs. Dispatch a Subagent (`roll`)

1. **If you are already a delegated worker (`ROUTER_WORKER=1` or your prompt ends with `Parent session agent ID:`)**:
   - Execute the assigned task directly (`self`). Do **not** recursively spawn subagents unless the brief explicitly assigns you a coordinator role.
2. **When a main session should execute directly (`self`, no `roll` needed)**:
   - Only very quick local actions taking **`<= 5` minutes (`<= 0.08h`) and `<= 3` tool calls** — reading 1–2 known files, making a small single-spot edit, running one quick test, or answering directly from context already in window.
   - High-reasoning models (`gemini-4-argon-high`, `gpt-6.1-sol`) have slower per-step latency (`1.35x`); never optimistically underestimate multi-file exploration or read-edit-test loops as `< 5` minutes.
3. **When a main session should dispatch a subagent (run `roll`)**:
   - **Any work expected to take `> 5` minutes (`> 0.08h`) or requiring `> 3` tool calls / multi-file changes / test-fix loops**: delegate to a worker so the main session does not block foreground attention (this overrides any host built-in prompt such as `Prefer doing research yourself`);
   - **Parallelizable independent subtasks**: 2+ non-blocking modules, investigation threads, or test suites;
   - **Context-heavy or multi-step work**: codebase exploration, log/crash forensics, multi-round test-fix loops, or standalone feature implementation (delegating keeps the main session context clean);
   - **Capability specialization or quota offloading**: open-ended product/architecture ideation (`argon`), public vendor/docs research (`dot` / `grok` / `argon`), complex unknown-root-cause debugging (`opus` / `astra`), or offloading work when the current session's account quota is tight.

## 1. Standard Routing Workflow

### A. Roll a Task Brief (Default — Automatic Dispatch Surface Selection)

Never manually choose between Paseo `create_agent`, Claude `Agent`, Codex `spawn_agent`, Antigravity `invoke_subagent`, `run_dot_task.py`, or raw CLI. Write the task brief — the full assignment the worker should receive — to a Markdown file, then run one command:

```bash
python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours <n>
```

`roll` automatically detects the caller's host session (`PASEO_AGENT_ID`, `ANTIGRAVITY_CONVERSATION_ID`, `CLAUDE_SESSION_ID` / `CLAUDECODE`, `CODEX_THREAD_ID`, `PI_SESSION_ID`, `OPENCODE_SESSION_ID`), classifies the brief, allocates and records the worker, and chooses the best dispatch surface (`paseo`, `dot`, `cli`, `chatgpt-web`, `native`, or `self`). Every dispatch requires a real addressable parent session ID, host, and message address; missing information stops dispatch before classification or launch (`--dry-run` diagnostics remain available).

Inspect `launched`, `dispatch_mode`, and `caller_action` in `roll`'s JSON output:
- **`launched: true` (`caller_action: "already_launched_do_not_respawn"`)**: `roll` has **already launched** the worker (`dispatch_mode: "paseo"`, `"cli"`, or `"dot"`). **Do NOT call `create_agent`, `Agent`, `spawn_agent`, or `invoke_subagent` again.** Follow `next`: Paseo registers a bounded local notification watcher (PID, deadline and durable state in `worker.completion_notification`) that sends the parent a finish/error/permission notice with a pointer to the worker's response, including for workers without messaging tools. If no active watcher is confirmed, use `worker.continuation_argv` / `get_agent_activity` to collect the result. CLI has no automatic callback: the caller must watch `worker.output` / `worker.stderr` and own the process until exit or cancellation. For Dot use `continuation_argv`.
- **`launched: false` and `dispatch_mode: "self"` (`caller_action: "execute_self"`)**: Execute the task directly in your current session; do not spawn a subagent.
- **`launched: false` and `dispatch_mode: "native"` (`caller_action: "launch_with_native_host_tool"`)**: Returned when `--parent-host native --parent-id <session-id> --parent-address <inbox-address>` is passed (or when neither Paseo nor a local CLI is available). Pass the **contents** of `worker_prompt_file` to the native host subagent tool using `route.bundle` and preserving the live preamble. Do not switch to Paseo or pass the filename as the prompt.

The source brief remains unchanged. The router appends the [worker communication contract](references/worker-communication.md) and actual parent address to `worker_prompt_file` on every route. `--hours` is required and positive. Follow the returned continuation command for Paseo/Dot; CLI worker PIDs belong to the caller and must be waited for or stopped. The [parent-address gate contract](references/parent-address-gate.md) describes enforcement and host coverage.

Grok Bot uses the distinct provider/account/model selector `grok-bot` and joins
automatic routing when ready and enabled; no override flags are needed.
Follow [Grok Bot setup and usage](references/grok-bot.md) for the pinned upstream
CLI and `roll` command. It runs remotely and accepts text; no local file access
or Paseo-native support is implied. Its service-level score is a provisional
generic text baseline, not a model benchmark; native weekly observations retain
freshness while capacity, burn and dollar cost stay unknown. Local-only, visual
and file-transfer tasks exclude it. Private material needs outbound authorization.
For uncertain delivery, `launched` is `null` and `caller_action` is
`check_delivery_do_not_resend`: run `continuation_argv`, never submit again.

Optional flags, only when they are facts:
- `--file <path>` (repeatable): files Dot needs; Dot's returned files are applied to `--apply-to` (default: cwd). Local workers ignore it.
- `--local-only "<concrete local dependency>"`: keep the work on this machine (local git/daemon/pytest/hardware/intranet/credentials).
- `--fast`: prefer fast mode (`fast_mode: true` on Codex bundles at `2.0x` quota burn) when quota is plentiful (`>= 45%` remaining after `2.0x` burn with low shortage risk) or during a pre-reset sprint.
- `--title`, `--workspace <id>`, `--wait <seconds>` (block for the result instead of returning at once).
- The user named the model/effort in this request: add `--provider <p> --account <a> --model <m> --effort <e> --basis "<exact user quote>"`.

Edit the brief freely before rolling. Rolling the same brief again while its Paseo worker is still live is refused instead of launching a second one. When `launched` is false, `next` says why: a user decision, no fitting worker, or neither Paseo nor a CLI for the routed provider (then start it natively with exactly `route.bundle` and the contents of `worker_prompt_file` as its prompt).

The stages below are what `roll` runs internally; call them directly only for diagnosis.

### A2. Plan Only (Diagnosis)

Write a `task.json` file (in a scratch/temp path) describing **only the factual task and runtime constraints**, then run `plan`:

```bash
python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py plan --input <task.json>
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
- `route_judge.py plan` automatically invokes the System-One classifier (`gemini-3.8-flash-low` via `agy` by default; the user may select TypeSafe Jev, Claude Fable/Sonnet low, Codex Sol low, a custom OpenAI-compatible API or the offline heuristic with `route_judge.py scorer --action set` or the AgentCompass settings page, stored in `~/.route-manager/scorer-config.json`) to determine the route category (`work_kind`), difficulty floor, and 10D `TaskProfile` (including `divergence` `1..3`) directly from `task.objective` and `task.context`. If a parent agent supplies `work_kind` or `difficulty`, the classifier's judgment overrides them (`caller_classification_ignored`).
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
python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py record \
  --input <task.json> --constraint user_override --basis "<exact user quote>" \
  --provider <provider> --account <account> --model <model> --effort <effort>
```

### C. Close Out the Dispatch Lifecycle

```bash
python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py dispatch-state \
  --id <dispatch_id> --state started --worker-id <worker_id>

python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py dispatch-state \
  --id <dispatch_id> --state completed
```

---

## 2. How the Allocator Scores Candidates

Provider availability is discovered automatically from native sign-ins, Paseo launchers,
local CC Switch and local Magpie. Account disablement, removed profiles and confirmed
credential rejection automatically pause new allocations; recovery takes effect on
the next allocation. Run `route_judge.py providers` to refresh and inspect the inventory.
New bound Claude/Codex launchers inherit the existing model policy; saved profiles
without a launcher are listed without changing the user's active account. See
[provider discovery and quota sources](references/provider-usage.md).

Defined in [`scripts/allocator.py`](scripts/allocator.py) using parameters in [`references/accounts.json`](references/accounts.json) and routes in [`references/model-capabilities.json`](references/model-capabilities.json):

1. **Hard Gates**:
   - Discovered account/launcher availability (`verified` is derived locally), explicit host policy ([`scripts/host_policy.py`](scripts/host_policy.py)), and known subscription expiry (`expires_at`).
   - Modality gates: `needs.visual` excludes text-only models (`grok-4.7`, `dot`); `local_only: true` (trusted from the classifier, or supplied by the caller with a valid `local_only_reason`) excludes `dot`.
   - Conditional bundles (`when.max_reasoning_depth: 1`): `sol_medium` and `pi_sol_medium` are offered only on profiled mechanical tasks (`reasoning_depth == 1`).
   - Ability floor: `ability_value = tier + effort_ability >= ability_floor[difficulty] + (0.3 if critical else 0.0)`.
   - Window reserve & in-flight reservations: Excludes windows whose remaining capacity after active in-flight dispatches falls below `hard_floor_pct` / `reserve_pct`.
2. **Minimum-Sufficient Ability & Effort Cap**:
   - Ability normally saturates at `floor + 0.6`; non-precision frontend work saturates at `floor + 0.3` so sufficient reasoning leaves visual/design fit to decide UI quality. On minimum-sufficient tasks meeting the floor is full ability (so `Flash 3.8` at the routine floor 3.0 is not handicapped against 3.4+ models).
   - On standard `routine` and `hard` non-critical tasks (`reasoning_depth <= 2`), effort above `high` earns zero extra ability score (`effort_capped: true`). Coupled/frontier, depth >= 3, critical tasks and depth >= 2/divergence >= 2/hallucination_risk >= 3 unlock higher effort.
   - High precision means unlocked frontier/critical work, or hallucination_risk >= 3 with divergence >= 2 or depth >= 3/code >= 3. Ability is measured above the critical-adjusted gate (minimum-sufficient work still treats meeting the floor as full ability). Positive effort ability gains above High earn an uncapped `0.14 * gain` quality bonus; effort burn and opportunity cost still discourage higher efforts under scarcity. Ordinary coupled work receives no precision bonus.
   - On minimum-sufficient tasks (`routine`/`hard`, non-critical, `code_complexity < 3`, `divergence < 3`), models above `floor + 0.3` pay an `overqualification` penalty of `0.08 * excess * usd_per_hour / $10` (price factor capped at `overqualification_price_cap` 3.0, so `Astra` at $22.6/h pays 2.26x; GUI work keeps the $10 cap). GUI and frontend implementation are exempt above `reasoning_depth` 1 because visual quality benefits from higher tiers.
3. **6D Capability Vector Fit (`vector_fit`) & 3D Subjective Preference Fit (`subjective_fit`)**:
   - Keeps the canonical 15 `work_kind` routes unchanged while blending `17%` route preference (`route_fit`), `34%` 6D capability satisfaction (`vector_fit`: `reasoning`, `coding`, `exploration`, `anti_hallucination`, `visual`, `gui`), and `49%` 3D subjective model inclination (`subjective_fit`: `divergence`, `global_rigor`, `module_precision`):
     - **`gemini-4-argon-high`** (`divergence: 0.96`): Prioritized when `profile.divergence == 3` (scheme formulation, creative research, open-ended ideation, architectural innovation).
     - **`claude-opus-5-5`** (`global_rigor: 0.98`, `module_precision: 0.92`): Prioritized when `code_complexity == 3` or `context_scale == 3` with convergent rigor (`divergence <= 2` — architectural landing, holistic cross-module synthesis, subtle edge cases).
     - **`gpt-6.1-sol`** (`module_precision: 0.95`, `global_rigor: 0.25`): Prioritized for bounded single-module implementation (`context_scale == 1`, `code_complexity in 1..2`, `reasoning_depth == 2`, `divergence == 1`), and heavily penalized on cross-module / repo-wide (`context_scale == 3`) tasks due to limited global vision.
     - Weights sum to 1 and step smoothly (`convergent_weights`): production code at depth <= 2 is `module_precision` 1.0 at scale 1 and 0.75/0.25 at scale 2; read-only review/analysis is 0.65/0.35 and 0.5/0.5; `global_rigor >= 0.8` only at depth >= 3 with scope, scale 3 or code 3; `divergence == 2` (and `complex_build_plan`) adds a 0.4 divergence share to that blend.
     - Routine convergent tasks (depth 1, scale <= 2, code <= 2) use route/vector fit without subjective weights, allowing Flash to absorb routine volume. Frontend implementation at divergence < 3 uses `global_rigor` 0.65 / `divergence` 0.35, preserving Opus > Argon > Astra under healthy quota. Route preference is capped only at divergence 3, scale 3 or code 3; frontend keeps its intentional route ordering.
4. **Opportunity Cost, Pre-Reset Surplus Bonus & Historical Context / Compaction Rotation**:
   - Prices each task in expected API-equivalent USD (`usd_per_hour * hours * duration_multiplier * burn / 0.70`, where slower models like `gemini-4-argon-high` and `gpt-6.1-sol` carry `duration_multiplier = 1.35`) against each window's remaining USD and demand forecast (`shortage_probability * shortage_loss_per_usd * expected_usd`). Every subscription window, Antigravity's included (`capacity_usd` gemini-weekly $625), is valued in USD; only a window with no USD capacity at all falls back to worker-hours at the bundle's own burn. Default baseline task durations when omitted are `{"routine": 0.4, "hard": 1.0, "coupled": 2.0, "frontier": 3.5}` hours.
   - Value and cost stay on one scale. `harness_factor` (default `1.0`; Antigravity Claude `0.9`) multiplies weighted fit and ability only. `scoring.opportunity_exchange` is the only rate between that value and quota: `score_per_displaced_usd` `0.012` (about `$83.33` of displaced USD per score point) and `score_per_displaced_worker_hour` `0.06` when a window has no USD capacity. `exchange_rate` reads those fields; top-level `shortage_loss_per_usd` and `shortage_loss_per_worker_hour` are a deprecated fallback, and a conflicting old key is ignored with a plan warning. A multi-pool bundle is gated by its tightest window; opportunity cost sums every window, and the binding window is the one with the highest cost. Codex versus Pi remains `harness_order` (sibling-only, `0.03` per place, best sibling unchanged). See [judge-workflow.md](references/judge-workflow.md).
   - Weekly windows behind their linear schedule floor observed demand at half the linear pace, preventing a quiet day from erasing future demand. A smooth scarcity charge (up to 0.06, weighted by shortage probability) grows as usable quota after the task falls below 45%; short windows receive no scarcity charge. Quota at 60% or 95% remains unaffected for small tasks.
   - Within `reset_horizon_hours` (`36h`) of a weekly window reset, surplus quota above expected demand (`usable - step - mean`, saturating at `reset_surplus_ref_pct = 15%` weighted by `1 - shortage_probability`) earns an expiring-reset bonus up to `reset_bonus_max` (`+0.20`) and proportionally discounts `overqualification`.
   - **Fast Mode (`fast_mode` at `2.0x` burn)**: For `fast_capable` Codex bundles (`gpt-6.1-sol`, `gpt-6-astra`), the allocator automatically enables `selected_bundle.fast = true` (dispatched as `settings: { features: { "fast_mode": true } }` in Paseo or `-c features.fast_mode=true` on Codex CLI, reserving `2.0x` expected USD) when all windows stay above safety margins under `2.0x` burn and either:
     1. **Fast-turnaround task + plentiful quota (`fast_requested_plentiful_quota`)**: `--fast`, `needs.fast: true`, or fast-turnaround wording in `objective`, with `>= 45%` usable quota remaining after `2.0x` burn, `shortage_probability <= 0.20`, and extra opportunity cost `<= 0.015`.
     2. **Pre-reset sprint (`pre_reset_sprint`)**: Within `reset_horizon_hours` (`36h`) of weekly reset with `shortage_probability <= 0.20` and substantial expiring surplus after `2.0x` burn (`sunk_surplus >= 0.25` automatically, or `>= 0.05` when fast turnaround is requested).
   - High-coupling chains stay with their current owner (`+0.30` continuity bonus) only while **historical uncompressed context** (`historical_context_tokens = compacted_tokens + context_tokens`, summing every pre-compaction context window replaced across the session's history plus the current active window — **never** just the post-compaction compressed window size) `< 2,000,000` tokens (`rotate_after_carried_tokens`). Once a worker reaches `2M` historical context tokens (regardless of compaction count), `router_gate_hook` blocks new turns on the old worker and `allocator.plan` sets `action = "rotate"` to dispatch a fresh worker with a handoff packet. The parent may still message its own currently running worker to steer or wrap up and produce that handoff; idle/unknown state receives no exemption.

---

## 3. Worker Launch, Communication & Permission Contract

**Questions to the main session must be sent as real messages.** A worker needing clarification/decisions must directly message its parent, never call `request_user_input_async`, `AskUserQuestion`, `ask_user_question`, `request_user_input` or similar UI-only tools that cannot communicate with the parent. In Paseo use `send_agent_prompt` to the actual parent ID with `background: true, notifyOnFinish: false`, then end the turn rather than synchronously waiting on the parent. If messaging is unavailable or fails, return `BLOCKED / needs_input` through the existing result channel and end the turn; the caller must collect/relay it. See the [communication contract](references/worker-communication.md) injected by `roll`. This does not restrict the main session asking the real user or bypass required approvals. For native/manual launches, include this contract and the actual parent ID, host and message address too. Missing parent information prohibits launch; the result-channel fallback applies only to delivery failures after launch.

**Workers with messaging tools must send a final report to the actual parent before ending the turn**, for done, failed and blocked outcomes. In Paseo send `[Worker final report]` through `send_agent_prompt` with `background: true, notifyOnFinish: false` (use equivalent nonblocking delivery if the host schema omits `background`), and check the send result. Include the full report or a concise result with an accessible location for the full report; keep the complete report in the final reply too. If sending fails, state the delivery failure in the final reply. Workers without messaging tools, such as agy, put the full report in the final reply and rely on the router's short `[Route Manager worker notification]` and response pointer; CLI callers must collect the output themselves. The state notice does not replace a messaging-capable worker's report. See the injected [final-report contract](references/worker-communication.md#final-reports).

`notifyOnFinish: false` above applies only to worker-to-parent questions and final reports. When the main session uses `send_agent_prompt` to add work, give clearance or resume its own worker, it must set `notifyOnFinish: true` (or omit it): the completion notification and final reply are the result channel for workers without messaging tools, such as agy. `router_gate_hook` blocks an explicit `false` from a parent to its own worker.

Disabling an account/bundle blocks new launches and new turns, while the parent can still control its own currently running worker (steer, scope cut, answering questions, wrap-up/handoff). The same exception applies at the 2M historical-context limit. Ownership must be proved by consistent locally persisted parent labels and running state by the local Paseo record; missing/unknown/idle state grants no exception, even for a prompt requesting only a commit or report. Collect existing output, cancel if necessary and roll a handoff to a fresh worker, or re-enable a disabled account in AgentCompass settings. Quota and completion-notification checks still apply. See the [gate contract and local-state limitation](references/worker-communication.md#disabled-target-and-context-rotation-gate-contract).

Paseo SDK creation does not implement MCP's `notifyOnFinish`. The router therefore starts its own notifier before SDK creation, deduplicated by dispatch ID and stable message IDs. Permission notices leave observation active; final delivery, parent closure or the seven-day watch deadline ends the process. A two-minute delivery grace follows the deadline; failures remain in the durable receipt. A stopped watcher can be recovered by observing the dispatch; recovery never creates another worker. No daemon settings or installed Paseo files are modified.

If notifier startup fails, the bridge does not create a worker whose result could be lost. Repair the local SDK dependencies or notification state directory and roll again. This startup check applies only to new creation; existing workers remain observable and their replies can be collected manually.

Always pass the explicit permission mode from `selected_bundle.modeId` (never rely on CLI defaults):

| Provider | Launch Surface | Required Permission Flag / Mode | Notes |
| :--- | :--- | :--- | :--- |
| **`codex`** | Paseo / Codex CLI | `--mode full-access` | Engineering & GUI (`cua_repl`) harness for `gpt-6-astra` and `gpt-6.1-sol`. |
| **`pi`** | Paseo (`provider: "pi"`) | `modeId: "full-access"` | Exploration & research-packet harness for GPT models (`launch_model: "openai-codex/<model>"`). |
| **`claude`** / **`claude-2`** | Paseo / Claude CLI | `--mode bypassPermissions` | Max 20x (`claude`) and Pro (`claude-2`, Sonnet-only, `HOME=~/.claude-2`, guarded against 5h wall). |
| **`antigravity-resilient`** | Paseo / `agy` CLI | `modeId: "full-access"` | `gemini-3.8-flash-high` (routine sink) and `gemini-4-argon-high` (frontier workhorse). |
| **`opencode`** | Paseo (`provider: "opencode"`) | `modeId: "build"` | OpenCode free worker (`deepseek-v4.1-flash-free`, `launch_model: "opencode/deepseek-v4.1-flash-free"`). |
| **`dot`** | [`dot-subagent`](../dot-subagent/SKILL.md) | `run_dot_task.py` | Free remote worker (`dot2api`); requires `needs.outbound_ok: true` if sending private material. |
| **`grok-bot`** | [Grok Bot adapter](references/grok-bot.md) | `run_grok_bot.py` | Automatic remote text candidate; no local file transfer, private material requires outbound authorization. |
| **`chatgpt-web`** | [`chatgpt-pro-consult`](../chatgpt-pro-consult/SKILL.md) | Browser bridge | `gpt-6-pro` advisor for `complex_build_plan` and `direction_reasoning`. |

**Gemini web is a local tool, not a worker:** [`gemini-web`](../gemini-web/SKILL.md) drives Route Manager's signed-in Gemini Web Chrome (`gemini-web`, CDP `127.0.0.1:17849`) for Gemini / AI Studio / Flow pages and Gemini Omni 1.1 video. The calling agent runs it directly, with no routing. A worker given such a task needs `needs.local_only: true` with `local_only_reason: "Gemini Web browser on this machine"`.

---

## 4. Reference Files (Internal Router Specs — Parent Agents Need Not Read)

- [`references/accounts.json`](references/accounts.json) — Account windows, USD ceilings, 6D capability vectors, tiers, effort curves, and bundle definitions.
- [`references/classifier-cases.json`](references/classifier-cases.json) — System-One classifier route definitions and calibration cases (loaded automatically by `route_judge.py`).
- [`references/model-capabilities.md`](references/model-capabilities.md) — Zero-to-frontier-ceiling normalization formula, empirical benchmark matrix (`2026-10-19`), and effort curves.
- [`references/model-capabilities.json`](references/model-capabilities.json) — Route definitions, modality defaults, and concise model summaries for judge prompts.
- [`references/dispatch-lifecycle.md`](references/dispatch-lifecycle.md) — Reservation state machine (`planned` -> `started` -> `completed`) and in-flight quota deduction.
- [`references/judge-workflow.md`](references/judge-workflow.md) — System-One `TaskProfile` schema (`score`) and low-effort judge stages (`delegate`, `select`).
- [`references/provider-usage.md`](references/provider-usage.md) — Live usage snapshot precedence, `capacity.json` overlay, host policy, and temporary overrides.
- [`scripts/route_judge.py`](scripts/route_judge.py) · [`scripts/allocator.py`](scripts/allocator.py) · [`scripts/host_policy.py`](scripts/host_policy.py) · [`scripts/check_router_contract.py`](scripts/check_router_contract.py)
