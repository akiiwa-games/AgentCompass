AgentCompass v0.6.1 fixes three things found after v0.6.0: an update could leave the tray closed, an update did not apply setup again, and a task that must look at images could be sent to a model that takes no image input. It builds on v0.6.0. Only application code changed, so the tray's "Update AgentCompass" installs the code package; the installer fixes below take effect the next time the installer itself runs.

- **A visual task needs a model that takes images.** A task the classifier marks `needs_visual` or `needs_gui` now requires image input at every `tool_operation` level. Before, the image-input check applied only at level 3, so such a task with a lower level could go to a model whose public data says it takes no image input.
  - A model that takes no image input leaves the task. A model with no cited modality leaves automatic routing; naming it still launches it, with the data error as a warning.
  - Tasks without the flag are routed as before.
- **Setup follows the update.** On a machine where `agentcompass setup` was run, an update applies it again with the new files: skill links, global rule blocks and an installed gate hook are refreshed, and the hook choice stays as it was. The installer does it before it starts the tray; after a code-package, wheel or Git update the tray does it, and a failure is reported without stopping the restart. A machine that was never set up is not set up by an update.
- **An update never re-admits an account you took out.** `agentcompass setup --yes` answered its own question about an account whose sign-in cannot be verified with "yes" and wrote the account back into the allowed list. Only an answer given at the prompt (or `--disable`) changes that list now.
- **The installer and a running tray.** The installer stops the tray, then waits up to a minute for the tray's and the CLI's program files to be released. If they are not, it changes nothing, says so, and starts the tray again; it exits with code 7. Before, the tray could stay closed after an update that did not finish.
- **Fixes.**
  - `agentcompass doctor` names the right fix for a leftover `route-agent-work` skill link: delete it.
  - The pre-release privacy check finds a denied path however its separators are written (`\`, `\\` or `/`); one machine path in a classifier calibration case was replaced by a neutral one.
- **For contributors.** The fast test suite passes on a hosted Windows runner.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.6.1/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
