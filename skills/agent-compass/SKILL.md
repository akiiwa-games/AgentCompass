---
name: agent-compass
description: Choose owner, model and effort, outbound authorization, advisor use, and continuation for delegated work. Reads local usage and effective account policy, profiles the task with the configured System-One scorer, then allocates a compatible worker. Use for independent worker or advisor work; ordinary local execution needs no routing.
---

# AgentCompass

To delegate work to a subagent, worker or advisor, run one command; the router picks the model, launches the worker and returns what to do next:

```bash
python -X utf8 ~/.codex/skills/agent-compass/scripts/route_judge.py roll --task "<what the worker should do>"
```

Long or multi-line assignments: pipe them in with `--task -` (or `--brief <file>`); the worker always gets your text unchanged. Do not call `create_agent`, `Agent`, `spawn_agent`, `invoke_subagent` or a CLI yourself.

Do small local work (about five minutes, three tool calls) yourself; a delegated worker never delegates again. Read `status` in the JSON and do exactly `next_action`: `launched` = start nothing else (`next_action` says how the result arrives), `pending` = run `next_argv`, never roll again, `self` / `native` / `blocked` / `error` = follow `next_action`. Full details are in `details`.

Optional, only when true: `--hours <n>`, `--local-only "<dependency>"`, `--file <path>`, `--fast`, and for a user-named model `--provider/--account/--model/--effort --basis "<user quote>"`.

More: [roll reference](references/roll-reference.md) (result variants, Dot/Grok/CLI/native) · [router internals](references/router-internals.md) (when to delegate, plan/record/close-out stages, scoring, launch contract, reference index).
