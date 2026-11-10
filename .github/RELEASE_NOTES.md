AgentCompass v0.4.5 stops a failing tray component from silently undoing an update. It also adds Claude Fable 5.2, a `--can-wait-hours` option for non-urgent work and a rewritten README. It builds on v0.4.4. This release changes the tray loader inside the exe, so the tray's "Update AgentCompass" installs the full installer automatically.

- **Updates are no longer undone in the background.** In v0.4.3 and v0.4.4, an error in a background tray component rolled the installed app back to the previous package, and the tray still reported the update as complete. The trigger seen in the field was a temporary-folder cleanup error in the capacity sampler.
  - A component failure, or a failed second launch while the tray is running, never changes the installed app.
  - The tray rolls back only when its own first start fails before the tray icon appears, which is the case a bad package causes.
  - Every tray or component failure is written with its traceback to `~/.agent-compass/tray-errors.log`, which rotates at 1 MB.
  - A rollback is announced once at the next tray start. A rollback to a package from before this release (v0.4.4 or older) cannot show this notice; the log entry is still written.
- **Claude Fable 5.2** joins the routable models, with scores from cited public data.
- **`roll` options.**
  - `--can-wait-hours <h>`: for work that is not urgent. It lets `roll` wait up to `h` hours for a stronger model's quota window to reset, and for a transient provider failure to clear. Without it, `roll` launches the best model that can start now.
  - `--workspace <id>` implies `--local-only`, and one writing worker per workspace is always enforced. Whether a brief only reads or writes its workspace is classified, and `--access` overrides it.
  - Removed: `--allow-concurrent` and `--wait`. Collect a worker's result through `next_action`.
- **README.** The README is rewritten around the quota recovery line, with new films of the decision compass and the Quota line tab.
- **Five-axis scoring ships off.** The public five-axis scores, the quota-economy ranking and the tray's Quota line charts are included but stay disabled (`scoring.five_axis.enabled` is off by default). Routing works as in v0.4.4 until it is turned on. The README describes that ranking.
  - The charts draw each quota window as its quota line and usage line, with a radar of the winner against the best-ranked other model.
  - A fast-mode route is marked "fast".
  - Decisions record those charts only while it is on.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.5/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
