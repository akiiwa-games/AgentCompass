# Provider Discovery & Quota Telemetry

The allocator reads local provider availability automatically on every allocation.
`accounts.json` supplies model scores, effort choices, disabled-model policy and
capacity estimates; its historical `verified` flags no longer describe this machine.
The [discovery contract](provider-discovery.md) defines binding and degradation.

## Provider sources

- Native Claude/Codex sign-ins, effective account homes and API settings, plus
  Paseo's provider inheritance, environment and explicit enable/disable choices.
- CC Switch: `~/.cc-switch/cc-switch.db`, opened read-only. Stored provider profiles
  match launchers by account identity or endpoint plus key, not name/current flags.
  Switching accounts is observed without editing router configuration. A saved
  profile with no launcher remains listed as `saved`; discovery does not switch
  CC Switch, execute its usage scripts or copy its credentials. Its persisted
  `provider_health.is_healthy=false` pauses the matching account.
- Magpie: local `$XDG_CONFIG_HOME/magpie` (default `~/.config/magpie`), reading
  `providers.json` and the `logins.json` array. Provider/key `off`, account pause,
  hidden or refused sign-in, and disabled backup participation pause matching accounts.
- Product removal: a previously bound entry removed from a successfully read source
  stays paused while the same account is still installed. Restoring the entry or
  switching to a different account automatically clears that removal. Unreadable
  sources do not imply deletion; previously confirmed pauses remain effective.
- Collector: a matching credential's explicit authentication refusal pauses its
  account. General network errors, missing usage and token expiry alone do not.

Magpie portable `data/` is found beside its executable (`.portable` or existing
product data). CC Switch portable `portable.ini` selects `data/app/`; its Tauri
`com.ccswitch.desktop/app_paths.json` selects `app_config_dir_override` when set.
Executable discovery uses PATH and bounded Windows install records. Discovery reads
local files only; it does not connect to Magpie on a VPS or any upstream service.

Additional enabled Claude/Codex Paseo providers inherit the family's known model
policy and explicit permission mode. Multiple launchers for the same identified
subscription share one quota owner. Unknown model families remain visible without
invented capability scores. Explicit host/model restrictions still apply.

```bash
python -X utf8 ~/.codex/skills/agent-compass/scripts/route_judge.py providers
```

The credential-free inventory is published to
`~/.agent-compass/provider-inventory.json`. Alternate installations can specify
`PASEO_HOME`, `CC_SWITCH_HOME`, `MAGPIE_CONFIG_DIR`, `MAGPIE_EXE`, `CC_SWITCH_EXE`, `XDG_CONFIG_HOME` or
`ROUTER_STATE_DIR`; `ROUTER_PROVIDER_HOME` supports isolated host replay/testing.

## Quota sources

1. `~/.usage-float/usage-cache.json`: AgentCompass's existing backend owns collection;
   the router reads cached observations. Freshness follows the account's active/idle
   poll cadence plus grace. A changed credential binding invalidates old quota.
2. `~/.agent-compass/capacity.json` supplies observed consumption rates when fresh.

Known plan changes reuse existing policy capacity estimates. Unknown plans discard
the previous plan's estimates. Token expiry and next billing dates are not treated
as cancellation. Existing explicit subscription expiry policy remains in force.
Missing quota remains unknown; the router does not synthesize full remaining capacity.
An upstream closure becomes visible once a local product or collector records it.

## Host policy and temporary overrides

`~/.agent-compass/host-policy.json` remains the explicit account/judge allowlist.
Temporary priority and expiry rules remain in
`~/.agent-compass/router-overrides.json`, managed by the existing `override` command
and tray menu. Discovery never rewrites either policy or the product configurations.
