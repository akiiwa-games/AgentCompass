---
name: route-agent-work
description: Choose owner, model and effort, outbound authorization, advisor use, and continuation for delegated work. Reads local usage and effective account policy, profiles the task with the configured System-One scorer, then allocates a compatible worker. Use for independent worker or advisor work; ordinary local execution needs no routing.
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
or Paseo-native support is implied. The service has independent conservative public
capability priors; its underlying model identity and dollar price remain unknown.
Quota ownership, capacity and local ratings come from effective account configuration.
The channel still excludes local-only, visual, GUI and file-transfer work: the Bot
cannot see images, transfer files, or run on this machine. Shared Grok observations retain their freshness; no separate Bot meter is read. Private material needs outbound authorization.
For uncertain delivery, `launched` is `null` and `caller_action` is
`check_delivery_do_not_resend`: run `continuation_argv`, never submit again.

Optional flags, only when they are facts:
- `--file <path>` (repeatable): files Dot needs; Dot's returned files are applied to `--apply-to` (default: cwd). Local workers ignore it.
- `--local-only "<concrete local dependency>"`: keep the work on this machine (local git/daemon/pytest/hardware/intranet/credentials).
- `--fast`: prefer Codex fast when quota is plentiful or during a pre-reset sprint (public subscription default: `2.5x`; explicit local calibration is retained). An explicit Claude provider/model override plus `--fast` opts into paid usage credits on supported Opus models; automatic quota/reset routing never enables paid Claude fast. See [fast contracts and verification](references/fast-modes.md).
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
- `route_judge.py plan` automatically invokes the System-One classifier (inspect `route_judge.py scorer --action show` for the effective scorer and available choices; configure it with `scorer --action set` or AgentCompass settings) to determine the route category (`work_kind`), difficulty floor, and 10D `TaskProfile` (including `divergence` `1..3`) directly from `task.objective` and `task.context`. If a parent agent supplies `work_kind` or `difficulty`, the classifier's judgment overrides them (`caller_classification_ignored`).
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

The sole machine-readable public catalogue is [`references/accounts.json`](references/accounts.json).
Its model rows and `capability_policy` define public capability priors and route scopes.
[`model-capabilities.json`](references/model-capabilities.json) and
[`model-capabilities.md`](references/model-capabilities.md) are generated views; regenerate
with `python -X utf8 skills/route-agent-work/scripts/capability_catalog.py` (`--check` verifies them).

A full `~/.route-manager/accounts.local.json` supplies the machine's fleet and personal
policy; sparse `accounts-override.json` preferences apply after discovery. Local fleet
model rows and route policy also feed judge prompts. Keep personal ratings, route order,
capacity/burn calibration and plan assumptions in this local layer. Keep their explanations
in `~/.route-manager/host-rules/*.md`.

The allocator first checks discovered availability, explicit disablement, host policy,
expiry, required tools and modalities, configured route restrictions and ability floors.
It then scores eligible candidates using the effective scoring weights, model capability
priors, optional personal preference vectors, continuity and available quota evidence.
Public routes have no model order and public scoring gives personal preference vectors
zero weight. A route restricts models only with `restrict_models: true` and a nonempty
`preferred` list. Account `model_defaults[model].disabled` supplies an explicit default
for dynamically discovered bundles; individual saved bundle choices still take precedence.

Generic account slots carry no plan capacity or burn estimate. Unknown quota is distinct
from unlimited access. Configure capacity and any model-hour estimates locally; observed
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
| **`pi`** | Paseo (`provider: "pi"`) | No `modeId` (adapter has fixed tool access and advertises no modes) | Exploration & research-packet harness for GPT models (`launch_model: "openai-codex/<model>"`). Passing `full-access` is rejected by Paseo. |
| **`claude`** | Paseo / Claude CLI | `--mode bypassPermissions` | Use the discovered launcher and configured account policy. |
| **`antigravity-resilient`** | Paseo / `agy` CLI | `modeId: "full-access"` | Use a model and effort advertised by the adapter. |
| **`opencode`** | Paseo (`provider: "opencode"`) | `modeId: "build"` | OpenCode free worker (`deepseek-v4.1-flash-free`, `launch_model: "opencode/deepseek-v4.1-flash-free"`). |
| **`dot`** | [`dot-subagent`](../dot-subagent/SKILL.md) | `run_dot_task.py` | Remote worker (`dot2api`); requires `needs.outbound_ok: true` if sending private material. |
| **`grok-bot`** | [Grok Bot adapter](references/grok-bot.md) | `run_grok_bot.py` | Automatic remote text candidate; no local file transfer, private material requires outbound authorization. |
| **`chatgpt-web`** | [`chatgpt-pro-consult`](../chatgpt-pro-consult/SKILL.md) | Browser bridge | `gpt-6-pro` advisor for `complex_build_plan` and `direction_reasoning`. |

**Gemini web is a local tool, not a worker:** [`gemini-web`](../gemini-web/SKILL.md) drives Route Manager's signed-in Gemini Web Chrome (`gemini-web`, CDP `127.0.0.1:17849`) for Gemini / AI Studio / Flow pages and Gemini Omni 1.1 video. The calling agent runs it directly, with no routing. A worker given such a task needs `needs.local_only: true` with `local_only_reason: "Gemini Web browser on this machine"`.

---

## 4. Reference Files (Internal Router Specs — Parent Agents Need Not Read)

- [`references/accounts.json`](references/accounts.json) — Public model catalogue, conservative priors, route scopes, account slots and bundle definitions.
- [`references/classifier-cases.json`](references/classifier-cases.json) — System-One classifier route definitions and calibration cases (loaded automatically by `route_judge.py`).
- [`references/model-capabilities.md`](references/model-capabilities.md) — Generated catalogue view and prior limitations.
- [`references/model-capabilities.json`](references/model-capabilities.json) — Generated policy/model view; runtime derives its policy from effective accounts.
- [`references/dispatch-lifecycle.md`](references/dispatch-lifecycle.md) — Reservation state machine (`planned` -> `started` -> `completed`) and in-flight quota deduction.
- [`references/judge-workflow.md`](references/judge-workflow.md) — System-One `TaskProfile` schema (`score`) and low-effort judge stages (`delegate`, `select`).
- [`references/effort-matching.md`](references/effort-matching.md) — Task demand (depth x property difficulty) vs model ability x per-model effort curve, and the ledger `decision_snapshot` the tray replays.
- [`references/provider-usage.md`](references/provider-usage.md) — Live usage snapshot precedence, `capacity.json` overlay, host policy, and temporary overrides.
- [`scripts/route_judge.py`](scripts/route_judge.py) · [`scripts/allocator.py`](scripts/allocator.py) · [`scripts/host_policy.py`](scripts/host_policy.py) · [`scripts/check_router_contract.py`](scripts/check_router_contract.py)
