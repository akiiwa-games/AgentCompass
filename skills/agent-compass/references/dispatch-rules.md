# Rules for a dispatching session

The global rule block AgentCompass installs is a short pointer to this skill. Read this file before you launch, continue or replace a worker, an advisor or any sub-agent. Each rule names the reference that holds its details; this file does not repeat them.

## 1. One entry, then do exactly `next_action`

1. Launch through `agentcompass roll` only, one brief per independent task ([SKILL.md](../SKILL.md#dispatch)). Never call Paseo `create_agent`, a host's `Agent`/`Task`, `spawn_agent`, `invoke_subagent`, `run_dot_task.py` or a host's worker launcher (`paseo run`, `orca orchestration worker-start`) yourself. A model CLI asked for one answer from a shell (`claude -p`, `codex exec`, `agy -p`, `gemini -p`, `opencode run`, `pi -p`) is a quick call, not a dispatch, and the gate does not read it as a launch; work you hand to a worker still goes through `roll`. A host's built-in tool agent is tool use, not a dispatch: in Claude Code `web-fetch` (reads a web page), `statusline-setup` (what `/statusline` runs) and `claude-code-guide` (answers questions about Claude Code). Codex and Antigravity have none; `Explore`, `Plan`, `general-purpose` and every other agent type are dispatches. The gate lets the built-in tool agents through unless a project or user agent definition takes one of their names. Antigravity's `browser_subagent` and each host's own web tools are ordinary tools and were never part of this rule.
2. Read `status` and do exactly `next_action`; `launched` means the worker runs, so start nothing else for that task. When `next_action` says completion arrives as a notification, finish any independent work and end your turn so the notification wakes you — never sleep or poll (`paseo ls`, `get_agent_status`, result files) in a shell command. Result variants: [roll reference](roll-reference.md).
3. A delegated worker (`AGENTCOMPASS_WORKER=1`, or a `Parent session agent ID:` contract at the end of its brief) does its assignment itself and does not delegate again unless the brief appoints it coordinator.

## 2. Model and effort belong to the router

1. Never pick a model or effort, and never raise the effort the router returned.
2. Only when the user named a model or effort in this request: add `--provider <p> --account <a> --model <m> --effort <e> --basis "<the user's words>"` to `roll` ([router internals, record an override](router-internals.md#b-record-an-explicit-user-modeleffort-override)).
3. When an account or model becomes unavailable (subscription expired, account removed, quota exhausted, `named_model_unavailable`), roll again. Do not substitute a model that looks similar.

## 3. Continue a worker, or replace it

Worker notifications carry the continuation verdict; host fields such as `contextWindowUsedTokens` and `contextWindowMaxTokens` are not the rotation measure. The verdict is `same worker` only when the worker is under the rotation threshold **and** nothing advises against a new turn to it. The gate enforces three of those conditions on a parent's new turn to its own worker: the account carries no leftover meter fields, the account's confirmed quota exhaustion is not latched, and no quota window the bundle draws refuses a launch. An account outside automatic routing or a model unchecked since the launch does not stop a new turn: continuing a worker is not choosing its account or model. The verdict adds advice the gate does not enforce: the worker's own last turn did not end in a provider failure, its account is ready, and neither its account, quota pool nor model is held after a provider failure (`turn_refusal.py`). Otherwise the verdict says `roll` and names the reason, or `rotate` at the threshold; a worker the router cannot resolve, or a check that cannot be read in time (such as `carried_context_unavailable` or `session_state_unavailable`), says `unknown`. An `unknown` verdict caused by unavailable telemetry is not by itself a reason to replace a worker; only `roll` or `rotate` are. On `roll` or `rotate`, do not send the follow-up to that worker; a parent that sends it anyway is admitted by the gate whenever the three enforced conditions pass. The verdict's own registry and quota reads are read-only and bounded (it records no hold and waits on no writer); it loads the fleet through `load_accounts`, which may update host policy and holds as it does for every other caller.

1. Follow-up work should go to the same worker (Paseo `send_agent_prompt`, a host's `SendMessage`, or `agentcompass roll --resume <id>` / matching `--workspace` for a new brief) while its verdict says `same worker` (or `unknown` only from unavailable telemetry): its accumulated uncompressed context is under the configured context rotation threshold (`rotate_after_carried_tokens`, set in the tray). Count `compacted_tokens + context_tokens`: the context before every earlier compaction plus the current window, never the current window alone.
2. At or above `rotate_after_carried_tokens`, write the progress and the remaining work into a handoff brief and run `agentcompass roll --brief <handoff.md>` for a fresh worker. Changing the owner, model or effort also means rolling again.
3. A correction or redesign of a worker's own task is the same task, not a new one. When the result is rejected or the design changes (wrong approach, a constraint was missed, scope moves), send the corrected task to that worker instead of cancelling it and rolling a fresh one. Its context about the code and the earlier attempt is what the redo needs. Replace it only when point 2 applies or its context window cannot hold the rest of the work. A different kind of task with little shared context still gets its own brief and its own roll.
4. The limit applies to starting a new turn. A parent may still send control messages (steer, cut scope, answer a question, wrap up and hand off) to its own worker while that worker is running. An idle worker, or one whose state or ownership is unknown, gets no exception: collect its output, `cancel_agent` if needed, and roll the handoff ([gate contract](parent-address-gate.md#disabled-target-quota-and-context-rotation-gate-contract)).

## 4. Permission mode on every launch

`roll` sets the mode for the launches it performs. When you start a worker yourself (`status: native`, or a native launch recorded with `record`), pass the mode explicitly; a provider default can leave a worker waiting on a permission prompt while its status still says running.

- First choice: `selected_bundle.modeId` (or `route_bundle.modeId`) from the router.
- Otherwise by provider: Codex `full-access`, Claude `bypassPermissions` (`bypass` is not a valid value), Antigravity `full-access`, OpenCode `build`; Pi and Grok take no mode. Full table: [router internals, launch contract](router-internals.md#3-worker-launch-communication--permission-contract).
- Narrow the mode for read-only roles (reviewer, advisor); never widen it.
- After launch, read the agent's status once to confirm the mode took effect and it is not waiting on a permission.

## 5. Gate and audit

1. When the gate hook is installed (`agentcompass gate-hook`, wired into each host's `PreToolUse` / `tool_call`), it blocks a sub-agent launch that has no `plan`, `select` or `record` entry in `~/.agent-compass/router-ledger.jsonl` from the last 15 minutes. Run `roll` (or `record`, below) first. `agentcompass uninstall` removes the hook.
2. The daily audit compares sub-agent launches with ledger records and reviews every `--keep-whole` reason.
3. Route only sub-agent, worker and advisor sessions. A one-shot, tool-less classification or structured-JSON call made inside a business or evaluation script is not a dispatch: do not write `record` entries for it.
4. Exhausted quota windows block new launches, and an unchecked model starts only on a route the user named; exhausted windows and the rotation limit block a parent's new turn to its own idle worker, never its control messages to its own running worker. A message to an agent the caller does not own is communication and is not checked ([gate contract](parent-address-gate.md)).
5. An account outside host policy's `allowed_accounts` and a model unchecked in model management are out of automatic routing: no plan, fallback, automatic resume, judge or scorer chooses them. A route the user names in this request (`roll --provider/--account/--model --basis`, `record --constraint user_override`) still dispatches on them, and the decision carries a warning that names the switch; `roll --resume <agent_id>` on such a worker proceeds with the same warning. A `hard_constraint` record does not override them: a project rule is shared between machines, the switches are this machine's.

## 6. A launch the router cannot perform

Some launches must go through the host's own sub-agent tool, for example a review agent defined in a project's `.claude/agents/` that a commit gate counts by agent type. Record the forced route first, then launch natively with the same model and an explicit permission mode:

```bash
agentcompass record --constraint hard_constraint --provider <p> --account <a> --model <m> --effort <e> --basis "<the project rule or user instruction that forces this route>"
```

Use `--constraint user_override` when the user named the route; only that constraint launches on an account kept out of automatic routing on this host (any other answers `status: blocked`, exit 2, and its `next_action` says why). `record` says in `warnings` what it did not look at or read: each quota window of the named model that was not read, a flag the stage does not take, a name read as another row. `record` writes the current session ID, so the gate matches it even right after another `roll`. Project-specific instructions of this kind (which agent, which model) belong in that project's own `CLAUDE.md` / `AGENTS.md`.

## 7. Parent address and worker messages

1. Every brief carries the real parent session ID, host and message address; without them, do not launch. Never substitute `top-level`, a task ID or the worker's own ID. `roll` fills these in and appends the [worker communication contract](worker-communication.md); a native launch must carry both too ([parent address gate](parent-address-gate.md)).
2. Each dispatch has one report channel, and `roll` writes that channel into the [worker communication contract](worker-communication.md). A dispatch with a completion watcher does not send `[Worker final report]`; the watcher sends one `[AgentCompass worker notification]` that already contains the final reply. A dispatch with no watcher (CLI, Dot, Grok Bot, Herdr, native, Cindy, T3, agy) still sends `[Worker final report]` itself. A question is still a real message (Paseo: `send_agent_prompt` with `background: true, notifyOnFinish: false`) and the turn ends. UI-only question tools (`AskUserQuestion`, `ask_user_question`, `request_user_input`, `request_user_input_async`) never reach the parent.
3. A parent that adds work to, releases or resumes its own worker keeps `notifyOnFinish` true (or omits it). That completion notification is the one end message for the continued turn; the worker does not also send a report.
4. No channel, no parent address, or a failed send: return `BLOCKED / needs_input` with the exact question and the delivery problem through the normal result channel.
5. None of this stops a main session from asking the real user, and it does not replace platform approvals.
