AgentCompass v0.3.12 fixes what moving a real machine from a checkout or pip/uv install to the Windows installer turned up, on top of v0.3.11.

- **Setup takes over its old skill links**: `~/.codex/skills/<name>` links that point at another AgentCompass install's copy of a skill (a checkout, a wheel or an older bundle) are now re-pointed at the installer's bundle, so skills stop running from the old copy. Gemini's materialised copy follows. Uninstall puts the previous links back. Links into anything that isn't an AgentCompass install are left alone.
- **`doctor` finds tools where their installers put them**: Without a checkout, `doctor` now also looks in each tool's standard location (`~/.bun/bin`, `~/.local/bin`, Program Files), as the tray does. Before, it reported Bun as missing even though ChatGPT Web was running with it.
- **Tray**: a docstring no longer raises a SyntaxWarning when the tray code is compiled.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.12/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
