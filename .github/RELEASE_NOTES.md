AgentCompass v0.3.9 adds version display and one-click updates to the tray, on top of v0.3.8.

- **The tray shows its version and updates itself**: The System card shows the AgentCompass version and every router install it finds: the skill scripts, the gate hooks and `agentcompass` on PATH. It flags a version mismatch, checks GitHub for a newer release every six hours, and has an "Update AgentCompass" button that updates each install the way it was installed: the installer, a pip or uv wheel, or a git checkout.
  - The wheel upgrade runs from the temp directory, not from inside the install it replaces. That stops `uv tool install --force` failing with os error 32 and leaving `agentcompass` on PATH broken.
  - The installer closes the running tray before replacing it and starts it again afterwards.
- **Tray restart responds**: Restart is single-flight and shows progress, so a click no longer looks like it did nothing, and repeated clicks don't stack restarts.
- **Oh My Pi subscription logins**: Discovery reads Oh My Pi's OAuth logins (OpenAI Codex, Anthropic), read-only, and counts them against the matching subscription's quota.
- **CLIs installed after start-up are found**: A long-running process such as the tray or Paseo now finds a CLI installed after it started, by reading the current user's PATH from the registry.
- **Router gate inside Orca terminals**: A route rolled from Claude Code running inside an Orca terminal now matches the Claude session that launches the worker, so the gate no longer blocks it ([#2](https://github.com/akiiwa-games/AgentCompass/issues/2)).

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.9/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
