# Parent addressing and worker questions

- Failure: an unaddressed worker cannot reach its coordinator; UI questions (including `request_user_input_async`) strand the workflow in the worker session.
- Action: refuse dispatch without an actual parent ID; refuse interactive user-question calls by an identified worker. Reads, repairs, messages and main-session questions remain allowed.
- Decision: use the launching session's host ID, include it and its messaging address in the delivered assignment, and preserve parentage in host metadata. Never substitute `top-level`, a task ID or the worker's own ID. Paseo resolves the parent before creation; native host orchestrators must carry the coordinator session ID plus its live inbox address and preamble.
- Recovery: obtain the actual parent from the host (`PASEO_AGENT_ID`, or the native host session ID and inbox address), supply it to `roll`, then deliver `worker_prompt_file` unchanged with the assignment. Workers send questions through the parent messaging channel.
- Degradation: missing parent information prevents launch. Missing worker-role evidence alone does not block a main-session question. Failed delivery after launch returns `BLOCKED / needs_input` through the existing result channel; it never permits a local question UI.
- Coverage: local hooks enforce questions for Paseo-labelled workers and CLI workers marked by the router. Native host preambles carry the same rule; runtime enforcement there depends on the host exposing worker identity to its hook.
