# Changelog

One entry per released tag, newest first. The full notes of each release are on the [Releases](https://github.com/akiiwa-games/AgentCompass-Router/releases) page.

## v0.7.5 - 2026-11-21

- The GitHub repository is `akiiwa-games/AgentCompass-Router`. Only the repository name and its URLs change (`origin.PRODUCT_REPO`, the project URLs, the installer's product URL, the documents); GitHub redirects the old address, so earlier installs keep finding updates and published data.
- The Windows tray carries a notice whose watcher died (Antigravity on Windows ends a command's processes when it returns, measured 2026-11-21): it runs `dispatch --dispatch-action reconcile` when a pass can change something (`dispatch_registry.needs_reader`, a watcher the bridge would restart, a receipt that proves an unrecorded ending), starts nothing otherwise, and backs off after a pass that changed nothing.
- One end notice per brief: a notice waits until its fallback chain is final, a relaunched attempt is recorded `relaunched` and not announced, and `reconcile` observes a covered failure whichever reader settled it.
- A send whose outcome Paseo reports as unknown is resolved from the parent's timeline; a resend takes a derived message id, 15 minutes after the first send and 60 after the second; after the third the notice is `undelivered` and the parent's next router answer names it once (`undelivered_end_notices`).
- A later dispatch of the same parent silences the earlier notice only once its turn landed (`end_notice.source: continued`, `continued_by`); the "took over" notice is removed.
- A launching claim stays with its `roll`: `missing_agent` leaves a claim in flight alone and `fallback_to_create` acts only on the claim it was given. A removed worker leaves a due notice. A reclaimed watcher lock holds its pid once. A turn that ended on its reply reads as replied after a late close of a tool call it started.
- The installer registers the logon task through `deploy startup` (no execution time limit, priority 4, 20 s delay); the task registered by earlier installers carried Task Scheduler's default 72 hour limit.
- The README and `llms.txt` say how AgentCompass differs from an HTTP proxy router.

## v0.7.4 - 2026-11-21

- An answer that refuses, defers or does nothing names what decided it and a way out: `roll`, `plan`, `record`, `dispatch`, the Paseo bridge (`condition` beside `error`) and the gate (what it denied on, and which check it could not make). A flag a stage does not read is said; a plan says when the user's own local settings kept the closest candidates out.
- The stages `select` and `delegate`, `subscription_judge.py` and the flags `--judge` and `--timeout` are removed. A plan decides a near tie by its documented order. `references/judge-workflow.md` is now `references/task-classifier.md`.
- A dispatch cadence is counted from the dispatches that launched, finished ones included, and is priced as a rolling limit. Automatic routing is held to it on every account that declares one; a route the user names launches past it with one warning; a project rule is refused at the limit.
- `roll --resume <id>` resumes a named worker whatever the age of its last dispatch. An account's `cwd_regex` is matched against the directory the worker runs in; an explicit `--resume` of a worker whose directory is unknown on such an account is refused.
- Pi's ChatGPT usage window is named by its length and read by the launch check. Antigravity's three Orchestrator options are scored between their planner and Gemini 4 Hydrogen.
- Nine maintainer scores set to the level the public boards measure (published 2026-11-21; data schema 8).
- Fixed: a store's lock file is no longer written to, so two first users cannot deny each other on Windows; a collection error after completion no longer leaves a dispatch open; `roll --resume --dry-run` answers instead of raising.
- Exit codes that moved: `record --basis` over 400 characters and `record --trigger workflow` without `user_override` exit 1; `record` with a project rule outside `allowed_accounts` is `blocked`, exit 2; `--account` alone is refused; `roll` with route flags refuses a `--constraint` other than `user_override`; a Dot runner's exit 3 with no task id is an error.

## v0.7.3 - 2026-11-20

- A model you name reaches the bundle you named. A bare name (`gpt-5.5`) names that model on every account, whatever prefix an account lists it under; one account being unavailable never blocks another. A name with a prefix (`openai/gpt-5.5`) is obeyed: it names only the bundles that prefix names (the listed spelling, or the account, provider, serving provider or Magpie preset of that id) and is never replaced by the same model elsewhere; when they cannot run, the answer lists each with its reason. A prefix that names no account or provider of your fleet is ignored and the rest is read as the name. Changed against v0.7.2: `openai/gpt-5.5` no longer launches another account's bare `gpt-5.5` while an `openai/gpt-5.5` bundle exists and cannot run; write the bare name when any account will do. Fixed: naming `opencode/deepseek-v4-flash` could launch `deepseek/deepseek-v4-flash` beside it, and `record` rewrote the named model onto the other row.
- A model name with an empty part around `/` (`/gpt-5.5`, `openai/`) is refused with a message instead of being read as the bare name. A temporary priority already stored with such a name stays in the file, matches nothing and ends at its own expiry.
- A `-preview` name is the released row of that name on every provider. A provider that shut its preview endpoint down, and a dated snapshot, keep their own declarations.
- One lookup order for a model spelling in every table (the full id, what follows each `/`, the last segment), so `<preset>/<publisher>/<model>` ids reach their row. A Magpie preset whose id differs from the catalog it serves (`opencode-zen`, `tencent-cloud` and 15 more) is read as that catalog on Magpie accounts only.
- The background Antigravity usage read can no longer open a sign-in page. When agy could not refresh its token (seen with a network failure on the token endpoint, sign-in intact), `agy -p /usage` started an interactive sign-in and opened the browser twice per collection pass. The read is now held to one process, which refuses the start of the browser, and a read that reached its time limit is not run again.

## v0.7.2 - 2026-11-19

- A model name reads the row its provider really serves (`accounts.json` `provider_names`, cited declarations). A name whose maker moved it to a newer dated snapshot reads the new one on any provider and with no provider known: DeepSeek's `deepseek-v4-pro` reads `deepseek-v4-pro-0813`; `deepseek-v4-flash` and `deepseek-v4.1-flash` read `deepseek-v4.1-flash-1017`. A spelling with an effort, a date, `-latest` or `-preview` reads the same row.
- A provider whose own listing names another dated snapshot keeps its own declaration (OpenCode's and Vivgrid's `deepseek-v4-flash`, listed as the `-0731` weights, has no row there); a listing that only repeats the name reads the maker's default. DeepSeek's repository ids (`deepseek-ai/DeepSeek-V4-Pro`, `-V4-Flash`, `-V4.1-Flash`) keep the weights they hold. Google's retired `gemini-3.1-flash-lite-preview` has no row; Mistral's two spellings of the Ministral 3 models are one row.
- What you stored is read on the row it meant: a model's checkbox under its earlier id, a manual score keyed by a moved name (it follows the name; a key naming the dated row wins for that row), temporary priorities and quick reset-priority targets. Your files keep their keys. Names that became one model and hold different choices leave it unchecked, and `agentcompass doctor` lists them.
- Every pair the Cindy catalog offers is covered (399 of 399); the eight known gaps of a provider serving other weights than the row are closed. The data schema stays 8: v0.7.1 accepts the published set and ignores the new table.
- Tray quota cards: an account out of automatic routing says so in one wording; a title that says a state no longer cuts the reading time, and a long title wraps; an account whose plan has no capacity says whether the plan could not be read or has no capacity data, instead of "Mode B".

## v0.7.1 - 2026-11-19

- A route you name (`--provider/--account/--model/--effort --basis`) launches past the automatic-routing switches (`allowed_accounts`, an unchecked model); facts still refuse it. The gate governs dispatch, not messages to a running worker.
- Reference data the maintainer publishes between releases reaches installs again: `published/<schema>/` on `main`, accepted before use, with the shipped set as fall-back. Off switch: `AGENTCOMPASS_PUBLIC_DATA=off`. The boards are still read once per release.
- Every model and effort the launch table and the Cindy and Magpie gateways offer has a value by a declared method or is on a known-gap list of a closed set of kinds; no inversion across effort or size. Fleet fits are frozen per release, so adding a model changes only its own rows.
- Gemini 4 Neon (Antigravity). `gpt-6-pro` is GPT-6 Astra at one operating point above Max with a 600 s response floor. The calibration panel takes `muse-spark-1.3` for `deepseek-v4.1-flash-free`. The aliases `gemini-4-pro` and `gemini-4-ultra` are removed.
- Scores and model checkboxes written under a name v0.7.0 resolved differently are kept (manual scores schema 4).
- On Windows no launch passes a value through `cmd.exe`: a `.cmd` launcher is started as the program it runs.
- The tray asks the host for model lists every six hours in the background instead of every ten minutes.

## v0.7.0 - 2026-11-18

- Level 3 of each facet's score scale is the calibration panel's best measured value instead of the anchor model; a level above 3 is shown as its number. Manual scores move to schema 3: the shipped ones and your own are converted by a declared rule so that each stands for the same value as before.
- Reference data ships with the version: the between-release public data channel is removed and nothing is fetched at run time.
- Settings written by earlier releases (account choices, host-rules overlays, removed providers, Usage Float autostart, manual scores) are carried into the current shape when read instead of being refused; `agentcompass doctor` lists only what needs you.
- Reference data rebuilt on 2026-11-18: the calibration panel adds `gpt-6-sol` and `grok-4.6` and drops `mimo-v2.6-pro` and `qwen3.8-max`; `qwen3-max` is removed.
- A manual responsiveness level bounds the other efforts, as a manual ability level already did.

## v0.6.8 - 2026-11-18

- GPT-6.2 Astra and GPT-6.2 Sol (`gpt-6.2-astra`, `gpt-6.2-sol`, released 2026-11-17) are in the shipped model data beside GPT-6 Astra and GPT-6.1 Sol, with their list prices, their Artificial Analysis readings and Codex and Pi bundles.
- The Artificial Analysis source reads the page's current record shape; it read no record after the page changed.

## v0.6.7 - 2026-11-17

- The classifier answers one of 11 work kinds with a depth (and an optional second kind); the four category demands are projected from a declared signature table instead of being asked one by one. The `divergence` category is renamed `creativity`.
- Aesthetics levels 2 and 3 also ask image recognition. Manual scores move to schema 2; `agentcompass score suggest` lists manual scores that differ from measured boards.
- After a watched launch, `roll` tells the coordinator to finish independent work and end its turn instead of polling in a shell.
- Ledger and dispatch rows written by earlier versions carry the old profile; `agentcompass doctor` lists them.

## v0.6.6 - 2026-11-17

- The default context rotation threshold rises from 2M to 4M carried tokens (the quota-saving preset from 1.5M to 3M), so workers are replaced less often.
- `roll` resumes an idle worker of the parent under the threshold that passes the gate checks for an explicit chain, the same brief or the same `--workspace`; an explicit `--resume` works across a different routed bundle with a warning and refuses a busy, worn or gated worker with a `blocked` result. The rules tell sessions to send follow-up work back to the worker holding its context.

## v0.6.5 - 2026-11-17

- The one-click update no longer fails when the anonymous GitHub API quota is used up: it reads the latest release from the release page instead, with the same SHA-256 check. Periodic checks send conditional requests and wait after a refusal.
- `codex exec` from a shell is a quick call like `claude -p`, and the spawn gate no longer reads it as a worker launch.

## v0.6.4 - 2026-11-17

- The spawn gate no longer blocks Claude Code's built-in tool agents (`web-fetch`, `statusline-setup`, `claude-code-guide`): calling one launches no worker. A project or user agent definition that takes one of those names is still a launch. The default rules name the same exception.
- `orca orchestration worker-start -h` and `codex exec -h` ask for help and are no longer read as launches.

## v0.6.3 - 2026-11-16

- Google Gemini Decision 1 (Decisions API) is a selectable task classifier (`scorer_type: gemini_decision`, key `gemini_api_key` or `GEMINI_API_KEY`). It bills input tokens, so it runs only when chosen.

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
