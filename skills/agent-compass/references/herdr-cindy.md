# Herdr core and Cindy

These are host integrations. Their models and subscriptions remain the existing routed accounts.

## Herdr core

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

## Cindy first-time setup

The stock shell is insufficient: `CINDY_INPUT_HANDOFF_ROUTE` is an opaque askpass capability, not a session ID, and Pi deliberately strips `CINDY_PI_SESSION_ID` from bash children. The router refuses to guess from recent sessions or send a provider thread ID to Paseo.

Use the installable plugin, whose declared `sessionContext:true` gets the actual parent and local workdir from Cindy. Cindy strips caller-supplied `session_context` before injecting its own value. The plugin's packaged Node worker invokes the same public `agentcompass roll --task - --verbose`; callers supply only the task. The bridge has no private IPC, credentials or model API of its own.

1. Install AgentCompass normally. Build a fresh plugin source directory:

   ```sh
   agentcompass cindy build-plugin --output <new-absolute-directory>
   ```

   Source checkout form: `python -X utf8 <checkout>/agentcompass cindy build-plugin ...`. The generated `router.json` binds this install's public entry. Rebuild after moving/removing that install; the plugin does not ship an embedded router.

2. If Cindy uses a different connection ID for a router account, provide an explicit JSON map at build time with `--account-map <file>`:

   ```json
   [{"routerProvider":"codex","routerAccount":"codex","providerId":"<exact Cindy connection ID>","agentKind":"codex"}]
   ```

   `agentKind` is `cc`, `codex` or `pi`. The mapping is a connection binding, not permission to change the routed account. Without a map only identical account/provider IDs match. Inspect Cindy's actual catalog; do not guess aliases or silently choose a default connection.

3. In Cindy use its official `ghost_forge_pack({dir:"<plugin source>"})`, then install the resulting `.cindy` through the official installation flow. Approve Node, sessionContext and tasks capabilities. The build command itself changes no Cindy settings. In Cindy's plugin task settings, set the work directory to the project being delegated and allow the required permission level. These are setup steps; they are not per-task roll arguments.

4. Delegate through the plugin's `roll` tool:

   ```json
   {"ghost_id":"agentcompass","tool":"roll","args":{"task":"<complete task unchanged>"}}
   ```

   Run this with Cindy's public `ghost_call` tool. The host supplies the context. This bridge entry is required instead of raw shell roll. Unsupported/remote/read-only contexts fail before dispatch.

## Cindy support boundary and completion

The upstream contract reviewed at commit **85428ac8085427e9de062341ba394c9562b20f86** calls `cindy.tasks` **experimental and not yet released**. No released Cindy version is claimed compatible. The plugin requires actual runtime capabilities `models/create/send/get/getRun/listRuns/readMessages/cancel/archive`, `exactRoute:true` and `createPermissionMode:true`. Older hosts without them are unsupported; there is no invented native fallback. No installed Cindy runtime was available for validation here.

On a compatible build the plugin checks exact harness, connection, model, effort and fast support against `tasks.models()`. Fleet bundles usually omit `modeId`; the Python adapter first makes the existing launcher defaults explicit (Claude `bypassPermissions`, Codex `full-access`, Pi's fixed full-tool worker as `full-access`). The plugin maps those supported modes to Cindy's documented `bypassPermissions`; an explicitly different mode is refused rather than widened. After create, it checks `resolvedConfig`, permission, plan state and exact project directory before sending input with the task's `expectedRevision`. A rejected draft is archived through the public API; if that fails the result explicitly provides cleanup. Creating a draft never means launched. Task plus appended return contract must fit Cindy's 32,768-character send limit; oversized tasks are rejected without truncation.

Each assignment has stable create/send request keys and a durable operation receipt. Concurrent calls are serialized. Unknown delivery is observed without resending; a confirmed run is found only within the uniquely created plugin-owned task. An unknown create with no confirmed task ID requires inspection in Cindy, not a new request key. Identical assignments keep their existing operation after completion/cleanup.

For `unknown_create`, the public task list does not expose the create request key, so it cannot identify the possible orphan draft safely. Plugin cleanup requires a confirmed task ID and is unavailable for this receipt. Inspect the plugin's tasks in Cindy and archive the confirmed idle draft there; do not guess from its title or creation time, resubmit the task, or claim automatic cleanup succeeded.

There is **no automatic completion callback**. `roll` returns an exact `next_tool` call to `status`. Status reads `getRun`, paginates `readMessages`, saves every page on disk and selects the exact `outputMessageId` for this run. The shortened summary names the full artifacts and whether collection is complete. A completed host run with no matching output message does not claim a final result is available. Workers return questions as `BLOCKED / needs_input` and their final report as a final assistant reply; the parent collects them through status. The parent session ID is genuine context, not a generally addressable mailbox.

`stop` calls scoped `tasks.cancel`. `stopping` or `reconciling` does not prove the process has stopped; collect until completed, failed, cancelled or interrupted. A status timeout/offline error does not cancel a task. `cleanup` archives only a confirmed inactive plugin-owned task; it preserves history. No promise of parent-death cleanup is made for these ordinary Cindy tasks.

Evidence is official-contract and external-boundary fixtures, including the actual plugin logic page and Node stdio bridge. These are distinct from a running Cindy installation or an authenticated model test.

Primary sources: [Cindy session context](https://github.com/makecindy/cindy/blob/85428ac8085427e9de062341ba394c9562b20f86/apps/desktop/resources/system-skills/cindy-plugin-creator/references/plugin-session-context.md), [Node protocol](https://github.com/makecindy/cindy/blob/85428ac8085427e9de062341ba394c9562b20f86/apps/desktop/resources/system-skills/cindy-plugin-creator/references/plugin-node-worker.md), [ordinary tasks API](https://github.com/makecindy/cindy/blob/85428ac8085427e9de062341ba394c9562b20f86/docs/dev-rules/plugin-task-api-implementation.md), [task slot capabilities and validation](https://github.com/makecindy/cindy/blob/85428ac8085427e9de062341ba394c9562b20f86/apps/desktop/src/main/cindy-brain/taskSlot.ts), [Herdr manual](https://github.com/herdrdev/herdr-core/blob/e07c2a1bedd62cf4787ee026ee23ee79c7d95491/crates/herdr-core/src/SKILL.md), [Herdr API](https://github.com/herdrdev/herdr-core/blob/e07c2a1bedd62cf4787ee026ee23ee79c7d95491/schema/README.md).
