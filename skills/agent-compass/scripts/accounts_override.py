"""Sparse account preferences shared by the tray and allocator; no discovery or disk writes."""
from copy import deepcopy
import json
from pathlib import Path
import re

import auto_bundles
import cwd_policy
import released_names
import state_upgrade

SCHEMA_VERSION = 3


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


def _entry_carries_over(entry):
    """True for an `allowed_cwd` entry whose restriction the regex built from it reproduces exactly: v0.5.0 compared
    path components (`account_fleet.account_cwd_allowed`, tag v0.5.0), the regex compares the normalized directory
    (`cwd_policy.normalized`). They agree for a path with a drive (`D:/work/app`, either slash, any case, a trailing
    separator, `.`, `..` or doubled separators) and for a relative name with no `.` or `..` segment (`work/app`, which
    v0.5.0 looked for anywhere in the directory). They differ for `~` (expanded to the home directory), a path without
    a drive (`/work/app`, resolved against the current drive) and a relative `.` or `..`, so those are not carried."""
    if re.match(r'[A-Za-z]:[\\/]', entry):
        return True
    return (entry[0] not in '~/\\' and ':' not in entry
            and all(part not in ('', '.', '..') for part in re.split(r'[\\/]', entry.rstrip('\\/'))))


def _allowed_cwd_accounts(document):
    """Accounts whose `allowed_cwd` carries over: one that restricts nothing (an empty value is dropped), or a path or
    list of paths next to no `cwd_regex`, every entry of which `_entry_carries_over` (the restriction becomes one)."""
    accounts = document.get('accounts')
    return [name for name, spec in (accounts.items() if isinstance(accounts, dict) else ())
            if isinstance(spec, dict) and isinstance(spec.get('allowed_cwd'), (str, list))
            and (not _prefix_entries(spec['allowed_cwd'])
                 or (not spec.get('cwd_regex') and all(_entry_carries_over(x.strip()) for x in _prefix_entries(spec['allowed_cwd']))))]


def _prefixes_to_regex(entries):
    """The regex that accepts the directories under any of the entries (all of them `_entry_carries_over`)."""
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


def _without_migration_record(document):
    """The v0.2.0 to v0.5.0 tray, when it converted a v1 file, listed the bundles whose `disabled` the old file did not
    state. It applied none of them, so the file without the list is the file those versions ran with."""
    return {key: value for key, value in document.items() if key != 'migration_ambiguous_enabled'}


def _schema_2_to_3(doc):
    """Schema 2 (through v0.7.0): an auto bundle's id holds the row its advertised name resolved to
    (auto_bundles.auto_bundle_id). Where the choice is read (`auto_bundles.choice_for`) a stored model name applies to
    the bundle it resolves to now, which keeps every id whose name resolves now as it did then, or that is the
    advertised name itself. What it cannot see is a name v0.7.0 resolved through an alias this release removed or
    re-targeted (`released_names.V0_7_0`): the v0.7.0 id holds the old row, which still names that row. So the choice
    is also stored under the advertised name, which is resolved where it is read like any other; the v0.7.0 id keeps its
    choice. A v0.7.0 id with a `#hash` suffix was one advertised name's own (the second name of the row on that
    account): it carries only to that name, under the same suffix."""
    names = {}
    for name, row in released_names.V0_7_0.items():
        names.setdefault(row, []).append(name)
    out = deepcopy(doc)
    for spec in (out.get('accounts') or {}).values():
        bundles = spec.get('bundles') if isinstance(spec, dict) else None
        if not isinstance(bundles, dict):
            continue
        for bid, pref in list(bundles.items()):
            parts = auto_bundles.split_id(bid)
            if parts is None or not isinstance(pref, dict) or 'disabled' not in pref:
                continue
            account, harness, row, effort, tag = parts
            for name in names.get(row, ()):
                if tag and '#' + tag != auto_bundles.name_tag(name):
                    continue
                bundles.setdefault(auto_bundles.auto_bundle_id(account, harness, name, effort) + ('#' + tag if tag else ''),
                                   {'disabled': pref['disabled']})
    out['schema_version'] = SCHEMA_VERSION
    return out


# The user's own choices are kept across updates: {the shape an earlier release wrote: (does the document have it, the
# step that carries it into the current shape)}. A shape that cannot be carried without guessing has no step and is
# refused by `validate_document`: a file without `schema_version` (the full copy of the shipped file that v0.1.0
# wrote) and an account whose `allowed_cwd` restricts and sits next to a `cwd_regex`.
LOCAL_UPGRADES = {
    'non-boolean disabled (v0.1.0 to v0.5.0)': (_has_non_boolean_disabled, _disabled_to_boolean),
    'allowed_cwd (v0.1.0 to v0.5.0)': (lambda doc: bool(_allowed_cwd_accounts(doc)), _allowed_cwd_to_regex),
    'migration_ambiguous_enabled (v0.2.0 to v0.5.0)': (lambda doc: 'migration_ambiguous_enabled' in doc,
                                                        _without_migration_record),
    'schema 2 (v0.2.0 to v0.7.0)': (lambda doc: doc.get('schema_version') == 2, _schema_2_to_3),
}


def upgrade_document(doc):
    """`doc` carried into the current shape through `LOCAL_UPGRADES`; a document of another shape is returned as it is
    and fails `validate_document`. Nothing is written here: the file takes the current shape the next time it is saved."""
    if isinstance(doc, dict):
        for has_shape, step in LOCAL_UPGRADES.values():
            if has_shape(doc):
                doc = step(doc)
    return doc


def _delete_fix(path):
    return state_upgrade.fix_delete(path, 'this discards every account choice in it; set them again in the tray')


def _allowed_cwd_refusal(name, spec):
    """(what is wrong, the fix) for an `allowed_cwd` that `LOCAL_UPGRADES` did not carry over."""
    allowed = spec.get('allowed_cwd')
    if spec.get('cwd_regex') and isinstance(allowed, (str, list)) and _prefix_entries(allowed):
        return (f'accounts.{name}.allowed_cwd sits next to a cwd_regex (v0.5.0 applied both)',
                f'edit the file: remove accounts.{name}.allowed_cwd. That widens the account to every directory its '
                f'cwd_regex allows, where v0.5.0 allowed only those that passed both; to keep the narrower restriction, '
                f'write it into cwd_regex first')
    return (f'accounts.{name}.allowed_cwd has an entry that is not carried over without guessing (a ~ path, a path '
            f'without a drive, or a . or .. segment in a relative name)',
            f'edit the file: replace accounts.{name}.allowed_cwd with accounts.{name}.cwd_regex, a regular expression '
            f'over the directory written with forward slashes (for D:/work/app: ^D:/work/app(/|$)); nothing else in the '
            f'file is lost')


def validate_document(doc, path):
    """Raise `ValueError` for a document that is not in the current shape (after `upgrade_document`). Each message
    names a fix that works when followed: delete the file (the tray then writes a new one), or, for an `allowed_cwd` that
    was not carried over, edit the one account."""
    if not isinstance(doc, dict):
        raise ValueError(state_upgrade.format_issue(
            path, 'invalid accounts-override format (expected JSON object)', _delete_fix(path),
        ))
    version = doc.get('schema_version')
    if version != SCHEMA_VERSION:
        raise ValueError(state_upgrade.format_issue(
            path, f'accounts-override schema_version {version!r} has no upgrade step (expected {SCHEMA_VERSION})',
            _delete_fix(path),
        ))
    accounts = doc.get('accounts')
    if accounts is not None:
        if not isinstance(accounts, dict):
            raise ValueError(state_upgrade.format_issue(
                path, 'invalid accounts-override.accounts (expected JSON object)', _delete_fix(path),
            ))
        for name, spec in accounts.items():
            if not isinstance(spec, dict):
                raise ValueError(state_upgrade.format_issue(
                    path, f'invalid accounts-override entry for {name!r} (expected JSON object)', _delete_fix(path),
                ))
            if 'allowed_cwd' in spec:
                raise ValueError(state_upgrade.format_issue(path, *_allowed_cwd_refusal(name, spec)))


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
            target, f'accounts-override unreadable ({exc})', _delete_fix(target),
        )) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError(state_upgrade.format_issue(
            target, f'accounts-override invalid JSON ({exc})', _delete_fix(target),
        )) from exc
    doc = upgrade_document(doc)
    validate_document(doc, target)
    return doc


def set_bundle_choice(document, owner, bundle_id, disabled, folded=()):
    """Store the user's checkbox of one bundle under its id. The stored ids it was read from (`folded`, the bundle's
    `_choice_ids`: auto_bundles.choice_for) go first, wherever they are stored, so the bundle reads this one choice."""
    for spec in (document.get('accounts') or {}).values():
        bundles = spec.get('bundles') if isinstance(spec, dict) else None
        for sid in folded:
            if isinstance(bundles, dict) and sid != bundle_id:
                bundles.pop(sid, None)
    document.setdefault('accounts', {}).setdefault(owner, {}).setdefault('bundles', {}).setdefault(
        bundle_id, {})['disabled'] = bool(disabled)


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
            if auto_bundles.split_id(bid) is not None:
                continue  # an auto bundle's choice is read below, by the name it holds
            target_bundle = (target.get('bundles') or {}).get(bid)
            if target_bundle is None:
                target_bundle = next((entry for account in accounts.get('accounts', {}).values()
                                      for key, entry in account.get('bundles', {}).items()
                                      if key == bid and entry.get('provider') == name), None)
            if isinstance(bundle, dict) and isinstance(target_bundle, dict) and 'disabled' in bundle:
                target_bundle['disabled'] = bundle['disabled']
    choices = auto_bundles.stored_choices(document, accounts)
    for account, target in (accounts.get('accounts') or {}).items():
        bundles = target.get('bundles') if isinstance(target, dict) else None
        if not isinstance(bundles, dict) or account not in choices:
            continue
        by_model = auto_bundles.harness_by_model(bundles)
        for bundle in bundles.values():
            if not isinstance(bundle, dict) or not bundle.get('auto'):
                continue
            chosen, ids, disagree = auto_bundles.choice_for(choices[account], bundle, accounts, by_model)
            if chosen is None:
                continue
            # The user's checkbox replaces the public default; a model the provider stopped listing stays off.
            bundle['disabled'] = chosen or bundle.get('availability') == 'expired'
            bundle.pop('disabled_by_default', None)
            auto_bundles.mark_choice(bundle, ids, disagree)
