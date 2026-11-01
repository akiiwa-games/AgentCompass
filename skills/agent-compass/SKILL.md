---
name: agent-compass
description: Choose owner, model and effort, outbound authorization, advisor use, and continuation for delegated work. Reads local usage and effective account policy, profiles the task with the configured System-One scorer, then allocates a compatible worker. Use for independent worker or advisor work; ordinary local execution needs no routing.
---

# AgentCompass

## Execution ownership

The main session executes serial work directly by default, including ordinary investigation, edits and verification. Duration, tool-call count and reading multiple files are not reasons to delegate. Delegate when independent work benefits from parallelism, isolation of a large context, a specialist capability or quota offloading. Continue related work with the existing worker while available and within its context limit. If routing itself is broken, diagnose and repair it directly. A delegated worker (`ROUTER_WORKER=1` or a `Parent session agent ID:` contract) executes its assignment without further delegation unless explicitly appointed coordinator.

## Dispatch

To delegate work to a subagent, worker or advisor, run one command; the router picks the model, launches the worker and returns what to do next:

```bash
agentcompass roll --task "<what the worker should do>"
```

Long or multi-line assignments: `--task -` reads stdin, `--brief <file>` reads a file; the worker always gets your text unchanged. Do not call `create_agent`, `Agent`, `spawn_agent`, `invoke_subagent` or a CLI yourself.

`agentcompass` is the only entry and works from any directory; do not run files under `scripts/`. Installed package (`uv tool install` / `pip install`): type it as shown. Source checkout without an install: `python -X utf8 <checkout>/agentcompass roll ...` (the same arguments; your global agent rules spell the absolute path). `agentcompass -h` lists the commands, `agentcompass <command> -h` shows the options of one.

Read `status` in the result and do exactly `next_action`: `launched` = start nothing else (`next_action` says how the result arrives), `pending` = run `next_argv`, never roll again, `self` / `native` / `blocked` / `error` = follow `next_action`. The full result is at `details` (or print it with `--verbose`).

Optional, only when true (`agentcompass roll -h`): `--hours <n>`, `--local-only "<dependency>"`, `--file <path>`, `--fast`, and for a user-named model `--provider/--account/--model/--effort --basis "<user quote>"`.

Herdr core auto-detects its actual terminal. In Cindy, use the AgentCompass plugin's `roll` tool with only `task`; raw shell context cannot identify its parent. Read [Herdr/Cindy first-time setup and support limits](references/herdr-cindy.md) for installation, exact account bindings and completion collection.

More: [roll reference](references/roll-reference.md) (result variants, Dot/Grok/CLI/native) · [router internals](references/router-internals.md) (when to delegate, plan and record, scoring, launch contract, reference index).
