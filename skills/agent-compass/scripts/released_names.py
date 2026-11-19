"""Model names a released version resolved through an alias that a later release removed or re-targeted, and the row
that release resolved each to: what the user meant by the name when they wrote it into local state (AGENTS.md: what a
released version wrote for the user is kept).

`V0_7_0` is the diff of the `aliases` tables of `references/accounts.json` between the `v0.7.0` tag and this release:
every name v0.7.0 resolved through an alias to one of its rows that this release resolves otherwise (the alias removed,
the name now a row of its own, or the alias re-targeted). It is read by the upgrade steps of the local files keyed by a
model name (`user_scores.LOCAL_UPGRADES`, schema 3 to 4) and where an auto bundle's v0.7.0 id is replayed
(`auto_bundles.derive`, `as_v0_7_0`).
test_released_names.py checks it against the tag.
"""

V0_7_0 = {
    'claude-fable-5': 'claude-fable-5-2',           # now a row of its own
    'claude-opus-5': 'claude-opus-5-5',             # now a row of its own
    'claude-sonnet-5': 'claude-sonnet-5-5',         # now a row of its own
    'deepseek-flash': 'deepseek-v4.1-flash-free',   # re-targeted to deepseek-v4.1-flash-1017
    'deepseek-v4-flash': 'deepseek-v4.1-flash-free',    # now a row of its own
    'deepseek-v4.1-flash': 'deepseek-v4.1-flash-free',  # now a row of its own
    'gemini-4-pro': 'gemini-4-argon-high',          # alias removed
    'gemini-4-ultra': 'gemini-4-argon-high',        # alias removed
    'glm-5.3-flash': 'glm-5.3-flash-free',          # now a row of its own
    'grok-4': 'grok-4.7',                           # alias removed
    'kimi-k3.1-pro-highspeed': 'kimi-k3.1-pro',     # now a row of its own
    'mimo-v2.6-pro-ultraspeed': 'mimo-v2.6-pro',    # now a row of its own
}


def v0_7_0_row(name):
    """The row v0.7.0 resolved `name` to where this release resolves it otherwise, else None. v0.7.0
    (user_scores.resolve_model_key) read a name as a row, then an alias, then its last `/` segment as a row, then that
    segment as an alias; a name or segment in `V0_7_0` is one it resolved through an alias this release changed. No
    provider-qualified alias of v0.7.0 has a last segment in `V0_7_0` (test_released_names.py), so the segment rule is
    v0.7.0's exactly."""
    leaf = str(name).rsplit('/', 1)[-1]
    return V0_7_0.get(name) or V0_7_0.get(leaf)


def as_v0_7_0(accounts):
    """`accounts` (accounts.json) with every name of `V0_7_0` resolving as v0.7.0 resolved it: an alias of the row it
    meant, not a row or an alias of its own. A name resolved through it (auto_bundles.default_profile: the advertised
    spelling, its last segment, case, effort and vendor suffixes) reaches the row v0.7.0 reached; read where the id
    v0.7.0 gave an auto bundle is replayed (auto_bundles.derive). v0.7.0 and v0.7.1 declared no row of a served name
    (`provider_names`, added after v0.7.1), so the replay declares none: the id v0.7.1 gave a bundle whose name a
    provider or its maker now declares is the id v0.7.0 gave it."""
    models = {k: v for k, v in ((accounts or {}).get('models') or {}).items() if k not in V0_7_0}
    aliases = {**{k: v for k, v in ((accounts or {}).get('aliases') or {}).items() if k not in V0_7_0}, **V0_7_0}
    return {**(accounts or {}), 'models': models, 'aliases': aliases,
            'provider_names': {'statement': 'v0.7.0 and v0.7.1 declared none', 'defaults': {}, 'rows': {}}}


def current_key(name, accounts):
    """The key a name resolves to under `accounts` (accounts.json) the way an auto bundle's id is keyed
    (auto_bundles.derive: a row or an alias collapses to the row; any other name keeps its own advertised spelling)."""
    models = (accounts or {}).get('models') or {}
    aliases = (accounts or {}).get('aliases') or {}
    if name in models:
        return name
    return aliases[name] if aliases.get(name) in models else name
