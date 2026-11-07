AgentCompass v0.4.2 leaves every judgement about a task to the classifier model, and tells you when an account can't be read or used instead of quietly routing around it. It builds on v0.4.1.

- **No more classification from task text**: the code no longer reads keywords, patterns or message length to decide what a task is. Every profile field comes from the classifier's answer or from a profile the caller passes.
  - **Two new classifier fields**: `wants_fast` (the task asks for fast mode, urgent turnaround or a pre-reset quota sprint) and `read_only_review` (a review that changes no code, so `code_complexity` must be 0). The calibration set grew to 53 cases.
  - **When the classifier is unavailable**, times out or answers with an invalid profile, `roll` is blocked and says why. It does not fall back to a guess.
- **Each account has one state**, the same in `plan`, `roll`, the tray account list and every warning: `ready`, `plan_inactive`, `usage_failing` or `auth_failed`.
  - **A failed reading shows at once**. The account keeps working on its last good reading for 6 hours, then is excluded until a reading succeeds. An account with no error but no reading for over 2 hours also counts as failing.
  - **A lapsed plan, a 402 or a subscription error code excludes the account at once.** So does a 401 or 403 on the usage API, shown as `auth_failed`.
  - **A launch that reports an inactive subscription** excludes the account until its credentials change, the online plan shows a paid plan, a later launch succeeds, you clear it with `account_state.py --clear <account>`, or 6 hours pass and it is probed again. The account list shows when that retry is due.
  - **Grok usage failures are no longer hidden**: a rejected key, an expired plan or a failed read now show in the account list like Codex and Claude, instead of "ready".
  - **Plans come from each account's own readings**: the usage API's plan outranks the sign-in token's claim, and an account never borrows another account's readings. Plan names are matched in full first, then without a trailing `×N` pool count.
- **Routing follows one contract**, shared with the Magpie and Cindy plugins:
  - **One model resolver**: a family name stands in for a model only when nothing more exact matches, and scores below it.
  - **Step-down stays in range**: a lower effort is chosen only from what the provider dispatches, what the row lists and what you left checked.
  - **Names you give hold**: a named model or effort is kept in every mode, or the route is blocked with a warning. It is never silently replaced.
- **Dispatch**: a worker creation the bridge gave up on now ends from the daemon's own record (`create_failed`) instead of waiting forever. A resume ends only on evidence from the host, not on a 2 minute clock. Only one worker that writes code can be unsettled per workspace.
- **This update installs the full tray installer once.** The runtime fingerprint no longer counts Windows API-set DLLs, so it differs from v0.4.1 and the tray downloads the installer instead of the smaller code package. Later updates use the code package again.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.4.2/packaging/SIGNING.md).

Status: alpha. Customize the reference fleet and model calibration for your own subscriptions.
