---
name: agent-compass
description: Choose owner, model and effort, outbound authorization, advisor use, and continuation for delegated work. Reads local usage and effective account policy, profiles the task with the configured System-One scorer, then allocates a compatible worker. Use for independent worker or advisor work; ordinary local execution needs no routing.
---

# AgentCompass

## Execution ownership

The main session executes serial work directly by default, including ordinary investigation, edits and verification. Duration, tool-call count and reading multiple files are not reasons to delegate. Delegate when independent work benefits from parallelism, isolation of a large context, a specialist capability or quota offloading. The main session is the only coordinator. Before writing any brief, list the separate tasks in the work (one deliverable each, with what it waits on): keep the tasks coupled to the conversation, write one brief per independent task, and roll them together; never hand several tasks to one worker to coordinate. Start a task that needs another's result after that result returns. Parts that each wait on the one before are one serial task: do it yourself or roll it as one brief. Continue related work with the existing worker while available and within its context limit. If routing itself is broken, diagnose and repair it directly. A delegated worker (`AGENTCOMPASS_WORKER=1` or a `Parent session agent ID:` contract) executes its assignment without further delegation unless explicitly appointed coordinator.

## Dispatch

To delegate work to a subagent, worker or advisor, run one command; the router picks the model, launches the worker and returns what to do next:

```bash
agentcompass roll --task "<what the worker should do>"
```

Before you launch, continue or replace a worker, read [the dispatch rules](references/dispatch-rules.md): model and effort overrides, re-rolling an unavailable owner, the configured context rotation threshold (`rotate_after_carried_tokens`, set in the tray), permission modes, the gate and `record`, and parent messaging.

Long or multi-line assignments: `--task -` reads stdin, `--brief <file>` reads a file; the worker always gets your text unchanged. Several single-task briefs go in one call, `--brief a.md --brief b.md`: they are classified at the same time and routed one after another (`status: batch`, one result per brief). Do not call `create_agent`, `Agent`, `spawn_agent`, `invoke_subagent` or a CLI yourself.

`agentcompass` is the only entry and works from any directory; do not run files under `scripts/`. Installed package (`uv tool install` / `pip install`): type it as shown. Source checkout without an install: `python -X utf8 <checkout>/agentcompass roll ...` (the same arguments; your global agent rules spell the absolute path). `agentcompass -h` lists the commands, `agentcompass <command> -h` shows the options of one.

Read `status` in the result and do exactly `next_action`: `launched` = start nothing else (`next_action` says how the result arrives), `pending` = run `next_argv`, never roll again, `split` = the classifier reads the brief as several separate tasks (`task_count`): split it yourself, roll one brief per independent task and do the coupled ones yourself; if it truly is one assignment, roll it again with `--keep-whole "<why>"` (the daily audit reviews these reasons), `self` / `native` / `blocked` / `error` = follow `next_action` (`blocked` with `blocked_by` = that `--workspace` already has a writer; `blocked` with `caller_action: "wait_for_reset"` = you passed `--can-wait-hours` and the task should wait `start_after_hours` for a stronger model's quota window to reset before rolling again; see [the dispatch lifecycle](references/dispatch-lifecycle.md)). The full result is at `details` (or print it with `--verbose`).

Optional, only when true (`agentcompass roll -h`): `--workspace <dir>`, `--hours <n>`, `--can-wait-hours <h>` (only when the work is not urgent: how many hours it may wait for a stronger model's quota window to reset; without it, `roll` launches the best model that can start now), `--local-only "<dependency>"` (only when no `--workspace` is given; `--workspace` already implies local work), `--keep-whole "<reason>"`, `--new-worker`, `--resume <agent_id>`, `--file <path>`, `--apply-to <path>`, `--fast`, and for a user-named model `--provider/--account/--model/--effort --basis "<user quote>"`.

Herdr core auto-detects its actual terminal. Read [Herdr first-time setup and support limits](references/herdr.md) for installation and completion collection. Raw shell roll in Cindy is refused; the Cindy plugin is a separate repository (`cindy-agentcompass-plugin`).

Troubleshooting after an update: if local state under `~/.agent-compass`, hooks or global rule blocks fail with an old-shape error, run `agentcompass doctor` and apply the printed fix (`re-run "agentcompass setup"`, `re-toggle in the tray`, or `delete "<file>"`).

More: [dispatch rules](references/dispatch-rules.md) · [roll reference](references/roll-reference.md) (result variants, Dot/Grok/CLI/native) · [router internals](references/router-internals.md) (when to delegate, plan and record, scoring, launch contract, reference index).
