AgentCompass v0.3.4 keeps the main session as the only coordinator: a brief that bundles several independent tasks comes back to the parent instead of going to one worker, and several single-task briefs can be rolled in one command.

- The System-One classifier reads the whole brief and also answers `multi_task` (true/false) and `task_count` (`1`, `2`, `3`, `>3`). Every scorer supports both fields: agy, Claude, Codex and OpenAI-compatible scorers through the structured profile schema, Jev/TypeSafe through a `noul` and a `choice` question, and the local heuristic from numbered task or deliverable headings.
- A multi-task brief returns `status: split` with the task count and starts no worker. The parent splits it and rolls each task; when the parent judges the work one coupled task, `--keep-whole "<reason>"` dispatches it whole and records the reason in the ledger.
- `roll --brief a.md --brief b.md ...` classifies all briefs concurrently, then routes them one after another and returns `status: batch` with one result per brief. Per-task options (hours, local-only, keep-whole, files, pinned model, wait) stay single-brief.
- The daily audit lists split and kept-whole decisions with their reasons. The Cindy bridge treats `split` like `self` and returns the router result.
- Host rule overlays can include the shared rules with `@@SHARED@@` instead of copying them, and the shipped rules describe split and batch dispatch.

The score cache version changed, so the first classification of each brief after upgrading runs the scorer again.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.4/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
