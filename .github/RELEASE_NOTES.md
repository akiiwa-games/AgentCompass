AgentCompass v0.3.13 fixes a setup bug that removed the router skill from a second Claude account, on top of v0.3.12.

- **A second Claude account keeps its skills**: A secondary Claude home such as `~/.claude-2` reads its skills through a link to `~/.claude/skills`. When only the secondary account was signed in, setup treated `~/.claude` as unused and removed the skill links inside it, so the signed-in account lost the router skill. Setup now keeps those links whenever an active secondary home shares them.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.13/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
