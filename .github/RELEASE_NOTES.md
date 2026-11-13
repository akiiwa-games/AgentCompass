AgentCompass v0.6.2 fixes the tray that the installer starts. It builds on v0.6.1. The installer itself changed, so the tray's "Update AgentCompass" downloads and runs the full installer.

- **A tray started by the installer could not read the router's skill links.** The installer is built with Inno Setup, which since 6.7 turns on Windows' RedirectionGuard for itself; every process it starts inherits that, and such a process cannot follow a directory junction a normal (non-elevated) program made. The skill links `agentcompass setup` creates are such junctions. After an install or a full-installer update:
  - Usage Float stopped at start with `WinError 448`, and tray pages that read the installed skill failed the same way, until the tray was next started at sign-in.
  - The installer's own "setup follows the update" run (new in v0.6.1) stopped at the skill links on a machine that already had them.
- **The fix.** The installer no longer turns the guard on (it installs per user and never elevates, so there was nothing for it to protect), and it starts the tray through the `AgentCompass` scheduled task instead of as its own child, so the tray inherits nothing from the installer or from the old tray that launched the update.
- **Updating from v0.6.0 or v0.6.1.** If your tray was last started by one of those installers, this update is launched by that tray and its setup step can still stop once at the skill links; the tray it starts afterwards is clean. Run `agentcompass setup --yes` once after the update if you want the skill links and rules refreshed right away. If Usage Float is missing now, quitting the tray and starting it from the Start menu also brings it back.
- **For contributors.** The Windows Sandbox check now follows v0.6.0 routing (a local quota fixture in the guest, a classified dry run, no assertion on which account wins) and asserts that no tray process started by the installer carries RedirectionGuard.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.6.2/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
