AgentCompass v0.3.11 makes the Windows installer the whole product: install `AgentCompass-tray-setup.exe` and nothing else, with no wheel and no system Python.

- **The command line ships in the installer**: A console `agentcompass.exe` is installed in `%LOCALAPPDATA%\AgentCompass\cli` and added to your user PATH, and the uninstaller removes it. The folder name never changes, so PATH entries, hooks and skill links survive the tray's one-click update.
- **No Python needed for hooks and skills**: The spawn gate runs as `agentcompass.exe gate-hook` in Claude Code, Codex, Gemini and Pi. The bundled exe also runs the skill scripts in place of `python`. Checkout and pip/uv installs keep their Python commands.
- **Setup asks first**: The installer changes no host config by itself. On first start the tray offers a setup window that previews `setup --dry-run`, leaves the spawn gate off unless you tick it, and writes nothing until you confirm. For unattended installs, use `AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks` (or `nohooks`); it logs to `~/.agent-compass/installer-setup.log`.
- **Moving from pip/uv or a checkout**: Setup replaces old `python … router_gate_hook.py` hook entries with the installer's exe. `agentcompass doctor` names the active install and lists every `agentcompass` on PATH. If a uv/pip copy comes first, doctor warns and prints the exact removal command. The tray shows "AgentCompass x.y.z" on an installer-only machine and labels a second install "命令行" (command line).
- **macOS/Linux and CLI-only use** still install with uv or pip; see the README.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.11/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
