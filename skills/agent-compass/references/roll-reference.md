# roll reference

Full mechanics behind `roll --task`; the SKILL.md default path needs none of this unless a result says otherwise.

Never manually choose between Paseo `create_agent`, Claude `Agent`, Codex `spawn_agent`, Antigravity `invoke_subagent`, `run_dot_task.py`, or raw CLI. Pass the task inline with `--task "<text>"`, or write a Markdown brief and pass `--brief <task.md>`; `--hours <n>` is optional (without it the classifier estimates hours in the same call; the source and confidence are reported under `assumptions`):

```bash
python -X utf8 ~/.codex/skills/agent-compass/scripts/route_judge.py roll --task "<text>"
```

`roll` automatically detects the caller's host session (`PASEO_AGENT_ID`, `ANTIGRAVITY_CONVERSATION_ID`, `CLAUDE_SESSION_ID` / `CLAUDECODE`, `CODEX_THREAD_ID`, `PI_SESSION_ID`, `OPENCODE_SESSION_ID`), classifies the brief, allocates and records the worker, and chooses the best dispatch surface (`paseo`, `dot`, `cli`, `chatgpt-web`, `native`, or `self`). Every dispatch requires a real addressable parent session ID, host, and message address; missing information stops dispatch before classification or launch (`--dry-run` diagnostics remain available).

Inspect `launched`, `dispatch_mode`, and `caller_action` in `roll`'s JSON output:
- **`launched: true` (`caller_action: "already_launched_do_not_respawn"`)**: `roll` has **already launched** the worker (`dispatch_mode: "paseo"`, `"cli"`, or `"dot"`). **Do NOT call `create_agent`, `Agent`, `spawn_agent`, or `invoke_subagent` again.** Follow `next`: Paseo registers a bounded local notification watcher (PID, deadline and durable state in `worker.completion_notification`) that sends the parent a finish/error/permission notice with a pointer to the worker's response, including for workers without messaging tools. If no active watcher is confirmed, use `worker.continuation_argv` / `get_agent_activity` to collect the result. CLI has no automatic callback: the caller must watch `worker.output` / `worker.stderr` and own the process until exit or cancellation. For Dot use `continuation_argv`.
- **`launched: false` and `dispatch_mode: "self"` (`caller_action: "execute_self"`)**: Execute the task directly in your current session; do not spawn a subagent.
- **`launched: false` and `dispatch_mode: "native"` (`caller_action: "launch_with_native_host_tool"`)**: Returned when `--parent-host native --parent-id <session-id> --parent-address <inbox-address>` is passed (or when neither Paseo nor a local CLI is available). Pass the **contents** of `worker_prompt_file` to the native host subagent tool using `route.bundle` and preserving the live preamble. Do not switch to Paseo or pass the filename as the prompt.

The source brief remains unchanged. The router appends the [worker communication contract](worker-communication.md) and actual parent address to `worker_prompt_file` on every route. `--hours` is optional and positive when given. Follow the returned continuation command for Paseo/Dot; CLI worker PIDs belong to the caller and must be waited for or stopped. The [parent-address gate contract](parent-address-gate.md) describes enforcement and host coverage.

Grok Bot uses the distinct provider/account/model selector `grok-bot` and joins
automatic routing when ready and enabled; no override flags are needed.
Follow [Grok Bot setup and usage](grok-bot.md) for the pinned upstream
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
- `--fast`: prefer Codex fast when quota is plentiful or during a pre-reset sprint (public subscription default: `2.5x`; explicit local calibration is retained). An explicit Claude provider/model override plus `--fast` opts into paid usage credits on supported Opus models; automatic quota/reset routing never enables paid Claude fast. See [fast contracts and verification](fast-modes.md).
- `--title`, `--workspace <id>`, `--wait <seconds>` (block for the result instead of returning at once).
- The user named the model/effort in this request: add `--provider <p> --account <a> --model <m> --effort <e> --basis "<exact user quote>"`.

Classifier input: the classifier reads the complete task text (never a head/tail view or summary) and the same call returns the hours estimate and the urgency the text itself states. `--hours` always wins; cost is then computed from hours and real rates/quota. Urgency is reported (compact `assumptions` when high, `details` always) and never changes owner, effort or fast mode; `--fast` or an explicit fast request in the text does. Transport: the default agy scorer sends a short task on its command line and a long one on stdin (stream-json); Claude/Codex scorers use stdin and Jev/OpenAI-compatible use HTTP. If no scorer can take the whole text (agy stdin failure with no fallback, HTTP 413), `roll` returns an `error` with the cause instead of cutting the text or guessing from keywords. The classification is cached 24 h per exact text + scorer identity + rubric/schema; the route (quota, policy, reservations) is always recomputed live.

Edit the brief freely before rolling. Rolling the same task again while its Paseo dispatch is still live returns that dispatch as `pending` with `next_argv` (no second classification, no second launch). When `launched` is false, `next` says why: a user decision, no fitting worker, or neither Paseo nor a CLI for the routed provider (then start it natively with exactly `route.bundle` and the contents of `worker_prompt_file` as its prompt).

