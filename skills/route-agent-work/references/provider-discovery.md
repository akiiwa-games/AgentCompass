# Local provider discovery contract

The router rereads local sign-ins, Paseo launcher bindings, Magpie and CC Switch
before allocation and at the prepared dispatch's launch check. `route_judge.py providers` also refreshes the inventory at
`~/.route-manager/provider-inventory.json`. Discovery performs no network requests
and does not read the VPS connection configuration.

## Availability decision

- Failure: a signed-out, disabled or removed account can otherwise keep receiving
  work because `accounts.json` still says `verified`. Product display names and
  CC Switch's selected profile can differ from the account actually launched.
- Action: exclude the affected account/launcher from new work. Listing, diagnosis,
  repairs, unrelated accounts and already running workers continue.
- Evidence: effective launcher credentials, stable account identity (or exact
  credential binding where no stable identity exists), explicit launcher disable,
  Magpie provider/key `off`, removed built-in subscription `hidden`, and login
  `paused`/`hidden`/`lapsed`/backup `on`, and
  CC Switch's persisted `provider_health.is_healthy`, or a matching local collector
  credential's explicit authentication refusal. A confirmed removal from a
  successfully read product source pauses its previously bound account too.
- Recovery: credentials restored, product status healthy/enabled, or the deleted
  entry restored becomes usable on the next allocation. Changing to a different
  account does not inherit the previous account's pause. No `verified` edit or
  confirmation phrase is needed. Explicit host and disabled-model policy still wins.
- Degradation: malformed/missing product files do not imply account deletion or
  block unrelated native sign-ins. An already confirmed pause survives a temporary
  source read failure. A removed-entry marker persists only for the same account.
  Missing native credentials remain unavailable. Unknown quota stays unknown.

Magpie's `on` controls backup participation; the current native account need not
have it set. A stale Claude `lapsed` credential does not invalidate a refreshed
credential for the same account; explicit account pause still does. CC Switch's
`is_current` and `in_failover_queue` are not generic enable/disable flags.
Magpie's empty model picks restore defaults unless `noModels` explicitly selects
none. Removing only a built-in subscription's custom picks also restores defaults.

## Read and storage boundary

Read Magpie's local `providers.json` and `logins.json`, and CC Switch's SQLite
provider/health tables with `mode=ro` and bounded queries. Resolve default,
portable and product-recorded relocated data directories. Never switch product
selection, execute stored usage scripts, refresh OAuth, copy credentials or call a
model. Secret values only participate in in-memory binding; the published snapshot
contains metadata and one-way fingerprints, not keys, tokens or raw settings.

Saved profiles attach to an existing launcher by actual identity or endpoint/key.
Additional enabled Claude/Codex Paseo launchers inherit the existing family policy
and permission mode. A saved profile without a launcher is visible as `saved`;
it is not an independent concurrent worker account. Explicit launchers advertising
models can be loaded without an account template. Model scores use exact calibrated
rows first, then normalized names, family priors, and a conservative generic
baseline. These defaults are provisional estimates, not benchmark measurements.
Duplicate launchers share one subscription quota owner.

## Grok and advertised model defaults (2026-10-27)

Grok has its own subscription account and `grok47_high` bundle. Local evidence is
an enabled Paseo Grok definition, a Grok CLI/config directory, or an active CC
Switch Grok/GrokBuild profile. Cache metadata alone does not prove a client is
installed. An explicit Paseo disable or bound CC Switch unhealthy state still
excludes it. The official active GrokBuild profile binds to this same account.

Reported Grok window IDs keep quota accounting enabled (weekly window, provisional
1% per worker-hour when no capacity prior exists). Without reported windows Grok
uses `unmetered`, without `unlimited`: no missing-snapshot exclusion and no free
usage bonus. An existing user expiry or disabled bundle still applies.

Dispatch adapter contract change: permit the locally ready Grok/ACP launcher
families to form a Paseo launch request. The previous family list rejected the
configured Grok ACP command before any launch. Missing/unready unfamiliar launchers
remain rejected; recovery is restoring the configured launcher. This changes only
request preparation; existing account, quota and launch authorization checks still
apply. Grok/ACP receives no invented permission mode. Tests pair ready and disabled
launchers, exercise reported/missing quota, and verify no unlimited bonus.

Normalization strips provider prefixes, dated/version tags and known suffixes,
then tries longest known prefixes and family defaults. Scores never replace the
advertised launch ID. Unknown model IDs get the generic 3.3-tier text-only baseline.
Saved CC Switch profiles remain metadata until bound to a real launcher; discovery
never switches the active product account or contacts a gateway.

Plan labels come from current credentials or a matching local collector snapshot.
Known plans reuse policy capacity estimates; unknown plans discard stale estimates.
OAuth expiry and next billing dates do not prove subscription cancellation. Remote
closure that has not yet reached the local product/collector cannot be detected by
this read-only local scan. Existing host cooldown policy covers runtime failures.

## Verification

`test_provider_inventory.py` uses isolated homes and SQLite fixtures, without
starting agents. Each scenario exercises the decision and its recovery or isolation:

- Actual identity vs stale CC Switch selection, read-only files and secret-free output.
- New registered launcher, sign-out, explicit disable and restored credentials.
- Magpie account pause/refusal/hide, backup participation and provider/key disable.
- CC Switch unhealthy/healthy transitions and confirmed profile removal/restoration.
- Collector authentication refusal vs ordinary outage, matching credential and recovery.
- Persisted deletion markers, changed identity and unreadable source after a pause.
- Default/portable/relocated product paths and absence of network/VPS access.
- Claude token rotation preserves account pause but supersedes old credential failure.
- Duplicate quota ownership (including a disabled alias), changed usage binding and unknown plan estimates.
- Corrupt sources leave unrelated sign-ins available; host/model policy still wins.
- Effective secure-storage home and API settings identify the account actually used.

Allocation, judgment, dispatch and host-policy tests isolate discovery from the
machine's live subscriptions. Obsolete documentation of direct OAuth probes and
gateway discovery has been removed: collection belongs to the existing backend.
Explicit model overrides cannot bypass account pause, and a prepared dispatch
rechecks availability immediately before claiming its launch.

Validated 2026-10-26: 104 tests (103 passed, one existing platform skip), 10 routing
documents, live local provider inventory and a CLI allocation dry-run. No agents
were launched. This change adds approximately 1,240 lines including the 18 discovery
scenarios, integration, fixtures and documentation; the adapter has 83 conditional
statements for source parsing, identity binding, availability and policy projection.
The local CC Switch database yielded six profiles. Local Magpie runtime data was
not found at a discovered path; its state transitions were verified with fixtures.
