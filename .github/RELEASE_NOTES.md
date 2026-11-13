AgentCompass v0.6.0 credits models above the anchor on the hardest tasks, makes adding a model a data edit plus one command, and adds Gemini 4 Hydrogen. It builds on v0.5.1. Only application code and reference data changed, so the tray's "Update AgentCompass" installs the code package.

- **Headroom on the hardest tasks.** A level-3 demand is now capped at the best measured model in each category instead of at the anchor (Claude Opus 5.5 High). Before, every model at or above the anchor scored the same on the hardest tasks, so a stronger model had no advantage there.
  - Levels 1 and 2 are unchanged. The quota value ladder follows the same ceiling.
  - The radar charts stay on the anchor scale; the scores page shows the cap the fit reads, and the level-3 hover shows the score above the anchor that the fit counts.
  - A data refresh that would change the calibration panel (a board dropping a panel model, for example) stops and names the change. `--accept-panel-change` accepts it.
- **Adding a model is a data edit.** Per-model facts (board names, fast-mode support, list prices, the calibration panel, model families) moved from Python into `references/accounts.json`, each a cited public value or a dated maintainer declaration.
  - `agentcompass data refresh --model <id>` merges only that model's measurement rows on the shipped scale and rebuilds the generated files. It takes seconds; exit code 3 means the request was refused and nothing was written.
  - A catalogue without the families table, or without list prices, is a named error instead of a silent default.
  - A fast launch is validated against the model's catalogue row: a model not declared fast-capable is not launched in fast mode.
- **Gemini 4 Hydrogen.** The model joins the catalogue on Antigravity accounts. Its category scores, tier, price per hour and duration come from public boards and the same-series conversion; no manual score is attached.
- **Antigravity Ultra: the Claude and GPT quota is about fifty times smaller than assumed.** Its weekly and 5-hour capacity had been taken as 5x / 20x the Google AI Pro figure ($2,580 a week on Ultra 20x). Google's statement does not name that track, and public reports have it run out within minutes to a few hours of one model. With the old figure the router kept sending work to a window that was nearly spent.
  - Ultra 20x now uses a maintainer measurement on one account: about $24 of list-priced usage per 5 hours and $48 per week, roughly two worker-hours of Claude Opus 5.5 a week. The data records the sample (runs, turns, dollars) with the value.
  - Ultra 5x has no measurement, so the capacity of `3p-5h` and `3p-weekly` there is unknown by declaration and the bundles that draw them are not picked automatically. Naming the model still launches it.
  - The Gemini windows keep their capacity and stay in automatic routing. Google AI Pro is unchanged.
  - A vendor plan multiplier now names the windows it covers, and a window declared unmeasured is listed by `agentcompass data refresh` without failing it.
- **Continuation verdict.** The verdict a parent receives says "same worker" only when the worker is under the context threshold and a new turn to it would be admitted: the account is ready, nothing holds the account, its quota pool or the model, the quota windows are not exhausted, and the target is not switched off. Otherwise it says to roll and names the reason. The check is read-only and never waits on a lock; when it cannot be made it says unknown.
- **Spawn gate.** A shell call is read in the dialect it runs in (PowerShell by default on Windows) and decided from the program each simple command runs, not from text anywhere in the line. Launches behind heredoc-fed shells, `eval` and wrappers are recognised.
- **Public reference data, schema 4.** The catalogue shape changed, so the data channel moves to schema 4. An install of v0.5.1 or earlier reports the published set as too new and keeps its last data set until it is updated; the message names the action.
- **Fixes.**
  - `agentcompass score list|set|clear` failed to start; it starts again.
  - A temporary priority rule on a model, and the check that retires it when the quota is nearly spent, resolve the model against the catalogue.
  - A failed auto-update shows its error text.
  - The dispatch gate holds a quota window of unknown capacity at the safety margin, as routing already did.
- **For contributors.** CI runs the fast test suite on Windows for every push to `main` and every pull request, and builds the wheel on Linux. `route_judge.py` and provider discovery are split into single-purpose modules (see the module map in `AGENTS.md`). The documents were checked against the code, and cited repository paths are verified by the document check. The demo animation and the social preview use the current category names.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.6.0/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
