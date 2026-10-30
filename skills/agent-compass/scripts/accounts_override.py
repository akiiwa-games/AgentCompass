"""Sparse account preferences shared by the tray and allocator; no discovery or disk writes."""
from copy import deepcopy


def normalize(document):
    """Normalize disabled flags in place; settings hold state only, never reasons.

    Every legacy string means disabled, including an empty string. Other old
    non-boolean values retain their truth value as a plain boolean.
    """
    if isinstance(document, dict):
        for key, value in document.items():
            if key == 'disabled':
                document[key] = True if isinstance(value, str) else bool(value)
            else:
                normalize(value)
    elif isinstance(document, list):
        for value in document:
            normalize(value)
    return document


def harness(bundle, account, bundle_id, resolver):
    # Older allocators default unknown providers to Codex. These providers have
    # their own execution channels and must remain separate in the preferences.
    provider = bundle.get('provider') or account
    if not bundle.get('harness') and provider in ('grok', 'grok-bot'):
        return provider
    return resolver(bundle, account, bundle_id)


def preference_account(bundle, owner, accounts):
    """Keep execution profiles visible even when discovery shares their billing quota."""
    provider = bundle.get('provider') or owner
    inventory = (accounts.get('_discovery') or {}).get('accounts') or {}
    profile, quota = inventory.get(provider) or {}, inventory.get(owner) or {}
    specs = accounts.get('accounts') or {}
    profile_family = profile.get('family') or (specs.get(provider) or {}).get('family')
    quota_family = quota.get('family') or (specs.get(owner) or {}).get('family')
    if provider in specs and profile_family and profile_family == quota_family:
        return provider
    return owner


MANUAL_ONLY_BILLING = ('metered', 'unknown')


def manual_only_billing(account, accounts):
    """Billing that keeps an account out of automatic routing by default, else None.

    Only subscription accounts route automatically. Metered (API) and unknown billing
    are explicit-selection only. An account drawing on another account's quota
    (`quota_source`) is judged by that source. A missing billing field is unknown:
    an account entry alone is not consent to automatic routing.
    """
    specs = accounts.get('accounts') or {}
    spec = specs.get(account) or {}
    source = specs.get(spec.get('quota_source')) if spec.get('quota_source') else None
    billing = (source or spec).get('billing') or 'unknown'
    return None if billing == 'subscription' else billing


def merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return normalize(result)


def _delta(value, baseline):
    if not isinstance(value, dict) or not isinstance(baseline, dict):
        return deepcopy(value)
    return {key: _delta(item, baseline.get(key)) for key, item in value.items()
            if key not in baseline or item != baseline[key]}


def migrate(document, baseline):
    """Return v2 without guessing whether an absent legacy disabled flag was a user action.

    Callers that persist this result must back up the original first. Explicit false
    is an enduring enable choice, even if the current baseline is already enabled.
    """
    document = normalize(deepcopy(document))
    if document.get('schema_version') == 2:
        return document
    result = {'schema_version': 2}
    # Locally defined providers are user configuration, not a copy of defaults.
    # In particular a proxy may carry its endpoint/model contract here.
    for key, value in document.items():
        if key not in baseline and key not in ('schema_version', 'accounts', 'models', 'scoring'):
            result[key] = deepcopy(value)
    if isinstance(document.get('scoring'), dict):
        scoring = _delta(document['scoring'], baseline.get('scoring', {}))
        if scoring:
            result['scoring'] = scoring
    for name, spec in (document.get('models') or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get('subjective'), dict):
            old = ((baseline.get('models') or {}).get(name) or {}).get('subjective', {})
            delta = _delta(spec['subjective'], old)
            if delta:
                result.setdefault('models', {})[name] = {'subjective': delta}
    ambiguous = []
    for account, spec in (document.get('accounts') or {}).items():
        if not isinstance(spec, dict):
            continue
        entry = {}
        base = (baseline.get('accounts') or {}).get(account) or {}
        if account not in (baseline.get('accounts') or {}):
            result.setdefault('accounts', {})[account] = deepcopy(spec)
            continue
        for key, value in spec.items():
            if key not in base and key not in ('bundles', 'verified', 'allowed_cwd'):
                entry[key] = deepcopy(value)
        for key in ('verified', 'allowed_cwd', 'cwd_regex'):
            if key in spec and spec[key] != base.get(key):
                entry[key] = deepcopy(spec[key])
        for bid, bundle in (spec.get('bundles') or {}).items():
            if not isinstance(bundle, dict):
                continue
            if 'disabled' in bundle:
                entry.setdefault('bundles', {})[bid] = {'disabled': bundle['disabled']}
            elif ((base.get('bundles') or {}).get(bid) or {}).get('disabled'):
                ambiguous.append({'account': account, 'bundle': bid})
        if entry:
            result.setdefault('accounts', {})[account] = entry
    if ambiguous:
        result['migration_ambiguous_enabled'] = ambiguous
    return result


def apply(accounts, document):
    """Apply only explicit preferences, after live discovery has supplied bundles."""
    normalize(accounts)
    document = normalize(deepcopy(document))
    # Choices saved under the retired proxy ids (`<proxy>__<model>_high`) carry over to their auto bundle;
    # a choice already stored under the auto id wins.
    legacy = (accounts.get('_auto_bundles') or {}).get('legacy_aliases') or {}
    for name, spec in (document.get('accounts') or {}).items():
        target = (accounts.get('accounts') or {}).get(name)
        if not isinstance(spec, dict) or not isinstance(target, dict):
            continue
        if legacy and isinstance(spec.get('bundles'), dict):
            rows = dict(spec['bundles'])
            for old, new in legacy.items():
                if old in rows and new not in rows:
                    rows[new] = rows[old]
            spec = {**spec, 'bundles': rows}
        if spec.get('verified') is False:
            target['verified'] = False
        elif spec.get('verified') is True and target.get('discovery_status') in (None, 'ready'):
            target['verified'] = True
        if 'allowed_cwd' in spec:
            target['allowed_cwd'] = spec['allowed_cwd']
        if 'cwd_regex' in spec:
            target['cwd_regex'] = spec['cwd_regex']
        for bid, bundle in (spec.get('bundles') or {}).items():
            target_bundle = (target.get('bundles') or {}).get(bid)
            if target_bundle is None:
                target_bundle = next((entry for account in accounts.get('accounts', {}).values()
                                      for key, entry in account.get('bundles', {}).items()
                                      if key == bid and entry.get('provider') == name), None)
            if isinstance(bundle, dict) and isinstance(target_bundle, dict) and 'disabled' in bundle:
                target_bundle['disabled'] = bundle['disabled']
                # An explicit enable is the user's opt-in to automatic routing for
                # non-subscription billing (see allocator.evaluate_bundle).
                if bundle['disabled'] is False:
                    target_bundle['user_enabled'] = True
