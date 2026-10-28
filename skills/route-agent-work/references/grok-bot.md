# Grok Bot external runner

Canonical provider/account/model selector: `grok-bot`. The model selector names
the Bot service, not an asserted underlying model. This is a Route Manager external
runner (like Dot), not a Paseo-native provider or an OpenAI-compatible endpoint.
It uses the published third-party `grok-bot-cli` 0.12.4 with the two-condition
descriptor-v3 compatibility backport described below, without an auth fork.
Model quality and cost are unknown; usage-float may supply native quota observations.
A canonical dispatch bundle participates in ordinary automatic routing when
ready and enabled; explicit requests remain supported. The existing
`grok` / Grok Build integration is independent.

Usage-float's `grok_bot_usage.py` reads the official client's `weekly-usage.cache`
for the current native account slot. It preserves the source observation time and
staleness; the official client owns network refresh. Grok Bot has its own display
entry. The adapter does not infer a shared pool or merge it with a local Grok Build
account from a plan label. Observed quota does not establish model quality,
capacity estimates. Routing uses the explicitly provisional profile below.

## Minimal dispatch contract

- Automatic routing: a ready, enabled Bot enters the ordinary candidate pool.
  The service profile uses the router's generic 3.3-tier text baseline as a
  provisional routing assumption, not a claim about its underlying model or
  benchmarks. Normal capability, account and specialist-route policies apply.
  Local-only work, file transfer/application and visual inputs are incompatible;
  an explicit refusal of outbound work excludes this remote worker. Private
  material still requires the caller's existing outbound authorization.
- Native weekly observations use their original freshness and reset. With no
  measured burn rate or USD capacity, consumption remains unknown and keeps the
  ordinary uncertainty penalty. Only fresh active-window observations below the
  existing launch floor block quota admission; stale/missing data is not empty
  or free. Recovery is a refreshed native observation or a compatible provider.
  Native trial observations keep the separate `trial` ID and have no assumed
  cycle. A fresh exhausted trial blocks new work even without a reset timestamp;
  missing timestamps never imply renewal. Stale or unknown usage remains unknown.

- Failure: upstream send can time out after acceptance. Replaying can execute the
  same remote action twice. Persist the receipt before calling send; an existing
  receipt is read back, never resent. Unknown delivery stays unknown until a
  correlated reply is found. Recovery is `status` or inspection in the official
  app; a new task ID means a genuinely new submission, not a retry mechanism.
  A failed post-send receipt write retains the known send result and task ID;
  an unreadable or locked existing receipt retains unknown delivery and its
  continuation. Neither means that the task was definitely not sent.
- The runner sends text to a specifically configured Bot, which executes on its
  remote computer. It does not copy local files or apply returned files. Local-only
  tasks and file-transfer flags are unsupported inputs, diagnosed before send.
- Acceptance is a message ID, not completion. Read the rooted thread, require an
  assistant reply carrying this task's final-response marker, and then return the
  answer. Unrelated, partial or unrecognized entries do not prove completion.
  A marker confirms a text answer, not that every background Bot activity stopped.
- Approval cards and a correlated request for help return `needs_input`; the
  adapter never approves, cancels, changes security settings or starts a relay.
  Resolve approvals in the owning app and continue with `status`.
- Missing configuration/transport is a setup diagnostic. Discovery stays local
  and reads no credentials; live readiness is checked by upstream before send.
- Canonical registration: the production decision registry rejects unknown
  provider/account/model/effort combinations. A configured `grok-bot` registers
  only provider/account/model `grok-bot` with no effort, using the existing account
  bundle catalog. This service ID does not normalize to a Grok model-family alias.
  The ordinary allocator ranks its provisional service profile;
  unknown billing receives no estimated spend or free-usage bonus. Wrong
  model/effort combinations and disallowed host accounts retain their existing
  rejection. Recovery is the documented canonical command/configuration, not a
  validator bypass. Validate the actual record stage with isolated SQLite/ledger
  and mocked transport: `--dry-run` alone does not exercise decision persistence.
  A host-policy exclusion reports `check_host_policy`, the affected account and
  policy path; it is a configuration block, not an unanswered task-permission
  question. Legacy allowlists preserve exclusions; opted-in automatic admission
  follows the [shared discovery contract](provider-discovery.md#automatic-admission-of-newly-discovered-accounts).

## Setup and use

From the repository root (Node >= 22.19.0):

```powershell
npm ci --prefix skills/route-agent-work/grok-bot --ignore-scripts --no-audit --no-fund
npm run --prefix skills/route-agent-work/grok-bot patch:descriptor-v3
python -X utf8 skills/route-agent-work/scripts/run_grok_bot.py doctor
python -X utf8 skills/route-agent-work/scripts/run_grok_bot.py configure --bot-id <exact-bot-id-from-doctor>
python -X utf8 skills/route-agent-work/scripts/route_judge.py providers
python -X utf8 skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours 0.1 --wait 30
```

No provider flags are needed for automatic selection. To honor a user explicitly
requesting Grok Bot, the existing override remains available:

```powershell
python -X utf8 skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours 0.1 --provider grok-bot --account grok-bot --model grok-bot --basis 'User requested Grok Bot' --wait 30
```

Use a dedicated Bot for Route Manager tasks. Configure validates the ID with an
upstream read-only bot listing, then saves only the ID under
`~/.route-manager/grok-bot/config.json`. Authentication remains in upstream/native
desktop handling under the current Windows user. No tokens belong in this config.
If `roll` reports `check_host_policy`, enable only `grok-bot` through Route Manager's
existing local account-admission settings for this authorized integration, keeping
the other account restrictions. `configure` does not edit that policy; subsequent
discovery admits new ready accounts only when automatic admission is enabled.
The app must supply a usable gateway descriptor; installation alone is not enough.
An absent descriptor or upstream multi-entry descriptor refusal requires resolving
the official app/upstream session, not extracting credentials into this adapter.

If already signed in and the descriptor is still absent,
[upstream issue #147](https://github.com/ScriptedAlchemy/grok-bot-cli/issues/147)
reports normally quitting and reopening the app while the cloud computer is awake
as a workaround. Windows 0.77.0 source binds **Ctrl+Q** to `quit_command`, but the
binding alone does not prove that the app exited. If the shortcut has no effect, do not keep
repeating it: first recheck the descriptor and read-only `doctor`, then diagnose
the available normal-exit control if needed. Do not assume a menu or tray entry,
force-kill or automatically restart the app. The descriptor may disappear again
after cloud-computer sleep; do not bypass storage policy or reconstruct credentials.

## Pinned descriptor-v3 compatibility

Grok Bot 0.77.0 writes descriptor version 3, while npm `grok-bot-cli@0.12.4`
accepts only 1/2. We backport exactly the two parser conditions from
[upstream PR #143](https://github.com/ScriptedAlchemy/grok-bot-cli/pull/143),
fixed at [commit e9ac66339d422733a6054ff61795d9841ffe43da](https://github.com/ScriptedAlchemy/grok-bot-cli/commit/e9ac66339d422733a6054ff61795d9841ffe43da):
accept v3 and read its entries using the v2 branch. This changes no decryption,
credential source, account selection, empty-payload or ambiguous-entry rejection;
unknown future versions remain rejected.

The published `dist/bin/gbot.mjs` entrypoint launches `gbot-flight.mjs` for rendered
commands. Both contain the parser, so `patch-descriptor-v3.mjs` patches both. It
records SHA-256 before/after hashes and validates both files before writing. This
prevents applying a known patch to a different bundle; unexpected bytes stop only
the patch operation. Recover by reinstalling the locked dependency and rerunning
the patch, or review a changed upstream version explicitly. Already patched bytes
are accepted unchanged. No installer code reads desktop session files.

**Every `npm ci` restores the unpatched package. Run both setup commands above, in
order.** We intentionally keep `--ignore-scripts`; there is no implicit postinstall
hook. The patch has no network dependency or global CLI dependency. Verify with:

```powershell
npm test --prefix skills/route-agent-work/grok-bot
```

These synthetic tests execute the installed parser from both bundles without
auth/decryption/network access and cover v1/v2/v3, ambiguous/empty entries, empty
payload, future-version rejection, repeat application and unexpected bundle bytes.

`roll` records the explicit user choice through the existing router and returns
`continuation_argv`. Run that exact command to read the answer without resending.
Receipts live under `~/.route-manager/grok-bot/tasks/`. Exit 0 means completed,
3 means pending/unknown/needs_input, and 1 means a setup or rejected-send failure.
Once a correlated final reply proves completion, roll returns `completed: true`,
`launched: true` and `caller_action: completed`, even if the receipt retains
`delivery: unknown` as evidence that the original send acknowledgement was lost.
There is no background watcher: the caller owns continuation and relays questions
to the real parent. A repeated roll with the same parent, task and prompt reuses
the receipt. Use the original receipt if configuration or task text changes.

For a smoke test, put `Reply with ROUTE_MANAGER_GROK_BOT_OK. Do not use tools,
files, connected apps or other side effects.` in a temporary brief and use the
roll command above. Submit once, then use continuation. Do not send repository
contents just to test connectivity.

The transport is the public CLI, not a source fork; `package-lock.json` pins its
registry integrity. Upstream CLI prompt arguments are subject to the Windows
command-line size limit; the adapter diagnoses oversized prompts rather than
silently truncating them. Responses without the agreed final marker remain open
for inspection in the app. No automatic completion callback or remote file
synchronization is provided.
