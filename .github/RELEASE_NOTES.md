AgentCompass v0.4.4 makes `roll` recover from a failed model by itself. It also holds a failing provider out of dispatch, closes gaps in the dispatch gate, and installs a short pointer into global rule files instead of the full dispatch procedure. It builds on v0.4.3.

- **`roll` retries and switches models by itself.** When the chosen model fails, AgentCompass handles it inside the same dispatch. Failures it covers: the worker could not be created, the model does not exist, the live model list no longer offers it, the worker ended in error, or the provider retried forever or failed the turn (for example with 429 or exhausted quota).
  - Transient failures are retried on the same model after a backoff. The retry is scheduled, so no process sleeps. It waits only as long as `--can-wait-hours` allows.
  - Model, pool or account failures are held, and the brief is relaunched on the next-best candidate. The original allocation is reused, with no new classifier call.
  - An owner-named model is never switched. A task-input failure is reported, not retried.
  - Only one worker runs a brief at a time. The parent keeps one dispatch id and gets a single notification that lists every attempt. `dispatch release` cancels a scheduled retry. `AGENTCOMPASS_ROLL_FALLBACK=off` restores the old behaviour.
- **Provider failures are held out of dispatch.**
  - A worker whose provider keeps retrying without progress is stopped.
  - A turn that fails at its provider is held on the narrowest scope the classifier gives: the model, its quota pool, or the account.
  - Holds back off from 2 minutes and lift after a successful turn, a newer good usage reading, a re-login, the tray clear button, or `agentcompass hold --clear`.
  - A model the provider says does not exist is latched out of routing. Paseo's live model list is checked before ranking.
- **The dispatch gate closes gaps.**
  - Sub-agent launches are recognised behind MCP server prefixes (`mcp__<server>__Agent` and similar).
  - `Workflow` and Paseo schedules get their own authorization:
    - a Workflow needs a same-session `agentcompass record --trigger workflow`;
    - a schedule needs a provider/model that matches a recent `plan` from the same session.
  - Paseo CLI launches are parsed the way the CLI parses them, including global options and `bash -c` / `npx` / `Start-Process` wrappers.
  - Plan, select and record ledger lines now carry the writing session's id. Ledger lines written before this release do not authorize a Workflow or schedule; run `plan` or `record` again.
- **Global rule files carry a pointer.** `agentcompass setup` now writes a short block that says: dispatch only through `agentcompass roll`, follow `next_action`, and load the `agent-compass` skill. The full rules live in the skill's new `references/dispatch-rules.md`, which covers permission modes, the 2M continuation limit, the gate, parent messaging and `record`. A personal `~/.agent-compass/host-rules/dispatch.md` is named by the pointer when present. Re-run `agentcompass setup --yes --hooks` after updating.
- **Tray.**
  - The tray menu shows one product line.
  - The integrations check shares the 6-hour release cache with the AgentCompass update button. A rate-limited GitHub read falls back to the last good release, and installer builds point at "Update AgentCompass".
  - The usage-float mpv player is provisioned after install and update, in the background.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.4/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
