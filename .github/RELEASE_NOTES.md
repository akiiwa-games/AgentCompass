AgentCompass v0.3.3 makes usage collection independent of Usage Float and improves dispatch, recovery and installation through the public `agentcompass` command. The v0.3.2 attempt was not published: its release checks exposed relative-path and Windows short-path errors in the test harness, corrected here without removing checks.

- The router owns usage collection and caching. Usage Float is optional, and existing usage caches remain readable.
- One public entry handles task text, stdin and brief files, with stage-specific help and complete recovery commands. Documentation and generated host rules use that entry.
- Herdr support adds parent-bound dispatch, durable launch receipts, result collection and cleanup. Windows workers now wait for the actual provider process and preserve its exit status.
- Cindy gains an installable plugin bridge with trusted session context, exact route and permission checks, and durable result collection. It requires the upstream experimental tasks API; compatibility with released Cindy builds is not claimed. An unknown create without a task ID requires inspecting the draft in Cindy rather than resending.
- Unconfirmed Paseo launches return `pending` with a continuation for the same dispatch. Exhausted short quota windows block dispatch even when weekly quota remains; recovery, account aliases and model-specific limits are respected.
- Setup hooks work with spaces and shell metacharacters in paths. Windows uninstall removes the managed environment variables and scheduled task.
- Decision theatre diagnostics are included and remain opt-in. An unwritable diagnostic log no longer prevents the window from loading.
- Distribution packages exclude development tests. Tests now exercise public commands, actual child processes, HTTP and shell boundaries, and installation and GUI operations in Windows Sandbox.

Validation included fresh-home dispatch, lost-response recovery, quota exhaustion and recovery, Windows installation/uninstallation, and a real Quota Monitor refresh click. External provider and host APIs used fixtures where noted; paid model calls and a released Cindy installation were not tested. Release publishing requires the installed-wheel sandbox checks to pass.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.3.3/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
