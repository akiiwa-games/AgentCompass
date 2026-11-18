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


def _row(model, model_info):
    if not isinstance(model_info, dict):
        raise ValueError(f'fast mode for {model!r} needs its catalog row (account_fleet.model_spec), not {model_info!r}')
    return model_info


def supported(harness, model, model_info):
    """`model_info` is the model's effective catalog row (account_fleet.model_spec)."""
    model_info = _row(model, model_info)
    return (harness in HARNESSES and model_info.get('fast_capable') is True
            and str(model_info.get('api_family') or '').strip().lower() == harness)


def refusal(harness, model, model_info):
    """Why `model` has no fast mode `harness` can launch (the one wording every stage uses), else None. A missing price
    or speed datum is not a refusal: that is the economy's data error (candidate_eval)."""
    if supported(harness, model, model_info):
        return None
    return f'{model} has no verified fast mode on {harness}; run it without --fast'


def price_gap(model, model_info):
    """The data error of a fast mode whose price or speed has no cited public source (the model's effective, overlaid
    catalog row), else None. Not a refusal: a named fast route launches with it (candidate_eval)."""
    fast_info = ((_row(model, model_info).get('five_axis') or {}).get('fast_mode')) or {}
    if fast_info.get('status') == 'ok':
        return None
    return (f"fast_mode has no cited public pricing/speed for {model} "
            f"({fast_info.get('reason') or fast_info.get('status') or 'missing'})")


def validate(harness, model, model_info):
    reason = refusal(harness, model, model_info)
    if reason:
        raise ValueError(reason)


def claude_billing(account):
    return {'subscription': 'usage_credits', 'metered': 'api'}.get(account.get('billing'), 'unknown_paid')
