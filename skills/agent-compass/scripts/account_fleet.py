"""Account and inventory loading: the fleet document, discovery overlay, model identity, bundles and harness capabilities."""

import atexit
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import host_policy
import launch_values
import provider_retry
import provider_inventory
import public_data
import accounts_override
import cwd_policy
from model_resolution import DIRECT_SOURCES, PROVIDER_NAME_STATUSES, resolve_in, split_model_effort


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


def fleet_document(environ=None):
    """The public accounts.json document of the reference set this process holds (public_data.document). Accounts,
    plans and capacities come from discovery and public data; personal choices live in accounts-override.json. No
    machine-local fleet document exists. ROUTER_ACCOUNTS_FILE pins an explicit document for tests and fixtures only."""
    env = os.environ if environ is None else environ
    configured = (env.get('ROUTER_ACCOUNTS_FILE') or '').strip()
    if configured and Path(configured).expanduser().is_file():
        return json.loads(Path(configured).expanduser().read_text(encoding='utf-8-sig'))
    return public_data.document(REFERENCES / 'accounts.json', environ)


def configured_quota_sources(environ=None):
    """Declared quota pools from the fleet document, without running discovery."""
    try:
        doc = fleet_document(environ)
    except (OSError, ValueError):
        return {}
    return provider_inventory.quota_sources(doc)


def reference_fleet_document():
    """The offline reference fleet (`plan --demo`, the acceptance of a reference set), never read from the user home: the
    checked-in fixtures/reference-accounts.json completed with the `families` and `effort_readings` declarations of the
    reference set this process holds, so each has one source and the fleet never pairs one set's declarations with
    another's scores."""
    doc = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'reference-accounts.json').read_text(encoding='utf-8-sig'))
    shipped = public_data.document(REFERENCES / 'accounts.json')
    doc.update({k: shipped[k] for k in ('families', 'effort_readings')})
    return doc


_REFERENCE_FLEET = {}


def reference_fleet_path():
    """reference_fleet_document() written to a temporary file for tests that pin ROUTER_ACCOUNTS_FILE; once per process,
    removed at exit."""
    if 'path' not in _REFERENCE_FLEET:
        folder = Path(tempfile.mkdtemp(prefix='agentcompass-reference-fleet-'))
        atexit.register(shutil.rmtree, folder, True)
        path = folder / 'reference-accounts.json'
        path.write_text(json.dumps(reference_fleet_document(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
                        newline='\n')
        _REFERENCE_FLEET['path'] = path
    return _REFERENCE_FLEET['path']


def load_accounts(path=None, document=None):
    """The routing fleet: the reference document (``document``, the file at ``path``, else fleet_document()) with
    discovery and, only for the fleet document itself, the user's own settings layered on top. The source document is
    reference data (the held set, a candidate set, a test fixture) and is checked against the declared launch patterns
    first (launch_problems). What discovery adds comes from the user's machine and is never dropped by a pattern: each
    launch site makes its values safe (launch_values)."""
    local = path is None and document is None
    if document is not None:
        source = json.loads(json.dumps(document))
    else:
        source = json.loads(Path(path).read_text(encoding='utf-8-sig')) if path is not None else fleet_document()
    problems = launch_problems(source)
    if problems:
        raise public_data.ReferenceDataError(f'accounts.json has values this code cannot launch: {"; ".join(problems[:5])}')
    accounts = accounts_override.normalize(source)
    if local:
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
    if local:
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


def resolve_model(model, accounts, providers=()):
    """model_resolution.resolve_in against this document: {'key', 'source', 'effort', 'bare', 'provider'}. `providers`:
    the endpoints the caller knows serve the name (model_resolution.serving_providers)."""
    return resolve_in(model, accounts, providers)


def canonical(model, accounts, providers=()):
    """The row a name scores as: the resolved key (a family guess folded to its target), else the name itself; a name
    with no row on its serving provider (PROVIDER_NAME_STATUSES) is that provider's own, `<provider>/<name>` as row_key
    gives it."""
    resolved = resolve_in(model, accounts, providers)
    if resolved['source'] in PROVIDER_NAME_STATUSES:
        return f"{resolved['provider']}/{resolved['bare']}".lower()
    return resolved['key'] or model


def account_providers(accounts, account):
    """The serving providers derive read an account's names with (auto_bundles.endpoint_providers, recorded on each
    derived bundle as `_served`): an endpoint account's catalog provider and the account, none for a launcher's list or
    a manual bundle. A model name stored for that account (a temporary priority, a quick reset-priority target) is read
    with them, so a stored name reads what the account's bundles of that name read; on a launcher account it reads as
    with no provider (moved only by a maker's default)."""
    spec = ((accounts or {}).get('accounts') or {}).get(account) or {}
    return tuple(dict.fromkeys(p for b in (spec.get('bundles') or {}).values() if isinstance(b, dict)
                               for p in b.get('_served') or ()))


def bundle_providers(bundle, accounts=None):
    """The serving providers `bundle` reads a bare model name with: `_served` from endpoint derivation, else the
    account's endpoint providers (none for a launcher's list or a manual bundle)."""
    served = (bundle or {}).get('_served')
    if served:
        return tuple(dict.fromkeys(p for p in served if p))
    if (bundle or {}).get('auto') is False:
        return ()
    account = (bundle or {}).get('account')
    return account_providers(accounts, account) if accounts and account else ()


def advertised_model(bundle):
    """The model spelling `bundle` advertises on its provider (no `:effort` suffix)."""
    return split_model_effort((bundle or {}).get('launch_model') or (bundle or {}).get('model') or '')[0]


def stored_form(model, accounts, account):
    """What a writer stores for a model name chosen on `account`: its stored_model when that reads back to itself
    (a row id, a dated snapshot id for a moved name, `<provider>/<name>` for a name with no row there), else the name
    as given (a repository id whose row's own id now reads another row), so the stored value reads its row again."""
    key = stored_model(model, accounts, account)
    return key if stored_model(key, accounts, account) == key else split_model_effort(model)[0]


def stored_model(model, accounts, account):
    """canonical of a model name stored for `account`: a bare name is read as the account's providers serve it
    (account_providers); a `provider/name` already names its provider (as a bundle's qualified model does)."""
    bare = '/' not in split_model_effort(model)[0]
    return canonical(model, accounts, account_providers(accounts, account) if bare else ())


def model_spec(accounts, model, providers=()):
    """The model row for a name: `exact`/`alias`/`provider`/`default` read the row itself; a name that is not a row (a
    family guess, an unknown name, a name with no row on its serving provider) has none, so it carries no scores and no
    price (rule B)."""
    resolved = resolve_model(model, accounts, providers)
    value = accounts.get('models', {}).get(resolved['key']) if resolved['source'] in DIRECT_SOURCES else None
    return value if isinstance(value, dict) else {}


def row_spec(accounts, key):
    """The model row for an already-resolved row key (`canonical`, `row_key`, `stored_model`), without reading `key`
    again as a model name."""
    value = ((accounts or {}).get('models') or {}).get(key) if isinstance(key, str) and key else None
    return value if isinstance(value, dict) else {}


def model_leaf(model):
    return split_model_effort(model)[0].rsplit('/', 1)[-1]


def row_key(model, accounts, providers=()):
    """Which model a row runs: the resolved key for its calibrated id or another spelling of it, its own name for a family
    guess or an unknown model. `canonical` folds a family guess to its target for scoring; two rows are the same model
    only when this says so. A name with no row on its serving provider (PROVIDER_NAME_STATUSES) is that provider's own,
    never the same name elsewhere."""
    resolved = resolve_model(model, accounts, providers)
    if resolved['source'] in PROVIDER_NAME_STATUSES:
        return f"{resolved['provider']}/{resolved['bare']}".lower()
    return resolved['key'] if resolved['source'] in DIRECT_SOURCES else model_leaf(model).lower()


def _pin_spellings(pin, accounts, providers=()):
    """(`(base_or_raw_spellings)`, `named_effort`) for a model pin or advertised model spelling, case-folded."""
    text = str(pin or '').strip()
    spelled, colon_effort = split_model_effort(text)
    if not spelled:
        return (), None
    low = spelled.lower()
    if colon_effort:
        eff = colon_effort.strip().lower()
        return (low, f'{low}-{eff}'), eff
    dash = resolve_model(spelled, accounts, providers)['effort']
    if dash and low.endswith('-' + dash):
        return (low, low[:-len(dash) - 1]), dash
    return (low,), None


def _advertises_pin(bundle, text, spelled, accounts):
    adv = advertised_model(bundle)
    if not adv:
        return False
    b_prov = bundle_providers(bundle, accounts)
    pin_forms, pin_eff = _pin_spellings(text, accounts, b_prov if '/' not in spelled else ())
    if not pin_forms:
        return False
    if adv.lower() in pin_forms:
        return True
    if pin_eff is None:
        adv_forms, _ = _pin_spellings(adv, accounts, b_prov if '/' not in adv else ())
        return any(form in pin_forms for form in adv_forms)
    return False


def pin_matches_identity(pin, bundle, accounts):
    """Whether `pin` names `bundle` by bundle id or by row identity as `bundle` reads names (no prefix test)."""
    from auto_bundles import split_id
    text = str(pin or '').strip()
    if not text or not isinstance(bundle, dict):
        return False
    bid = bundle.get('id') or bundle.get('bundle_id')
    spelled = split_model_effort(text)[0]
    if bid and (text == bid or spelled == bid):
        return True
    if split_id(text) is not None:
        return False
    own = row_key(bundle.get('model') or '', accounts)
    if not own:
        return False
    providers = bundle_providers(bundle, accounts) if '/' not in spelled else ()
    return row_key(text, accounts, providers) == own


def pin_matching_bundles(pin, pool, accounts):
    """Subset of `pool` (order kept) named by `pin`: if `pin` is an auto-bundle id, only that id; else if any bundle in
    `pool` matches by bundle id or advertises the spelling the user typed (effort dropped, case-insensitive), those
    bundles and no other; otherwise every bundle in `pool` matching by row identity as that bundle reads names."""
    from auto_bundles import split_id
    text = str(pin or '').strip()
    items = [b for b in (pool or ()) if isinstance(b, dict)]
    if not text or not items:
        return []
    if split_id(text) is not None:
        return [b for b in items if (b.get('id') or b.get('bundle_id')) == text]
    spelled = split_model_effort(text)[0]
    exact = [
        b for b in items
        if ((bid := (b.get('id') or b.get('bundle_id'))) and (text == bid or spelled == bid))
        or _advertises_pin(b, text, spelled, accounts)
    ]
    if exact:
        return exact
    return [b for b in items if pin_matches_identity(text, b, accounts)]


def _same_provider(bundle_provider, provider):
    if not provider:
        return True
    aliases = {'antigravity', 'antigravity-resilient'}
    if provider in aliases and bundle_provider in aliases:
        return True
    return bundle_provider == provider


def pin_pool(accounts, *, account=None, provider=None, bundle_id=None, source=None, extra=()):
    """Every bundle that exists on the accounts/providers the request addresses (`account` and `provider` when given,
    all accounts when none; `bundle_id` when pinned by id), before any filter of preference (`host_policy`, `disabled`),
    availability (`unavailable`, `discovery_unavailable`, holds, quota), modality or effort. Every call site of
    `pin_matching_bundles` builds its pool through this function so no two sites see different pools for one request."""
    base = list(bundles(accounts).values()) if source is None else [b for b in source if isinstance(b, dict)]
    seen = {(b.get('id') or b.get('bundle_id')) for b in base if (b.get('id') or b.get('bundle_id'))}
    for item in extra or ():
        if not isinstance(item, dict):
            continue
        bid = item.get('id') or item.get('bundle_id')
        if bid and bid in seen:
            continue
        if bid:
            seen.add(bid)
        base.append(item)
    return [
        b for b in base
        if (not account or b.get('account') == account)
        and _same_provider(b.get('provider') or b.get('account'), provider)
        and (not bundle_id or (b.get('id') or b.get('bundle_id')) == bundle_id)
    ]


def pin_names_bundle(pin, bundle, accounts, pool=None):
    """Whether `pin` names `bundle` in `pool` (defaulting to `pin_pool(accounts)` when `pool` is omitted): reads
    `pin_matching_bundles`."""
    if not isinstance(bundle, dict):
        return False
    items = (pin_pool(accounts, extra=(bundle,))
             if (pool is None and isinstance(accounts, dict) and isinstance(accounts.get('accounts'), dict))
             else ((bundle,) if pool is None else pool))
    matched = pin_matching_bundles(pin, items, accounts)
    bid = bundle.get('id') or bundle.get('bundle_id')
    return any((m is bundle) or (bid and (m.get('id') or m.get('bundle_id')) == bid) for m in matched)


def lookup_names(model, accounts=None):
    """Names a table may hold this model under: its resolved key, then its raw spellings. A family guess never answers
    for the flagship it folded to, so its key is left out; a name with no row on its provider never borrows the bare
    name's row."""
    raw = split_model_effort(model)[0]
    if accounts:
        resolved = resolve_model(model, accounts)
        if resolved['source'] in PROVIDER_NAME_STATUSES:
            return list(dict.fromkeys(n for n in (f"{resolved['provider']}/{resolved['bare']}".lower(), raw) if n))
        names = [raw, model_leaf(model)]
        if resolved['source'] in DIRECT_SOURCES:
            names.insert(0, resolved['key'])
        return list(dict.fromkeys(name for name in names if name))
    return list(dict.fromkeys(name for name in (raw, model_leaf(model)) if name))


def bundle_harness(bundle, account=None, bundle_id=None, *, family=None):
    """Execution harness family for a bundle (distinct from its billing account or gateway provider)."""
    explicit = str((bundle or {}).get('harness') or '').strip().lower()
    if explicit:
        return explicit
    prov = str((bundle or {}).get('provider') or account or '').strip().lower()
    bid = str(bundle_id or (bundle or {}).get('id') or '').strip().lower()
    launch_m = str((bundle or {}).get('launch_model') or '').strip().lower()
    model = str((bundle or {}).get('model') or '').strip().lower()
    family = family or (prov if prov in (*CLI_FAMILIES, 'antigravity-resilient') else None)
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
    if not isinstance(accounts, dict) or not isinstance(accounts.get('accounts'), dict):
        return out
    inventory = accounts.get('_discovery') or {}
    for account, spec in accounts['accounts'].items():
        if not isinstance(spec, dict):
            continue
        for bundle_id, bundle in (spec.get('bundles') or {}).items():
            if not isinstance(bundle, dict) or not (bundle.get('model') or bundle.get('launch_model')):
                continue
            provider = bundle.get('provider', account)
            row = ((inventory.get('launchers') or {}).get(provider)
                   or (inventory.get('accounts') or {}).get(provider)
                   or ((accounts.get('accounts') or {}).get(provider) or {}))
            # `model:effort` is a fixed effort: the name is the model, the suffix the only level this row runs at.
            model_name, fixed = split_model_effort(bundle.get('model') or bundle.get('launch_model'))
            out[bundle_id] = {'id': bundle_id, 'account': account, 'provider': provider,
                              'harness': bundle_harness(bundle, account, bundle_id, family=row.get('family')),
                              'model': model_name, 'effort': bundle.get('effort') or fixed,
                              **({'effort_fixed': True} if fixed else {}),
                              **{k: bundle[k] for k in ('modeId', 'disabled', 'disabled_by_default', 'efforts', 'launch_model',
                                                        'discovery_unavailable', 'draws', '_served',
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


# The account families whose bundles the launch code can start (roll.CLI_RUNS, Paseo providers, the Dot and ChatGPT Web
# runners). A reference row of another family, or an effort the family's launch contract does not offer, needs a release.
CLI_FAMILIES = ('claude', 'codex', 'pi', 'omp', 'opencode', 'grok', 'grok-bot', 'antigravity')
LAUNCH_FAMILIES = (*CLI_FAMILIES, 'dot', 'chatgpt-web')
LAUNCH_HARNESSES = (*(f for f in LAUNCH_FAMILIES if f != 'antigravity'), 'antigravity-resilient')
# Harnesses whose launch passes no effort (dispatch_registry, roll.CLI_RUNS): the model id carries it.
EFFORT_IN_MODEL = ('antigravity-resilient',)


def launch_problems(doc):
    """What in a reference accounts document this code cannot launch: values outside the declared launch patterns
    (launch_values), an account family or bundle harness the launch code does not know, or an effort its family's
    launch contract (`dispatch_efforts`) does not offer. A reference set with any of these is not accepted."""
    if not isinstance(doc, dict) or not isinstance(doc.get('accounts'), dict):
        return ['accounts.json has no accounts table']
    found = []
    for table in (doc.get('dispatch_efforts') or {}).values():
        for model, efforts in (table.items() if isinstance(table, dict) else ()):
            found += [p for p in (launch_values.problem('dispatch_efforts model', model, launch_values.MODEL),) if p]
            found += [p for p in (launch_values.problem(f'dispatch_efforts.{model}[]', e, launch_values.NAME)
                                  for e in (efforts if isinstance(efforts, list) else [efforts])) if p]
    for name, row in (doc.get('models') or {}).items():
        found += [p for p in (launch_values.problem('models key', name, launch_values.MODEL),) if p]
        if isinstance(row, dict) and row.get('launch_model') not in (None, ''):
            found += [p for p in (launch_values.problem(f'models.{name}.launch_model', row['launch_model'],
                                                        launch_values.MODEL),) if p]
    for account, spec in doc['accounts'].items():
        found += [p for p in (launch_values.problem('account', account, launch_values.NAME),) if p]
        if not isinstance(spec, dict):
            found.append(f'account {account!r} is not an object')
            continue
        family = account_family(spec) or re.sub(r'[-_]\d+$', '', str(account))
        if family not in LAUNCH_FAMILIES:
            found.append(f'account {account!r} has family {family!r}, which this code does not launch')
        rows = spec.get('bundles') if spec.get('bundles') is not None else {}
        if not isinstance(rows, dict):
            found.append(f'{account}.bundles is not an object')
            continue
        for bundle_id, bundle in rows.items():
            row_problems = launch_values.bundle_problems(account, bundle_id, bundle)
            found += row_problems
            if row_problems:
                continue
            harness = bundle_harness(bundle, account, bundle_id, family=family if family in LAUNCH_FAMILIES else None)
            if harness not in LAUNCH_HARNESSES:
                found.append(f'{account}.bundles.{bundle_id} runs on harness {harness!r}, which this code does not launch')
            effort = bundle.get('effort') or split_model_effort(bundle.get('model') or '')[1]
            offered = dispatch_allowed_efforts({**bundle, 'account': account}, doc)
            if effort and harness not in EFFORT_IN_MODEL and offered is not None and effort not in offered:
                found.append(f'{account}.bundles.{bundle_id} effort {effort!r} is not one the launch contract offers '
                             f'{bundle.get("model")!r} ({", ".join(offered) or "none"})')
    return found


def unaccepted_effort(harness, model, effort, accepted):
    """The refusal of an effort the launch contract does not list for `model` on `harness`: the contract is the fact of
    what the harness accepts, so the reason names it and what it does accept (`accepted`)."""
    from auto_bundles import EFFORTS
    order = {e: i for i, e in enumerate(EFFORTS)}
    listed = sorted({e for e in accepted if e}, key=lambda e: (order.get(e, len(order)), e))
    return f"{effort} is not an effort {harness}/{model} accepts ({', '.join(listed) or 'it takes none'})"


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
