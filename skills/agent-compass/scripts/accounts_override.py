"""Sparse account preferences shared by the tray and allocator; no discovery or disk writes."""
from copy import deepcopy
import json
from pathlib import Path
import re

import cwd_policy
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


def _has_non_boolean_disabled(node):
    if isinstance(node, dict):
        return any((key == 'disabled' and not isinstance(value, bool)) or _has_non_boolean_disabled(value)
                   for key, value in node.items())
    return isinstance(node, list) and any(_has_non_boolean_disabled(value) for value in node)


def _disabled_to_boolean(node):
    """Releases through v0.5.0 read a non-boolean `disabled` as a boolean (a string, even an empty one, meant disabled;
    any other value kept its truth value), and the tray wrote a reason string there."""
    if isinstance(node, dict):
        return {key: (value if isinstance(value, bool) else True if isinstance(value, str) else bool(value))
                if key == 'disabled' else _disabled_to_boolean(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_disabled_to_boolean(value) for value in node]
    return node


def _prefix_entries(allowed):
    return [x for x in ([allowed] if isinstance(allowed, str) else allowed) if isinstance(x, str) and x.strip()]


def _allowed_cwd_accounts(document):
    """Accounts whose `allowed_cwd` carries over: a path or list of paths next to no `cwd_regex` (the restriction
    becomes one), or one that restricts nothing (an empty value is dropped)."""
    accounts = document.get('accounts')
    return [name for name, spec in (accounts.items() if isinstance(accounts, dict) else ())
            if isinstance(spec, dict) and isinstance(spec.get('allowed_cwd'), (str, list))
            and (not spec.get('cwd_regex') or not _prefix_entries(spec['allowed_cwd']))]


def _prefixes_to_regex(entries):
    """Releases through v0.5.0 restricted an account to directories under `allowed_cwd` (a path or a list of paths); the
    tray showed that restriction as this expression and saved it as `cwd_regex` when the user edited the account."""
    patterns = []
    for value in entries:
        path = cwd_policy.normalized(value.strip())
        start = '^' if ':' in path or path.startswith('/') else r'(^|/)'
        patterns.append(start + re.escape(path) + r'(/|$)')
    return patterns[0] if len(patterns) == 1 else '(?:' + '|'.join(patterns) + ')' if patterns else ''


def _allowed_cwd_to_regex(document):
    result = deepcopy(document)
    for name in _allowed_cwd_accounts(document):
        spec = result['accounts'][name]
        regex = _prefixes_to_regex(_prefix_entries(spec.pop('allowed_cwd')))
        if regex:
            spec['cwd_regex'] = regex
    return result


# The user's own choices are kept across updates: {the shape an earlier release wrote: (does the document have it, the
# step that carries it into the current shape)}. A shape that cannot be carried without guessing has no step and is
# refused by `validate_document`: a file without `schema_version` (the full copy of the shipped file that v0.1.0
# wrote), `migration_ambiguous_enabled`, and an account whose `allowed_cwd` restricts and sits next to a `cwd_regex`.
LOCAL_UPGRADES = {
    'non-boolean disabled (v0.1.0 to v0.5.0)': (_has_non_boolean_disabled, _disabled_to_boolean),
    'allowed_cwd (v0.1.0 to v0.5.0)': (lambda doc: bool(_allowed_cwd_accounts(doc)), _allowed_cwd_to_regex),
}


def upgrade_document(doc):
    """`doc` carried into the current shape through `LOCAL_UPGRADES`; a document of another shape is returned as it is
    and fails `validate_document`. Nothing is written here: the file takes the current shape the next time it is saved."""
    if isinstance(doc, dict):
        for has_shape, step in LOCAL_UPGRADES.values():
            if has_shape(doc):
                doc = step(doc)
    return doc


def validate_document(doc, path):
    """Raise `ValueError` for a document that is not in the current shape (after `upgrade_document`)."""
    if not isinstance(doc, dict):
        raise ValueError(state_upgrade.format_issue(
            path, 'older or invalid accounts-override format (expected JSON object)', state_upgrade.FIX_TRAY,
        ))
    version = doc.get('schema_version')
    if version != 2:
        raise ValueError(state_upgrade.format_issue(
            path, f'older accounts-override schema_version {version!r} (expected 2)', state_upgrade.FIX_TRAY,
        ))
    if 'migration_ambiguous_enabled' in doc:
        raise ValueError(state_upgrade.format_issue(
            path, 'older accounts-override field migration_ambiguous_enabled is no longer supported',
            state_upgrade.FIX_TRAY,
        ))
    accounts = doc.get('accounts')
    if accounts is not None:
        if not isinstance(accounts, dict):
            raise ValueError(state_upgrade.format_issue(
                path, 'invalid accounts-override.accounts (expected JSON object)', state_upgrade.FIX_TRAY,
            ))
        for name, spec in accounts.items():
            if not isinstance(spec, dict):
                raise ValueError(state_upgrade.format_issue(
                    path, f'invalid accounts-override entry for {name!r} (expected JSON object)', state_upgrade.FIX_TRAY,
                ))
            if 'allowed_cwd' in spec:
                raise ValueError(state_upgrade.format_issue(
                    path, f'older field accounts.{name}.allowed_cwd cannot be carried over (replaced by cwd_regex)',
                    state_upgrade.FIX_TRAY,
                ))


def load_override(path):
    """Load accounts-override.json in the current shape, carrying a file an earlier release wrote (`upgrade_document`),
    or return None when absent. Raises `ValueError` for a file that is unreadable or of a shape with no upgrade step."""
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
    doc = upgrade_document(doc)
    validate_document(doc, target)
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
