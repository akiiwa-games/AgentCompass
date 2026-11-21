AgentCompass v0.7.6 fixes a crash in the setup step of v0.7.5, which an update runs for you.

- **Setup no longer stops while removing a leftover skill copy.** v0.7.5 removes the skill links and Antigravity copies it had made for a skill it no longer ships, and puts back what they had replaced. On a machine whose backup of such a copy held a directory link into an install that no longer exists, putting it back failed and the whole setup stopped there: the gate hook was not refreshed and the setup manifest was not saved (found 2026-11-22 on a machine that had used a checkout before the installer). The tray, the `agentcompass` command and the logon task were not affected. A backup's directory links now come back as links, and one whose target is gone is left out.
- **A link is put back only where its earlier target still exists.** v0.7.5 could re-create a skill link pointing at a folder that no longer exists.
- **A leftover Antigravity copy goes with the version that made it.** A copy under `~/.gemini/skills` is kept only while this version still makes it; before, a copy of a skill that was renamed or removed could stay.
- **A removal that fails is reported, not fatal.** The line names the path and the error, the entry is tried again on the next setup, and the rest of the setup runs.

If you updated to v0.7.5 and your `~/.agent-compass/installer-setup.log` ends in a traceback, updating to v0.7.6 runs the setup again and completes it.

Verified: the tests of the setup folder, including one that rebuilds the failing machine's state and fails on v0.7.5, and the Windows Sandbox installer check. Not verified: the full suite was not run again for this fix, and the sandbox guest has no leftover of an earlier install, so it does not reach the failing path; the machine where the crash was found is updated after this release.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass-Router/blob/v0.7.6/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
