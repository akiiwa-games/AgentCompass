# Parent addressing and worker questions

- Failure: an unaddressed worker cannot reach its coordinator; UI questions (including `request_user_input_async`) strand the workflow in the worker session.
- Action: refuse dispatch without an actual parent ID; refuse interactive user-question calls by an identified worker. Reads, repairs, messages and main-session questions remain allowed.
- Decision: use the launching session's host ID, include it and its messaging address in the delivered assignment, and preserve parentage in host metadata. Never substitute `top-level`, a task ID or the worker's own ID. Paseo resolves the parent before creation; native host orchestrators must carry the coordinator session ID plus its live inbox address and preamble.
- Recovery: obtain the actual parent from the host (`PASEO_AGENT_ID`, or the native host session ID and inbox address), supply it to `roll`, then deliver `worker_prompt_file` unchanged with the assignment. Workers send questions through the parent messaging channel.
- Degradation: missing parent information prevents launch. Missing worker-role evidence alone does not block a main-session question. Failed delivery after launch returns `BLOCKED / needs_input` through the existing result channel; it never permits a local question UI.
- Coverage: local hooks enforce questions for Paseo-labelled workers and CLI workers marked by the router. Native host preambles carry the same rule; runtime enforcement there depends on the host exposing worker identity to its hook.

## Completion-notification gate contract

- **Failure:** disabling completion notifications can lose the result of a worker without messaging tools.
- **Action:** block only `send_agent_prompt` with explicit `notifyOnFinish: false` from a parent to its own worker, including statically parsed wrapper calls.
- **Decision:** the hook's caller ID must equal the target's locally persisted `router.parent_id` / `paseo.parent-agent-id`; conflicting labels do not prove ownership. No network lookup is needed.
- **Recovery:** set `notifyOnFinish: true` or omit the parameter. Worker-to-parent questions and final reports remain allowed with `false`.
- **Degrade:** missing caller identity, missing/unreadable target records, ambiguous ownership or dynamic wrapper arguments leave this check open; other router checks still apply.

## Disabled-target and context-rotation gate contract

- **Failure:** launching new work on a disabled account/bundle ignores the user's scheduling choice; starting another turn on a worker with >= 2M historical uncompressed tokens risks context drift. Blocking its parent's control messages during an existing turn instead prevents orderly wrap-up and handoff.
- **Action:** block new launches on disabled targets and new turns on disabled or over-limit workers. Exempt only messages from the parent to its own currently running worker, so the parent can steer, cut scope, answer questions or request wrap-up. Quota and completion-notification checks remain unchanged.
- **Decision:** reuse the completion-notification ownership test: a non-self caller must match every nonempty locally persisted `router.parent_id` / `paseo.parent-agent-id`. The local Paseo agent record must also say `lastStatus: running` (or `status: running` when present); an explicit current non-running `status` takes precedence. `activeTurn` alone, tool arguments and prompt wording do not prove a running turn. No network lookup is required. Idle workers remain blocked even for prompts requesting only commit/handoff/report: the API has no structured wrap-up-only operation.
- **Recovery:** while the owned worker is running, send a control message with completion notifications enabled. Otherwise collect its existing output, use `cancel_agent` if necessary, write a handoff and run `agentcompass roll --brief <handoff.md>`; a disabled account/bundle may instead be re-enabled in AgentCompass settings. An over-limit worker still requires rotation.
- **Degrade:** missing/unreadable records, missing caller identity, conflicting ownership or unknown/non-running status grant no exemption; existing checks retain their existing degrade behavior. This keeps unknown/idle state from authorizing new work on a disabled target. Local `lastStatus` is the persisted snapshot, not an atomic daemon guarantee: status can change between the hook and message delivery, and offline records can be stale. Without a structured steer-only API this hook cannot eliminate that race; do not infer liveness from timestamps or message keywords.
