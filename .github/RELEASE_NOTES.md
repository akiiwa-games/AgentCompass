AgentCompass v0.6.4 stops the spawn gate from blocking calls that launch no worker. It builds on v0.6.3.

- **Claude Code's built-in tool agents are tool use, not a dispatch.** The gate treated every call of Claude Code's `Agent` tool as an unrouted sub-agent launch. Three built-in agents are a host tool or command in agent form and hand no work to a worker, so the gate now lets them through:
  - `web-fetch`: stands in for the WebFetch tool in a session that has none of its own. With the gate installed, such a session could not read a web page at all.
  - `statusline-setup`: what the `/statusline` command runs.
  - `claude-code-guide`: answers questions about Claude Code itself.
- **What stays a launch.** `Explore`, `Plan`, `general-purpose`, `claude`, `fork` and every custom agent still need a route. Codex `spawn_agent` roles and Antigravity `invoke_subagent` types are all delegated work, so those hosts get no exception. A project or user agent definition that takes one of the three names replaces the built-in and carries its own tools: the gate looks for one (every `.claude/agents` from the working directory upwards, and the user's Claude home) and keeps blocking that case.
- **The rules say the same.** The default global rule, the Paseo daemon summary and the dispatch rules now name the exception, so a session that follows the rule text does not refuse a call the gate lets through. `agentcompass setup` (run by the update on a machine that was set up) rewrites the rule blocks. If you keep your own rule overlay, edit it yourself.
- **Asking a launching CLI for help is not a launch.** `orca orchestration worker-start -h` and `codex exec -h` were blocked while `--help` was let through; both spellings now pass.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.6.4/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
