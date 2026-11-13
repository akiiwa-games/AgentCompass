# Changelog

One entry per released tag, newest first. The full notes of each release are on the [Releases](https://github.com/akiiwa-games/AgentCompass/releases) page.

## v0.6.2 - 2026-11-13

- The installer starts the tray through its scheduled task and no longer turns on Inno Setup's RedirectionGuard: a tray or setup run started by the installer could not follow the skill links (`WinError 448`), which stopped Usage Float and the installer's own setup run.
- The Windows Sandbox check follows v0.6.0 routing and asserts that no installer-started tray carries RedirectionGuard.

## v0.6.1 - 2026-11-13

- A task marked `needs_visual` or `needs_gui` requires a model that takes image input at every `tool_operation` level, not only at level 3.
- An update applies an earlier `agentcompass setup` again (skill links, rule blocks, an installed gate hook); a machine that was never set up is left alone.
- `agentcompass setup --yes` no longer writes an account back into the allowed list that the user had taken out.
- The installer waits for a stopped tray to release its files; when it cannot continue it changes nothing and starts the tray again.
- `agentcompass doctor` names the right fix for a leftover `route-agent-work` skill link.

## v0.6.0 - 2026-11-13

- Top-level (level 3) demands are capped at the best measured model, not at the anchor, so a model above the anchor is credited on the hardest tasks. A change of the calibration panel stops a data refresh until it is accepted.
- Adding a model is a data edit: per-model facts moved from code into `references/accounts.json`, and `agentcompass data refresh --model <id>` merges only that model's measurements on the shipped scale.
- Gemini 4 Hydrogen joins the catalogue, scored from public boards.
- Antigravity Ultra: the Claude / GPT quota windows were sized at 5x / 20x the Pro figure, about fifty times too large. Ultra 20x now uses a maintainer measurement (about $24 per 5 hours and $48 per week at list prices); on Ultra 5x the capacity is unknown and those bundles leave automatic routing. The Gemini windows are unchanged.
- The continuation verdict says "same worker" only when a new turn to that worker would be admitted; otherwise it says to roll and names the reason.
- The spawn gate reads a shell call in the dialect it runs in and decides from the program each command runs.
- Public reference data moves to schema 4: v0.5.1 and earlier keep their last data set until they update.
- `agentcompass score list|set|clear` starts again; CI runs the fast test suite on every pull request.

## v0.5.1 - 2026-11-13

- A dollar API key shows the dollars it has spent (read from the key's own spend endpoint, no completion request); a budget is a balance that does not recover. Self-built harness keys are found without a network call.
- A dispatch's quota reservation is released when the worker ends; a watched worker sends one end notification.
- One tray switch per account decides whether it takes part in routing; every exclusion shows its reason.
- Old local state shapes are named by `agentcompass doctor` instead of being read silently.
- CLI updates run while sessions stay open; a failed release check stops the update; `agentcompass data --refresh` validates a fetched set again.

## v0.5.0 - 2026-11-12

- One scoring: four categories (creativity, coding, aesthetics, tool operation) fitted to the task's need and capped at the target, minus the quota economy. The older floors, tier gates and exchange-rate pricing are removed.
- Category scores and plan capacity come from cited public data; plans are read from each account, never assumed.
- `roll --workspace` takes a directory with one writer per directory; context rotation follows `rotate_after_carried_tokens`; completed dispatches record ground truth.
- Tray: a scores page with manual scores, strategy presets, quota-line views.

## v0.4.5 - 2026-11-10

- A failing tray component no longer rolls the installed app back; failures are written to the tray error log.
- Claude Fable 5.2 joins the catalogue (not auto-routed on metered accounts without a cited price).
- `roll --can-wait-hours`; `--workspace` implies `--local-only`; `--allow-concurrent` and `--wait` removed.
- README rewritten around the quota recovery line.

## v0.4.4 - 2026-11-10

- `roll` retries transient failures and switches to the next-best model by itself; failing providers are held out of dispatch.
- The dispatch gate recognises sub-agent launches behind MCP prefixes and authorizes `Workflow` and Paseo schedules.
- Global rule files carry a short pointer to the skill instead of the full dispatch procedure.

## v0.4.3 - 2026-11-09

- Failed usage readings retry after 2 minutes, doubling up to the normal interval.
- Quota, Integrations and Scoring & Capabilities pages repaired (remaining-quota bars, reading times, update badges, display scale).

## v0.4.2 - 2026-11-08

- Every task judgement comes from the classifier; an unavailable classifier blocks `roll` instead of guessing.
- Each account has one state (`ready`, `plan_inactive`, `usage_failing`, `auth_failed`) shared by `plan`, `roll` and the tray.
- One model resolver, step-down within what the provider dispatches, and named models are never silently replaced.

## v0.4.1 - 2026-11-07

- New `realtime_loop` task flag and per-effort responsiveness from community measurements keep slow-per-turn models away from live-loop tasks.

## v0.4.0 - 2026-11-06

- Incremental Windows updates: the installer carries only the runtime and updates download a small code package, with automatic rollback if it fails to start; the full installer is used only when the runtime changes.
- A review brief is never resumed on the worker that wrote the work.
