AgentCompass v0.3.0 ships an installable router CLI and a Windows tray installer, with first-run guidance and safer host setup.

- Install the release wheel with `uv tool install <wheel-path-or-URL>` and use the `agentcompass` CLI: `setup`, `uninstall`, `doctor`, `providers`, and `plan` / `roll`.
- Windows tray installer: per-user Inno Setup installation, with a bundled Python runtime and helper processes. This build is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.0/packaging/SIGNING.md).
- First-run onboarding explains missing prerequisites and next steps. Host setup uses managed blocks and backups, supports uninstall and legacy-rule migration, and leaves hooks opt-in.
- A neutral synthetic default fleet replaces personal account assumptions. Keep your subscriptions and preferences in local `accounts.local.json`; the public model catalogue is separate from per-user policy.
- Router decisions report missing usage data honestly. Accounts, model families and quota pools derive from discovery and configuration; account preferences remain boolean state rather than generated reasons.
- Paseo completion notifications and explicit worker final reports return delegated results to the parent. Routing gates let parents steer their own running workers while preserving launch and notification checks.
- Tray account cards, priorities and usage follow discovered model families. The Decision Compass rendering, window identity and icons are improved, and frozen helper launch paths no longer assume a host Python installation.
- Dot plugin controls use stable attributes before translated labels, and remote work resolves the actual remote root without the old tunnel sender.
- Wheel, source distribution and tray payload checks exclude untracked repository files and private host state.

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
