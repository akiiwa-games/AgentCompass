"""Sparse account preferences shared by the tray and allocator; no discovery or disk writes."""
from copy import deepcopy
import json
from pathlib import Path

import state_upgrade


def normalize(document):
    """Validate disabled flags in place; only boolean values are accepted."""
    if isinstance(document, dict):
        for key, value in document.items():
            if key == 'disabled':
                if not isinstance(value, bool):
                    raise ValueError(f'disabled flag must be a boolean, got {value!r}')
            elif key == 'allowed_cwd':
                raise ValueError('allowed_cwd is no longer supported; use cwd_regex')
            else:
                normalize(value)
    elif isinstance(document, list):
        for value in document:
            normalize(value)
    return document


def load_override(path):
    """Load and validate a v2 accounts-override.json file, or return None when absent."""
    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise ValueError(state_upgrade.format_issue(
            target, f'accounts-override unreadable ({exc})', state_upgrade.FIX_TRAY,
        )) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError(state_upgrade.format_issue(
            target, f'accounts-override invalid JSON ({exc})', state_upgrade.FIX_TRAY,
        )) from exc
    state_upgrade.validate_accounts_override_doc(doc, target)
    return doc


def harness(bundle, account, bundle_id, resolver):
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


def apply(accounts, document):
    """Apply only explicit preferences, after live discovery has supplied bundles."""
    normalize(accounts)
    document = normalize(deepcopy(document))
    for name, spec in (document.get('accounts') or {}).items():
        target = (accounts.get('accounts') or {}).get(name)
        if not isinstance(spec, dict) or not isinstance(target, dict):
            continue
        if spec.get('verified') is False:
            target['_legacy_disabled_flag'] = True
        discovery = ((accounts.get('_discovery') or {}).get('accounts') or {}).get(name) or {}
        if (spec.get('verified') is True and target.get('discovery_status') in (None, 'ready', 'usage_failing')
              and not discovery.get('usage_excluded')):
            target['verified'] = True
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
