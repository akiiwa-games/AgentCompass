# Rules for a dispatching session

The global rule block AgentCompass installs is a short pointer to this skill. Read this file before you launch, continue or replace a worker, an advisor or any sub-agent. Each rule names the reference that holds its details; this file does not repeat them.

## 1. One entry, then do exactly `next_action`

1. Launch through `agentcompass roll` only, one brief per independent task ([SKILL.md](../SKILL.md#dispatch)). Never call Paseo `create_agent`, a host's `Agent`/`Task` (including a built-in Explore), `spawn_agent`, `invoke_subagent`, `run_dot_task.py` or a worker CLI yourself.
2. Read `status` and do exactly `next_action`; `launched` means the worker runs, so start nothing else for that task. Result variants: [roll reference](roll-reference.md).
3. A delegated worker (`ROUTER_WORKER=1`, or a `Parent session agent ID:` contract at the end of its brief) does its assignment itself and does not delegate again unless the brief appoints it coordinator.

## 2. Model and effort belong to the router

1. Never pick a model or effort, and never raise the effort the router returned.
2. Only when the user named a model or effort in this request: add `--provider <p> --account <a> --model <m> --effort <e> --basis "<the user's words>"` to `roll` ([router internals, record an override](router-internals.md#b-record-an-explicit-user-modeleffort-override)).
3. When an account or model becomes unavailable (subscription expired, account removed, quota exhausted, `named_model_unavailable`), roll again. Do not substitute a model that looks similar.

## 3. Continue a worker, or replace it

1. Follow-up work may go to the same worker (Paseo `send_agent_prompt`, a host's `SendMessage`) only while its accumulated uncompressed context is under the configured context rotation threshold (`rotate_after_carried_tokens`, set in the tray). Count `compacted_tokens + context_tokens`: the context before every earlier compaction plus the current window, never the current window alone.
2. At or above `rotate_after_carried_tokens`, write the progress and the remaining work into a handoff brief and run `agentcompass roll --brief <handoff.md>` for a fresh worker. Changing the owner, model or effort also means rolling again.
3. The limit applies to starting a new turn. A parent may still send control messages (steer, cut scope, answer a question, wrap up and hand off) to its own worker while that worker is running. An idle worker, or one whose state or ownership is unknown, gets no exception: collect its output, `cancel_agent` if needed, and roll the handoff ([gate contract](parent-address-gate.md#disabled-target-quota-and-context-rotation-gate-contract)).

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
4. Disabled accounts or efforts and exhausted quota windows block new launches and new turns, never a parent's control messages to its own running worker ([gate contract](parent-address-gate.md)). Re-enabling a disabled account is the user's choice in AgentCompass settings.

## 6. A launch the router cannot perform

Some launches must go through the host's own sub-agent tool, for example a review agent defined in a project's `.claude/agents/` that a commit gate counts by agent type. Record the forced route first, then launch natively with the same model and an explicit permission mode:

```bash
agentcompass record --constraint hard_constraint --provider <p> --account <a> --model <m> --effort <e> --basis "<the project rule or user instruction that forces this route>"
```

Use `--constraint user_override` when the user named the route. `record` writes the current session ID, so the gate matches it even right after another `roll`. Project-specific instructions of this kind (which agent, which model) belong in that project's own `CLAUDE.md` / `AGENTS.md`.

## 7. Parent address and worker messages

1. Every brief carries the real parent session ID, host and message address; without them, do not launch. Never substitute `top-level`, a task ID or the worker's own ID. `roll` fills these in and appends the [worker communication contract](worker-communication.md); a native launch must carry both too ([parent address gate](parent-address-gate.md)).
2. A worker asks its parent through a real message (Paseo: `send_agent_prompt` with `background: true, notifyOnFinish: false`) and ends its turn. Before finishing (done, failed or blocked) a worker with a messaging tool sends `[Worker final report]` the same way and keeps the full report in its final reply. UI-only question tools (`AskUserQuestion`, `ask_user_question`, `request_user_input`, `request_user_input_async`) never reach the parent.
3. A parent that adds work to, releases or resumes its own worker keeps `notifyOnFinish` true (or omits it): the completion notification is the result channel for workers without messaging tools.
4. No channel, no parent address, or a failed send: return `BLOCKED / needs_input` with the exact question and the delivery problem through the normal result channel.
5. None of this stops a main session from asking the real user, and it does not replace platform approvals.
