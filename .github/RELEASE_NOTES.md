AgentCompass v0.5.1 shows a dollar API key as the dollars it has spent, releases a worker's quota reservation when the worker ends, and sends one end notification per watched worker. It builds on v0.5.0. Only application code changed, so the tray's "Update AgentCompass" installs the code package.

- **Dollar API accounts.** An API key billed in dollars shows what it has spent.
  - The spend is read only from `GET /key/info` (the LiteLLM key endpoint). No completion request is sent to read it, so reading costs nothing.
  - With a budget the tray shows "spent $X / $Y"; without one it shows "spent $X". There is no reset time, countdown, period or time-zone choice.
  - A 401 or 403 shows no number, and the hover says there is no permission to query usage.
  - Routing treats a budget as a balance that does not recover: the account leaves automatic routing when the remainder falls inside the starter safety margin. An account without a budget stays a metered last resort.
- **A new machine finds self-built API keys.** A Claude or Codex harness configured with its own endpoint and key is found without a network call. The usage poll then reads `/key/info` with the key from that harness's own configuration; the key is never copied into AgentCompass files.
  - A successful read marks the account as metered; a later timeout keeps that category, and only a changed key or endpoint clears it. The spend number always follows the latest read.
  - Redirects are not followed, so a key is never sent to another origin. The system proxy still applies.
  - Two keys on the same proxy are two accounts, each with its own spend. A hand-written account with the same endpoint and key wins.
  - One unreadable harness configuration skips that harness only.
  - A Claude harness that signs in with `ANTHROPIC_AUTH_TOKEN` keeps its subscription quota read until the gateway probe records it as not Magpie; only then is `/key/info` read.
- **Reservations release themselves.** A dispatch's quota reservation is released when its record is written as ended, from the watcher or from the host's agent record. A parent no longer has to clear it before dispatching again, and a worker that resumes after asking a question is counted again.
- **One end notification per watched worker.** A watched worker no longer sends its own final report: the watcher sends one notification that carries the worker's final reply (a reply over 8,000 characters is saved next to the receipt, and the notification gives its path and opening). An undelivered notification stays pending and is sent once at the next read; a continued worker's earlier notification is not sent. Hosts without a watcher keep the worker's own report.
- **One switch per account.** Whether an account takes part in routing is the single switch in the tray. A leftover hidden flag no longer keeps a ticked account out of the candidates, and every exclusion shows its reason.
- **Upgrade checks instead of compatibility code.** Old state shapes are not read silently. `agentcompass doctor` names each one, and `agentcompass setup` removes leftover dollar-account fields and old Usage Float proxy keys without changing an account's switch. A stale field excludes only its own account.
- **Updates.**
  - CLI updates run while sessions stay open. An update the CLI reports but does not apply is shown as needing attention, not as done.
  - A failed release check stops the update instead of reinstalling the cached release.
- **Public reference data.** `agentcompass data --refresh` now validates a fetched set again. Before, the refresh reported an update but the router kept reading the bundled set.
- **Tray.**
  - Opening the panel no longer flashes console windows.
  - A discovered model named like its family has its own checkbox in model management.
  - A model that failed to launch or is held now shows that note even when the account already carries a warning.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.5.1/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
