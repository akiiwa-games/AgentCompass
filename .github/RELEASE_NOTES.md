AgentCompass v0.4.1 keeps models that are slow per turn away from tasks where the worker itself sits in a live loop: playing a game, idling through dailies, grabbing tickets, watching a market, or polling a page to click the moment something appears. It builds on v0.4.0.

- **A new task flag for live loops**: `realtime_loop` marks a task where the worker repeatedly observes and acts inside a running system, so a slow turn changes the outcome. Writing a ticket bot, fixing game automation code, or analysing an idle-farming log is not a live loop: a script runs the loop, not the model.
  - **Only the classifier decides**: the flag comes from the classifier's reading of the task, or from a value the caller passes. No keywords or patterns in the code; a missing value is `false`. It does not turn on `needs_gui`, `needs_visual` or `local_only` by itself. The classifier calibration set grew to 47 cases, including paired opposites such as "write a ticket bot" and "watch the ticket page and buy at noon".
- **Models now carry a per-effort `responsiveness`**: taken from community measurements (Artificial Analysis time to first answer token and output tokens per second; OpenRouter only where AA has none) and shipped in the reference data, so a fresh install has it with no local history.
  - **How it is computed**: per-turn time = (time to first answer + 400 / output tok/s) × harness overhead, mapped from 4 s → 1 to 45 s → 0. For example Gemini 4 Argon High takes about 46 s per turn (0.00); Claude Sonnet 5.5 low about 6.6 s.
  - **Models without measurements** (including variants found by provider discovery) get a cautious 0.52: never excluded, but ranked behind measured fast models.
  - Maintainers refresh the numbers with `scripts/refresh_speed_data.py`; routing never goes online for them.
- **Live-loop routing**: a live-loop task needs responsiveness of at least 0.50 (about 24.5 s per turn).
  - A bundle below that steps down to the highest faster effort of the same model that clears both the floor and the task's ability gate, and only to an effort that harness actually supports. Antigravity models never step down. Cost, quota and the ledger use the effort actually dispatched.
  - A model with no effort fast enough is excluded as "too slow per turn for a live loop".
  - A model or effort you name yourself is kept as chosen, with a warning that it is slow for a live loop.
- **Other tasks route as before**: compared with v0.4.0, every non-live-loop calibration case and existing routing test picks the same worker.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.1/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
