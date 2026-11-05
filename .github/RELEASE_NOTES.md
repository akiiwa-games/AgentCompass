AgentCompass v0.3.14 fixes the version the installed tray shows and makes an interrupted dispatch say why it can't finish, on top of v0.3.13.

- **The tray no longer shows "命令行 0.0.0"**: On a machine with only the Windows installer, the tray read the version of its own hook, skill link and PATH entry from files the installer bundle doesn't have on disk, and showed "AgentCompass 0.3.13 · 命令行 0.0.0（版本不一致）". Those entries come from the same install as the tray, so it now shows "AgentCompass x.y.z".
- **An interrupted dispatch explains itself**: If a `roll` is killed before its create reaches the host, nothing is created, and the dispatch stays `launching` until its lease expires; it never creates a second time. `agentcompass launch --dispatch-id …` now returns an `observe_error` (for example `agent_not_observed`) instead of a bare `launching`.
- **Release check**: the "roll killed while it watches" scenario now kills the roll only after the host has answered its create, so it no longer fails at random on slow machines.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.14/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
