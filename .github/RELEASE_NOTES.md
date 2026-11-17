AgentCompass v0.6.5 keeps the one-click update working when GitHub's anonymous API quota is used up, and makes the spawn gate treat every one-shot model CLI call alike. It builds on v0.6.4.

- **The update no longer stops at "rate limit exceeded".** GitHub gives anonymous API requests 60 an hour per public address, shared by everything behind that address. When the quota was gone, **Update AgentCompass** failed at once. Now:
  - An explicit update that gets a rate-limit refusal reads the latest release from the release page instead (the `releases/latest` redirect, then that tag's asset list). The installer is verified against its SHA-256 exactly as before; a release without a digest is still refused.
  - Periodic checks send conditional requests (`ETag`), which GitHub does not count when nothing changed, and wait 15 minutes, or as long as GitHub says, after a refusal instead of asking again.
  - No GitHub token is needed, read or stored.
- **A one-shot model CLI call from a shell is a quick call, not a launch.** The gate read `codex exec "..."` as an unrouted worker launch while `claude -p`, `agy -p`, `gemini -p`, `opencode run` and `pi -p` passed. A model CLI asked for one answer starts no worker that needs a route, so `codex exec` passes too. Worker launchers (`paseo run`, `orca orchestration worker-start`, the Dot runner) and every sub-agent tool still need a route. The dispatch rules say the same.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.6.5/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
