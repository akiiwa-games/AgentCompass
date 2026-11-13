# Security

## What AgentCompass reads on your machine

- **Provider sign-in files, read-only.** To find your accounts and read quota windows, AgentCompass reads the local sign-in state of the provider CLIs it routes to (Claude Code, Codex, Antigravity, Grok, Pi, and the homes of additional accounts). It does not refresh or overwrite the tokens of the Claude, Codex, Antigravity and Grok CLIs; refreshing stays with the owning CLI. One write exists: when the Codex usage endpoint answers 401 for the sign-in that Pi keeps in `~/.pi/agent/auth.json`, the Pi usage poll (`fetch_pi_usage` in `usage_core.py`) asks the provider for a new token and writes the new access and refresh token back to that file. Snapshots are bound to a one-way SHA-256 fingerprint of their credential source, not to the credential.
- **Never printed, logged or committed.** Credentials, tokens, raw transcripts and unredacted API responses are not written to output, logs, the decision ledger or the repository. A dollar API key found in a harness configuration is read again on each usage poll and is not copied into any AgentCompass file.
- **Local state** lives under `~/.agent-compass` (settings, usage cache, decision ledger, dispatch state). A scorer API key, if you configure one, is stored in `scorer-config.json` there.
- **Network.** Quota reads go to the providers you already use. Update checks read GitHub Releases and the public reference data on the repository's `main` branch. The task text is sent to the configured System-One scorer, which runs on your own subscription CLI or a configured API.

The reference for these rules is [`docs/technical-reference.md`](docs/technical-reference.md) (credential handling) and [`AGENTS.md`](AGENTS.md) (invariants).

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab, choose **Report a vulnerability**, or go to <https://github.com/akiiwa-games/AgentCompass/security/advisories/new>. Describe the affected version, what an attacker needs, and how to reproduce it. Please do not open a public issue for a vulnerability and do not include real credentials in a report.
