"""Verified fast contracts, independent of a model's inferred capability scores.

Sources and billing semantics: ../references/fast-modes.md.
No family-name inference: a model is fast-capable only where its catalog row declares `fast_capable: true`, and only
on the harness of its `api_family` (the CLI whose fast launch setting is implemented here).
"""

# A fast-mode variant is its own candidate in a ranking and carries its base bundle's id plus this suffix, so a bundle id names
# exactly one variant: the plain bundle id the base one, `<id>+fast` the fast one.
ID_SUFFIX = '+fast'


def variant_id(bundle_id):
    return f'{bundle_id}{ID_SUFFIX}'


# Harnesses with an implemented fast launch setting (Codex service tier, Claude Code fastMode): a CLI protocol fact.
HARNESSES = ('codex', 'claude')


def supported(harness, model, model_info):
    """`model_info` is the model's effective catalog row (account_fleet.model_spec)."""
    return (harness in HARNESSES and model_info.get('fast_capable') is True
            and str(model_info.get('api_family') or '').strip().lower() == harness)


def validate(harness, model, model_info):
    if harness not in HARNESSES:
        raise ValueError(f'Provider {harness!r} does not support fast mode.')
    if model_info.get('fast_capable') is not True:
        raise ValueError(f'Model {model!r} does not declare fast_capable=true in effective policy; use normal mode.')
    if not supported(harness, model, model_info):
        raise ValueError(f'Model {model!r} does not support fast mode on {harness}.')


def claude_billing(account):
    return {'subscription': 'usage_credits', 'metered': 'api'}.get(account.get('billing'), 'unknown_paid')
