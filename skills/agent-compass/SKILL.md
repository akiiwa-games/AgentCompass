---
name: agent-compass
description: Choose owner, model and effort, outbound authorization, advisor use, and continuation for delegated work. Reads local usage and effective account policy, profiles the task with the configured System-One scorer, then allocates a compatible worker. Use for independent worker or advisor work; ordinary local execution needs no routing.
---

# AgentCompass

To delegate work to a subagent, worker or advisor, run one command; the router picks the model, launches the worker and returns what to do next:

```bash
agentcompass roll --task "<what the worker should do>"
```

Long or multi-line assignments: `--task -` reads stdin, `--brief <file>` reads a file; the worker always gets your text unchanged. Do not call `create_agent`, `Agent`, `spawn_agent`, `invoke_subagent` or a CLI yourself.

`agentcompass` is the only entry and works from any directory; do not run files under `scripts/`. Installed package (`uv tool install` / `pip install`): type it as shown. Source checkout without an install: `python -X utf8 <checkout>/agentcompass roll ...` (the same arguments; your global agent rules spell the absolute path). `agentcompass -h` lists the commands, `agentcompass <command> -h` shows the options of one.

Decide by task shape, not duration: single-thread work (one deliverable, each step depends on the last, the user may redirect) stays with you even if it takes 10 to 30 minutes; delegate parallel independent work, context-heavy work where only the conclusion matters, clearly long-horizon work, or work that needs another model or account's quota. A delegated worker never delegates again. Read `status` in the result and do exactly `next_action`: `launched` = start nothing else (`next_action` says how the result arrives), `pending` = run `next_argv`, never roll again, `self` / `native` / `blocked` / `error` = follow `next_action`. The full result is at `details` (or print it with `--verbose`).

Optional, only when true (`agentcompass roll -h`): `--hours <n>`, `--local-only "<dependency>"`, `--file <path>`, `--fast`, and for a user-named model `--provider/--account/--model/--effort --basis "<user quote>"`.

Herdr core auto-detects its actual terminal. In Cindy, use the AgentCompass plugin's `roll` tool with only `task`; raw shell context cannot identify its parent. Read [Herdr/Cindy first-time setup and support limits](references/herdr-cindy.md) for installation, exact account bindings and completion collection.

More: [roll reference](references/roll-reference.md) (result variants, Dot/Grok/CLI/native) · [router internals](references/router-internals.md) (when to delegate, plan and record, scoring, launch contract, reference index).
