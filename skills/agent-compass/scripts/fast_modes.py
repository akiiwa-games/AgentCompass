"""Verified fast contracts, independent of a model's inferred capability scores.

Sources and billing semantics: ../references/fast-modes.md.
No family-name inference: a new Opus/GPT model needs a verified contract.
"""

# A fast-mode variant is its own candidate in a ranking and carries its base bundle's id plus this suffix, so a bundle id names
# exactly one variant: the plain bundle id the base one, `<id>+fast` the fast one.
ID_SUFFIX = '+fast'


def variant_id(bundle_id):
    return f'{bundle_id}{ID_SUFFIX}'


MODELS = {
    'codex': frozenset(('gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna')),
    'claude': frozenset(('claude-opus-5-5', 'claude-fable-5-2', 'claude-opus-5', 'claude-opus-4-8')),
}


def supported(harness, model, model_info=None):
    return model in MODELS.get(harness, ()) and (model_info or {}).get('fast_capable') is not False


def validate(harness, model, model_info=None):
    if harness not in MODELS:
        raise ValueError(f'Provider {harness!r} does not support fast mode.')
    if not supported(harness, model):
        raise ValueError(f'Model {model!r} does not support fast mode on {harness}.')
    if (model_info or {}).get('fast_capable') is False:
        raise ValueError(f'Model {model!r} has fast_capable=false in effective policy; use normal mode.')


def claude_billing(account):
    return {'subscription': 'usage_credits', 'metered': 'api'}.get(account.get('billing'), 'unknown_paid')
