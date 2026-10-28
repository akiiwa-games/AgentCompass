# Grok Bot external runner

The AgentCompass **Integrations** page includes **Grok Bot CLI** with
**Install/repair**, **Check connection**, and **Usage guide** actions. Install/repair
uses the fixed upstream artifact and scope patch below. It never installs
a global CLI or starts a service. Account enablement remains in **Routing Settings**.
Installed status is separate from gateway connectivity. Entering the page automatically
checks once; refreshing, checking explicitly, or completing installation checks again.
Checks may wake the cloud computer through upstream recovery. There is no periodic
wake timer. The last result and its time remain visible while rechecking; requests
cannot overlap and older callbacks cannot replace newer results. Checks never send
a Bot task. Local installation checks alone do not establish remote availability.
The card exposes only controlled HTTP status/operation, without server payloads or credentials.

Canonical provider/account/model selector: `grok-bot`. The model selector names
the Bot service, not an asserted underlying model. This is a Route Manager external
runner (like Dot), not a Paseo-native provider or an OpenAI-compatible endpoint.
It uses third-party `grok-bot-cli` from the fixed, unreleased upstream PR158 commit
below (base version 0.12.4), with a small dependency-level scope correction.
Authentication and HTTP transport stay in upstream code.
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
python -X utf8 skills/route-agent-work/grok-bot/install_cli.py
python -X utf8 skills/route-agent-work/grok-bot/install_cli.py --check
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
A logged-in native account with readable, valid stored credentials is required.
Opening the cloud-computer preview or keeping an app process running is not required
by the CLI. It obtains a route on demand through upstream `EnsureSandBox`, including
when the optional gateway descriptor is absent. This can wake the remote computer.
Initial authentication, revoked/expired access tokens, and unsupported local secret
formats still require the official authentication flow; no token-refresh service is added.
Native parking/storage policy can remove descriptors. The adapter does not recreate
descriptors, change storage policy, or persist fresh routes/tokens.

## Fixed upstream recovery dependency

`upstream.json` pins [PR158](https://github.com/ScriptedAlchemy/grok-bot-cli/pull/158)
at commit `e76b5a065b6202ee34c337ea62d3a7521d4ecf71`, the public archive SHA256,
and all 29 original artifact files. This is an **unreleased upstream snapshot**, not
an npm upgrade to a new published version. `install_cli.py` verifies the archive and
full artifact tree, applies `dependency_patch.py` to both CLI bundles, verifies
patched hashes, and replaces only its scoped `runtime/` installation. `--archive`
accepts an offline copy of the same pinned archive. `--check` is read-only and
verifies installed identity, not remote health. No upstream install hook is run.
The old npm/v3-only installation is no longer used; a global CLI is never consulted.

Scope contract: native descriptor entries bind an account, but their payloads do
not bind the selected team. The dependency patch therefore obtains a fresh route
using the upstream active-account credential reader and `EnsureSandBox`, instead
of trusting a cached descriptor's team. It snapshots account/team, checks them
after Ensure and before gateway requests, and refuses changes before delivery.
Missing credentials, corrupt teams, unsupported account scopes, and ambiguous
legacy top-level team storage fail honestly; it does not guess or migrate secrets.
The original upstream decryption helpers and destination/redirect checks remain
unchanged. Ensure receives the account access token; gateway calls use only the
returned gateway credential. Explicit upstream environment overrides retain their
upstream semantics and do not get native-account fallback.

Upstream's allowlisted hibernated-read recovery remains bounded to one retry of
that read. `send` resolves the Bot in the same session, then sends once; a write
failure is never replayed. The runner budget is 210 seconds to accommodate the
bounded cold chain and local startup. A timeout after possible send remains
unknown; the owned CLI process tree is cleaned up and the existing receipt is
continued, never blindly resubmitted.
On Windows this uses Route Manager's `pywin32` runtime and an owned Job object.
A small stdin-gated launcher joins the Job before it can start the CLI; cleanup
waits for owned process exit handles even if an intermediate parent exited first.
An unavailable Job setup fails before the CLI is released.

Offline checks:

```powershell
python -X utf8 -m unittest discover -s skills/route-agent-work/grok-bot -p 'test_*.py'
node --test skills/route-agent-work/grok-bot/test-recovery.mjs
```

The Node tests execute the installed upstream modules from both bundles with
synthetic encrypted Windows storage and mocked HTTP. They cover absent descriptor
bootstrap, same-scope recovery, list-417/Ensure/list-success followed by exactly
one send, no write replay, scope drift, and missing/corrupt/legacy credentials.
No app process, preview, native credential read, or real Bot submission is needed.

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

The transport remains the fixed upstream CLI plus the documented scope patch.
Upstream CLI prompt arguments are subject to the Windows
command-line size limit; the adapter diagnoses oversized prompts rather than
silently truncating them. Responses without the agreed final marker remain open
for inspection in the app. No automatic completion callback or remote file
synchronization is provided.
