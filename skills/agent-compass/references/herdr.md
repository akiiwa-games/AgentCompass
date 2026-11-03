# Herdr core

This is a host integration. Its models and subscriptions remain the existing routed accounts.

Supported API: current [herdr-core](https://github.com/herdrdev/herdr-core), CLI `hc`, also used by the current Herdr App. The older `herdr` TUI has a different API and is not supported by this adapter. Start the host normally and make `hc` available on PATH. Windows preflight uses the non-starting `hc server status`; on Unix the upstream CLI may autostart a core, but the adapter still requires the real parent to be live before creating a worker. It never requests a core restart. The underlying interactive Claude, Codex or Pi executable must also be installed.

Inside an hc terminal the usual command is sufficient:

```sh
agentcompass roll --task "<complete task>"
```

`HC_TERMINAL_ID` is the real parent terminal address, taking precedence over a provider thread and inherited router parent metadata. An actual `PASEO_AGENT_ID` retains Paseo ownership. No extra host or parent flags are needed. `hc run --bg --parent ...` creates one child with an explicit routed permission mode; Codex uses the upstream-required `--no-daemon`. Account environment is resolved inside the child through the existing account adapter. The complete original assignment and appended return contract are retained in the prompt file; a short initial message tells the interactive worker to read that entire file, avoiding OS command-line length limits.

The worker sends questions and `[Worker final report]` with `hc send <parent> <message>`. Core can also notify the parent about a child needing attention or exiting. Do not infer successful work from terminal creation. The returned public `next_argv` collects status; `agentcompass herdr cleanup --request <receipt>` kills/removes only the terminal whose name and parent match that receipt. Children die with their parent; the adapter never requests `--outlive-parent`. It never stops the user's core.

On Windows the supervised wrapper waits for the actual interactive child and propagates its exit code; Unix keeps process replacement. Test environments must verify the resolved executable itself: current hc can refresh PATH and USERPROFILE from the host, so passing isolated environment variables alone does not prove a fixture CLI will run.

Receipts use a process lock and atomic writes. A repeated assignment observes the same child even after completion/cleanup; new work needs a new assignment. A lost run reply stays uncertain and is recovered by the deterministic terminal name through status, never by resending the launch. Status distinguishes working, completed, failed, needs_input and cleaned. Full retained terminal text and RPC read metadata are saved on disk. The API's `complete:false` means history is unavailable or retained only partly: collect the worker's full report artifact rather than treating screen text as complete output.

Verified locally with the official Windows portable **hc 0.1.16**, isolated `HC_SESSION` and XDG paths: real terminal parent binding, send/read, exit and parent-death cleanup. No authenticated model was started in that runtime check. Provider mappings have boundary fixture tests; they are not authenticated provider E2E claims.

Primary sources: [Herdr manual](https://github.com/herdrdev/herdr-core/blob/e07c2a1bedd62cf4787ee026ee23ee79c7d95491/crates/herdr-core/src/SKILL.md), [Herdr API](https://github.com/herdrdev/herdr-core/blob/e07c2a1bedd62cf4787ee026ee23ee79c7d95491/schema/README.md).

## Cindy & Magpie Plugins

- **Cindy ([cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin))**: Raw shell `roll` inside Cindy is refused (`CINDY_INPUT_HANDOFF_ROUTE` is an opaque askpass capability, not a session ID, and Pi strips `CINDY_PI_SESSION_ID` from bash children). Because Cindy manages its own task lifecycle and message history (`cindy.tasks.*`), the standalone `cindy-agentcompass-plugin` calls `agentcompass roll --parent-host native` for routing and prompt packaging, then dispatches and collects results through Cindy's own task API.
- **Magpie ([magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router))**: For per-turn HTTP gateway routing inside Magpie (where Magpie manages request proxying and account pools), use the standalone `magpie-agentcompass-router` plugin.
