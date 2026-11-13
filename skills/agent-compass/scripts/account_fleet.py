"""Account and inventory loading: the fleet document, discovery overlay, model identity, bundles and harness capabilities."""

import json
import os
from pathlib import Path
import re

import host_policy
import provider_retry
import provider_inventory
import public_data
import accounts_override
import cwd_policy
from model_resolution import DIRECT_SOURCES, resolve_model as _resolve_model, resolve_canonical_model, split_model_effort


REFERENCES = Path(__file__).resolve().parents[1] / 'references'


def accounts_override_path(environ=None):
    env = os.environ if environ is None else environ
    configured = env.get('AGENTCOMPASS_ACCOUNTS_OVERRIDE')
    if configured:
        return Path(configured).expanduser()
    state_dir = env.get('ROUTER_STATE_DIR')
    if state_dir:
        return Path(state_dir).expanduser() / 'accounts-override.json'
    return Path.home() / '.agent-compass' / 'accounts-override.json'


def fleet_document_path(environ=None):
    """The public accounts.json (the fetched copy when newer than the shipped one). Accounts, plans and capacities
    come from discovery and public data; personal choices live in accounts-override.json. No machine-local fleet
    document exists. ROUTER_ACCOUNTS_FILE pins an explicit document for tests and fixtures only."""
    env = os.environ if environ is None else environ
    configured = (env.get('ROUTER_ACCOUNTS_FILE') or '').strip()
    if configured and Path(configured).expanduser().is_file():
        return Path(configured).expanduser()
    return public_data.effective(REFERENCES / 'accounts.json', environ)


def configured_quota_sources(environ=None):
    """Declared quota pools from the fleet document, without running discovery."""
    try:
        doc = json.loads(fleet_document_path(environ).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return provider_inventory.quota_sources(doc)


def reference_fleet_path():
    """Checked-in fleet fixture for tests. It is not read from the user home."""
    return Path(__file__).resolve().parent / 'fixtures' / 'reference-accounts.json'


def load_accounts(path=None):
    source = Path(path) if path is not None else fleet_document_path()
    accounts = accounts_override.normalize(json.loads(source.read_text(encoding='utf-8-sig')))
    if path is None:
        user_doc = accounts_override.load_override(accounts_override_path())
        if user_doc is not None:
            if isinstance(user_doc.get('scoring'), dict):
                accounts['scoring'] = accounts_override.merge(accounts['scoring'], user_doc['scoring'])
        provider_inventory.apply(accounts)
        if user_doc is not None:
            accounts_override.apply(accounts, user_doc)
            for owner, spec in accounts.get('accounts', {}).items():
                for bid, bundle in (spec.get('bundles') or {}).items():
                    account = accounts_override.preference_account(bundle, owner, accounts)
                    harnesses = (user_doc.get('harness_overrides') or {}).get(account) or {}
                    harness = accounts_override.harness(bundle, owner, bid, bundle_harness)
                    if (harnesses.get(harness) or {}).get('disabled'):
                        bundle['_harness_disabled'] = True
                        bundle['_model_disabled'] = bundle.get('disabled', False)
                        bundle['disabled'] = True
    provider_inventory.apply_shared_quota(accounts)
    try:
        # An account hold from when the account had one pool now holds only its failed models' pools (provider_retry).
        provider_retry.scope_account_holds(accounts)
    except OSError:
        pass
    if path is None:
        try:
            accounts['_host_policy_sync'] = host_policy.sync_discovered(accounts)
        except (OSError, host_policy.HostPolicyError) as exc:
            # Discovery still reports local facts; existing policy checks retain the denial.
            accounts['_host_policy_sync'] = {'mode': 'error', 'added': [], 'error': str(exc)}
    return accounts_override.normalize(accounts)


def account_family(spec):
    if not isinstance(spec, dict):
        return None
    family = spec.get('family')
    if isinstance(family, str) and family.strip():
        return family.strip().removesuffix('-resilient')
    for bundle in (spec.get('bundles') or {}).values():
        if not isinstance(bundle, dict):
            continue
        raw = bundle.get('family') or bundle.get('harness') or bundle.get('provider')
        if isinstance(raw, str) and raw.strip():
            return raw.strip().removesuffix('-resilient')
    return None


def load_capabilities(path=None, accounts=None):
    if path is not None:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    from capability_catalog import compose
    return compose(load_accounts() if accounts is None else accounts)


def models_table(accounts):
    return {k: v for k, v in accounts['models'].items() if isinstance(v, dict)}


def resolve_model(model, accounts):
    """model_resolution.resolve_model against this document: {'key', 'source', 'effort', 'bare'}."""
    return _resolve_model(model, accounts.get('models', {}), accounts.get('aliases', {}))


def canonical(model, accounts):
    return resolve_canonical_model(model, accounts.get('models', {}), accounts.get('aliases', {})) or model


def model_spec(accounts, model):
    """The model row for a name: `exact`/`alias` read the row itself; a name that is not a row (a family guess, an unknown
    name) has none, so it carries no scores and no price (rule B)."""
    resolved = resolve_model(model, accounts)
    value = accounts.get('models', {}).get(resolved['key']) if resolved['source'] in DIRECT_SOURCES else None
    return value if isinstance(value, dict) else {}


def model_leaf(model):
    return split_model_effort(model)[0].rsplit('/', 1)[-1]


def row_key(model, accounts):
    """Which model a row runs: the resolved key for its calibrated id or another spelling of it, its own name for a family
    guess or an unknown model. `canonical` folds a family guess to its target for scoring; two rows are the same model
    only when this says so."""
    resolved = resolve_model(model, accounts)
    return resolved['key'] if resolved['source'] in DIRECT_SOURCES else model_leaf(model).lower()


def lookup_names(model, accounts=None):
    """Names a table may hold this model under: its resolved key, then its raw spellings. A family guess never answers
    for the flagship it folded to, so its key is left out."""
    raw = split_model_effort(model)[0]
    names = [raw, model_leaf(model)]
    if accounts:
        resolved = resolve_model(model, accounts)
        if resolved['source'] in DIRECT_SOURCES:
            names.insert(0, resolved['key'])
    return list(dict.fromkeys(name for name in names if name))


def bundle_harness(bundle, account=None, bundle_id=None, *, family=None):
    """Execution harness family for a bundle (distinct from its billing account or gateway provider)."""
    explicit = str((bundle or {}).get('harness') or '').strip().lower()
    if explicit:
        return explicit
    prov = str((bundle or {}).get('provider') or account or '').strip().lower()
    bid = str(bundle_id or (bundle or {}).get('id') or '').strip().lower()
    launch_m = str((bundle or {}).get('launch_model') or '').strip().lower()
    model = str((bundle or {}).get('model') or '').strip().lower()
    family = family or (prov if prov in ('claude', 'codex', 'pi', 'omp', 'opencode', 'grok', 'grok-bot',
                                         'antigravity', 'antigravity-resilient') else None)
    if family:
        return 'antigravity-resilient' if family == 'antigravity' else family
    if bid.startswith('pi_') or '__pi_' in bid or launch_m.startswith('openai-codex/'):
        return 'pi'
    if launch_m.startswith('opencode/'):
        return 'opencode'
    if prov in ('dot', 'chatgpt-web'):
        return prov
    if 'claude' in model:
        return 'claude'
    return 'codex'


def bundle_rows(accounts, bundle_id):
    """The configured rows of `bundle_id`, one per account that has it, each with its `account` and `bundle_id`. The
    spawn gate resolves a labelled worker and the continuation verdict resolves a dispatched one through this."""
    return [dict(row, account=name, bundle_id=bundle_id)
            for name, spec in ((accounts or {}).get('accounts') or {}).items()
            for row in [(spec.get('bundles') or {}).get(bundle_id)] if isinstance(row, dict)]


def bundles(accounts):
    """Flat list of launchable bundles with their account and execution harness attached."""
    out = {}
    inventory = accounts.get('_discovery') or {}
    for account, spec in accounts['accounts'].items():
        for bundle_id, bundle in spec.get('bundles', {}).items():
            provider = bundle.get('provider', account)
            row = ((inventory.get('launchers') or {}).get(provider)
                   or (inventory.get('accounts') or {}).get(provider)
                   or ((accounts.get('accounts') or {}).get(provider) or {}))
            # `model:effort` is a fixed effort: the name is the model, the suffix the only level this row runs at.
            model_name, fixed = split_model_effort(bundle['model'])
            out[bundle_id] = {'id': bundle_id, 'account': account, 'provider': provider,
                              'harness': bundle_harness(bundle, account, bundle_id, family=row.get('family')),
                              'model': model_name, 'effort': bundle.get('effort') or fixed,
                              **({'effort_fixed': True} if fixed else {}),
                              **{k: bundle[k] for k in ('modeId', 'disabled', 'disabled_by_default', 'efforts', 'launch_model',
                                                        'discovery_unavailable', 'draws',
                                                        'auto', 'billing', 'score_source', 'score_basis',
                                                        'availability', 'compatibility', 'compatibility_reason') if k in bundle}}
    return out


def dispatch_table(provider, accounts):
    """`dispatch_efforts` row for a launch provider. A second account reads its main family's row (claude-2 -> claude)."""
    tables = accounts.get('dispatch_efforts') if isinstance(accounts, dict) else None
    if not isinstance(tables, dict):
        return None
    name = str(provider or '')
    inventory = accounts.get('_discovery') if isinstance(accounts.get('_discovery'), dict) else {}
    spec = (accounts.get('accounts') or {}).get(name)
    for candidate in (name, ((inventory.get('launchers') or {}).get(name) or {}).get('family'),
                      ((inventory.get('accounts') or {}).get(name) or {}).get('family'),
                      account_family(spec) if isinstance(spec, dict) else None,
                      re.sub(r'[-_]\d+$', '', name)):
        if candidate and isinstance(tables.get(candidate), dict):
            return tables[candidate]
    return None


def dispatch_allowed_efforts(bundle, accounts, model=None):
    """Efforts the launch contract offers this bundle's model: None when the provider has no table at all (a gateway).
    A table that does not list the model leaves what the row itself lists, and nothing when it lists none, so a step-down
    never lands on an unverified flag."""
    table = dispatch_table((bundle or {}).get('provider') or (bundle or {}).get('account'), accounts)
    if table is None:
        return None
    raw = (bundle or {}).get('model')
    for name in (lookup_names(raw, accounts) if raw else [model]):
        if table.get(name):
            return tuple(table[name])
    own = (bundle or {}).get('efforts')
    return tuple(own) if isinstance(own, (list, tuple)) else ()


HARNESS_CAPABILITIES = {
    'antigravity-resilient': {
        # No prompt image attachments; but the CLI's own read_file tool shows local image files to multimodal models.
        'accepts_image_input': False,
        'reads_image_files': True,
    },
    'grok-bot': {
        # Remote web bot transport takes prompt text and images on the web channel, but cannot transfer or apply local files.
        'transfers_local_files': False,
    },
}


def harness_transfers_files(harness):
    """True when the execution harness can transfer or apply local files for `--file` / `--apply-to` work."""
    key = 'antigravity-resilient' if harness == 'antigravity' else harness
    cap = HARNESS_CAPABILITIES.get(key)
    return True if not cap else bool(cap.get('transfers_local_files', True))


def harness_accepts_visual(harness, model, needs_attachment=False):
    """True when visual work can reach this model on this harness (manual and auto bundles alike).

    The router dispatches text briefs only; visual workers read local image files themselves, so a task is
    treated as file-based unless the caller says it needs prompt attachments. The model's own multimodality
    is checked separately (models table `visual`)."""
    key = 'antigravity-resilient' if harness == 'antigravity' else harness
    cap = HARNESS_CAPABILITIES.get(key)
    if not cap:
        return True
    if needs_attachment:
        return bool(cap.get('accepts_image_input', True))
    return bool(cap.get('reads_image_files', True) or cap.get('accepts_image_input', True))


def account_cwd_allowed(spec, cwd=None):
    """True when the account has no cwd_regex restriction or when cwd matches cwd_regex."""
    if isinstance(spec, dict) and 'allowed_cwd' in spec:
        raise ValueError('allowed_cwd is no longer supported; use cwd_regex')
    return cwd_policy.matches((spec or {}).get('cwd_regex'), cwd)
