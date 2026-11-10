"""Runtime-derived ("auto") bundles from detected models.

Pure functions only: nothing here reads or writes ~/.agent-compass, calls a model, or edits
references/accounts.json. Callers pass the merged accounts document, the model-catalog cache
(tray/model_catalog.py format) and the sparse user overrides.

A discovered name resolves, most specific first, to:
  1. calibrated   - the canonical model has a row in accounts['models'] (exact id or alias).
  2. family       - the same-series row `series_target` names (model_resolution's family resolution). The name has no
                    scores of its own: calibration (calibration/five_axis.py) converts them from that row when the model
                    enters the public data, so until then it is out of automatic routing (rule B) and a named dispatch
                    carries the data error.
  3. unmeasured   - no row and no same-series row: the same, with no series to convert from.
A small-size name (flash/lite/mini/...) is not in the series of a non-small target. No default row, score or price is
supplied here.
"""
import copy
import hashlib
import json
import os
import re
from pathlib import Path

from model_resolution import DIRECT_SOURCES, resolve_canonical_model, resolve_model

AUTO_PREFIX = 'auto:'
STALE_LIMIT_SECONDS = 7 * 24 * 3600
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max')
EFFORT_SUFFIX = re.compile(r'^(?P<base>.+?)-(?P<effort>' + '|'.join(EFFORTS) + r')$')
NON_CHAT = re.compile(r'embed|image|imagen|dall-?e|tts|speech|audio|whisper|transcri|rerank|moderation|veo|video', re.I)
SMALL_NAME = re.compile(r'(?:^|[-_.])(flash|lite|mini|nano|air|small|tiny|haiku|luna)(?:$|[-_.])', re.I)
HARNESS_DEFAULT_EFFORT = {'codex': 'high', 'pi': 'high', 'claude': 'high', 'opencode': None,
                          'antigravity-resilient': None, 'grok': 'high'}
HARNESS_MODE = {'codex': 'full-access', 'claude': 'bypassPermissions',
                'opencode': 'build', 'antigravity-resilient': 'full-access'}
OPENAI_NATIVE = re.compile(r'^(gpt|o\d|codex)', re.I)
ANTHROPIC_NATIVE = re.compile(r'^claude', re.I)


def split_effort(raw, accounts=None):
    """('claude-opus-5-5', 'high') for 'claude-opus-5-5-high'; (raw, None) without an effort suffix.

    '-max' is also a size name (qwen3-max), so with `accounts` it splits only when the base is calibrated.
    """
    name = str(raw or '').strip()
    match = EFFORT_SUFFIX.match(name)
    if not match:
        return name, None
    if match['effort'] == 'max' and accounts is not None:
        known = accounts.get('models') or {}
        if match['base'] not in known and match['base'] not in (accounts.get('aliases') or {}):
            return name, None
    return match['base'], match['effort']


def is_chat_model(raw):
    return not NON_CHAT.search(str(raw or ''))


def series_compatible(name, target):
    """False when one side is a small-size name and the other is not: not the same series."""
    return bool(SMALL_NAME.search(str(name))) == bool(SMALL_NAME.search(str(target)))


def series_target(raw_model, accounts):
    """The same-series row model_resolution's family resolution gives this name (a name that is itself a row resolves
    exactly and has none), or None."""
    raw_models = (accounts.get('models') if isinstance(accounts, dict) and isinstance(accounts.get('models'), dict)
                  else (accounts if isinstance(accounts, dict) else {}))
    known = {k: v for k, v in (raw_models or {}).items() if isinstance(v, dict)}
    aliases = (accounts.get('aliases') if isinstance(accounts, dict) and isinstance(accounts.get('aliases'), dict) else {})
    resolved = resolve_model(raw_model, known, aliases)
    target = resolved['key'] if resolved['source'] == 'family' else None
    base = _name_candidates(raw_model, accounts)[0]
    return target if target and series_compatible(base, target) else None


def _name_candidates(raw_model, accounts):
    """(base, ids to look up most specific first): raw, effort-stripped, alias-resolved, vendor-tag-stripped."""
    aliases = (accounts.get('aliases') if isinstance(accounts, dict) and isinstance(accounts.get('aliases'), dict) else {})
    acct_for_split = accounts if isinstance(accounts, dict) and 'models' in accounts else {'models': accounts if isinstance(accounts, dict) else {}}
    base, _ = split_effort(str(raw_model).rsplit('/', 1)[-1], acct_for_split)
    name = base
    seen = set()
    while name in aliases and name not in seen:
        seen.add(name)
        name = aliases[name]
    stripped = base
    while True:  # vendor date/tag suffixes (@20251001, -20261023, :free, -latest) are not new models
        nxt = re.sub(r'(?:@\d{8}|-\d{8}|:\w+|\[\w+\]|-(?:latest|preview))$', '', stripped)
        if nxt == stripped:
            break
        stripped = nxt
    return base, (str(raw_model), base, name, stripped, aliases.get(stripped, stripped))


def superseded_by(raw_model, accounts):
    """The newer model id public data names for this model, or None (latest, or unknown to the data).

    The only source of a discovered model's default checkbox: `accounts['superseded']` maps an old model id
    to {'by': newer id, 'basis': ...}. Efforts and harnesses of one model share its entry.
    """
    table = accounts.get('superseded') if isinstance(accounts.get('superseded'), dict) else {}
    resolved = resolve_model(raw_model, accounts.get('models') or {}, accounts.get('aliases') or {})
    names = [*_name_candidates(raw_model, accounts)[1], resolved['bare']]
    if resolved['source'] in DIRECT_SOURCES:
        names.append(resolved['key'])
    for candidate in names:
        entry = table.get(candidate)
        if isinstance(entry, dict) and entry.get('by'):
            return entry['by']
    return None


def default_profile(raw_model, accounts):
    """How a model name resolves, with its source: {'model', 'source', 'target', 'basis'}. Never mutates accounts."""
    raw_models = (accounts.get('models') if isinstance(accounts, dict) and isinstance(accounts.get('models'), dict)
                  else (accounts if isinstance(accounts, dict) else {}))
    known = {k: v for k, v in (raw_models or {}).items() if isinstance(v, dict)}
    aliases = (accounts.get('aliases') if isinstance(accounts, dict) and isinstance(accounts.get('aliases'), dict) else {})
    base = _name_candidates(raw_model, accounts)[0]
    resolved = resolve_model(raw_model, known, aliases)
    if resolved['source'] in DIRECT_SOURCES:
        return {'model': resolved['key'], 'source': 'calibrated', 'target': resolved['key'],
                'basis': 'Calibrated row in accounts.models.'}
    target = series_target(raw_model, accounts)
    if target:
        return {'model': base, 'source': 'family', 'target': target,
                'basis': f'Same series as {target}; no scores of its own until calibration converts them from it.'}
    return {'model': base, 'source': 'unmeasured', 'target': None,
            'basis': 'No row and no same-series row: no scores (out of automatic routing).'}


def harness_compatibility(harness, model, account_spec):
    """('ok'|'unverified'|'excluded', reason). Read-only rules; no endpoint is called here.

    Metered proxies (LiteLLM) are driven by Codex CLI over the Responses API. LiteLLM translates
    /v1/responses for non-OpenAI providers, but that bridge has not been verified on this proxy,
    so non-OpenAI models on codex/pi over a proxy are 'unverified' (still derivable; metered
    bundles stay a routing last resort). Claude Code needs an Anthropic Messages endpoint; non-Claude models there are
    excluded unless the account declares `anthropic_compatible: true`.
    """
    spec = account_spec or {}
    if not is_chat_model(model):
        return 'excluded', 'non-chat model (embedding/image/audio)'
    proxy = spec.get('billing') == 'metered' or spec.get('backend_kind') == 'litellm'
    if harness in ('codex', 'pi'):
        if OPENAI_NATIVE.match(model) or not proxy:
            return 'ok', ''
        if spec.get('responses_api_verified') is True:
            return 'ok', 'proxy declares responses_api_verified'
        return 'unverified', 'non-OpenAI model through proxy Responses API bridge (not verified)'
    if harness == 'claude':
        if ANTHROPIC_NATIVE.match(model) or spec.get('anthropic_compatible') is True:
            return 'ok', ''
        return 'excluded', 'Claude Code requires an Anthropic Messages endpoint'
    return 'ok', ''


def _account_harnesses(account, spec):
    """(harness, catalog provider) pairs an account already launches through, from its manual bundles."""
    from allocator import bundle_harness  # local import: allocator is heavy and imports this module's peers
    pairs = {}
    for bid, bundle in (spec.get('bundles') or {}).items():
        if isinstance(bundle, dict):
            pairs.setdefault(bundle_harness(bundle, account, bid), bundle.get('provider') or account)
    if not pairs:
        pairs[PER_MODEL] = account  # proxy / discovered account: harness chosen per model
    return pairs


PER_MODEL = '*'


def proxy_harness(family, model):
    """Harness for a model on an account without manual bundles (the retired proxy synthesis rule)."""
    family = str(family or 'codex')
    if family in ('claude', 'codex'):
        return 'claude' if str(model).startswith('claude-') else 'codex'
    return family


def _draws(spec, model, accounts):
    import window_scope
    windows = spec.get('windows') if isinstance(spec.get('windows'), dict) else {}
    canonical = resolve_canonical_model(model, accounts.get('models') or {}, accounts.get('aliases') or {}) or model
    return [wid for wid, w in windows.items() if window_scope.binds_any((canonical, model), w)]


def _availability(entry, raw, now, seen_at):
    """detected | stale (<7d, still selectable) | expired (>=7d, not selectable)."""
    listed = bool(entry.get('ok')) and any((m.get('id') if isinstance(m, dict) else m) == raw for m in entry.get('models') or [])
    if listed:
        return 'detected', now
    last = seen_at or entry.get('checked_at')
    if last is None or now - last >= STALE_LIMIT_SECONDS:
        return 'expired', last
    return 'stale', last


def auto_bundle_id(account, harness, model, effort=None):
    return f'{AUTO_PREFIX}{account}:{harness}:{model}' + (f'@{effort}' if effort else '')


def derive(accounts, catalog, now, overrides=None, seen=None, families=None):
    """Return {'bundles': {account: {bid: bundle}}, 'excluded': [...], 'seen': {...}}.

    overrides: sparse preferences document ({'accounts': {acc: {'bundles': {bid: {'disabled': bool}}}}}),
    the same shape tray router_data.toggle_harness_model writes.
    seen: {bundle_id: last_detected_epoch}; the returned 'seen' should be persisted by the caller.
    Manual bundles always win for the same (account, harness, canonical base model).
    """
    providers = (catalog or {}).get('providers') or {}
    prefs = ((overrides or {}).get('accounts') or {})
    seen = dict(seen or {})
    out = {'bundles': {}, 'excluded': [], 'seen': seen}

    def base_key(model):
        # Only exact ids/aliases collapse; a family guess keeps its own advertised name.
        return default_profile(model, accounts)['model']

    for account, spec in (accounts.get('accounts') or {}).items():
        if not isinstance(spec, dict):
            continue
        manual = {(h, base_key(b.get('model') or ''), b.get('effort')) for bid, b in (spec.get('bundles') or {}).items()
                  if isinstance(b, dict) for h in [_harness_of(b, account, bid)]}
        manual_models = {(h, m) for h, m, _ in manual}
        metered = spec.get('billing') == 'metered'
        for pair_harness, provider in _account_harnesses(account, spec).items():
            entry = providers.get(provider) or providers.get(account)
            if not isinstance(entry, dict) or entry.get('type') == 'static':
                continue
            for item in entry.get('models') or []:
                raw = item.get('id') if isinstance(item, dict) else item
                if not isinstance(raw, str) or not raw.strip():
                    continue
                if '[' in raw:  # context-window variants (claude-opus-4-6[1m]) are the same model
                    continue
                base, id_effort = split_effort(raw, accounts)
                harness = pair_harness if pair_harness != PER_MODEL else \
                    proxy_harness((families or {}).get(account) or spec.get('family'), default_profile(raw, accounts)['model'])
                profile = default_profile(raw, accounts)
                status, reason = harness_compatibility(harness, profile['model'], spec)
                if status != 'excluded' and not is_chat_model(raw):
                    status, reason = 'excluded', 'non-chat model (embedding/image/audio)'
                key = base_key(raw)
                if status == 'excluded':
                    out['excluded'].append({'account': account, 'harness': harness, 'model': raw, 'reason': reason})
                    continue
                effort = id_effort or HARNESS_DEFAULT_EFFORT.get(harness)
                if (harness, key, effort) in manual or (id_effort is None and (harness, key) in manual_models):
                    continue
                bid = auto_bundle_id(account, harness, key, effort)
                taken = out['bundles'].get(account, {}).get(bid)
                if taken and taken['launch_model'] != raw:
                    bid += '#' + hashlib.sha256(raw.encode()).hexdigest()[:8]
                availability, last = _availability(entry, raw, now, seen.get(bid))
                if availability == 'detected':
                    seen[bid] = now
                model_name = profile['model'] if profile['source'] != 'calibrated' else key
                pref = ((prefs.get(account) or {}).get('bundles') or {}).get(bid) or {}
                # The checkbox default is public data alone: checked unless superseded there. The user's
                # model-management checkbox (pref) overrides it. Metered stays a last resort in routing.
                default_disabled = superseded_by(raw, accounts) is not None or superseded_by(key, accounts) is not None
                # Unchecked only by default, not by the user: naming this model still reaches it.
                by_default = default_disabled and 'disabled' not in pref and availability != 'expired'
                bundle = {
                    'provider': provider, 'harness': harness, 'model': model_name, 'launch_model': raw,
                    **({'effort': effort} if effort else {}),
                    **({'modeId': HARNESS_MODE[harness]} if harness in HARNESS_MODE else {}),
                    'draws': [] if metered and 'spend' not in (spec.get('windows') or {}) else
                             (['spend'] if metered else _draws(spec, key, accounts)),
                    'disabled': bool(pref.get('disabled', default_disabled)) or availability == 'expired',
                    **({'disabled_by_default': True} if by_default else {}),
                    'auto': True, 'billing': 'metered' if metered else 'subscription',
                    'score_source': profile['source'], 'score_basis': profile['basis'],
                    'availability': availability, 'last_detected_at': last,
                    'compatibility': status, **({'compatibility_reason': reason} if reason else {}),
                }
                if availability == 'expired':
                    bundle['discovery_unavailable'] = True
                out['bundles'].setdefault(account, {})[bid] = bundle
    return out


def _harness_of(bundle, account, bid):
    from allocator import bundle_harness
    return bundle_harness(bundle, account, bid)


def merge_into(accounts, derived):
    """Copy of accounts with the auto bundles added. Manual bundles are never replaced."""
    merged = copy.deepcopy(accounts)
    for account, rows in derived['bundles'].items():
        target = merged['accounts'].setdefault(account, {}).setdefault('bundles', {})
        for bid, bundle in rows.items():
            target.setdefault(bid, bundle)
    return merged


# --- Migration from provider_inventory._synthesize_proxy_bundles ids ------------------------------
def _legacy_digest(value):
    return hashlib.sha256(str(value or '').encode()).hexdigest()


def legacy_proxy_ids(account, raw, accounts):
    """Ids the retired proxy synthesis produced for this advertised model (plain and digest-suffixed)."""
    canon = resolve_canonical_model(raw, accounts.get('models') or {}, accounts.get('aliases') or {}) \
        or str(raw).rsplit('/', 1)[-1]
    plain = f"{account}__{canon.replace('-', '_').replace('.', '_')}_high"
    return [plain, f'{plain}_{_legacy_digest(raw)[:8]}']


def legacy_aliases(accounts, derived):
    """{old synthesized id: auto id} for every derived metered bundle."""
    out = {}
    for account, rows in (derived.get('bundles') or {}).items():
        for bid, bundle in rows.items():
            if bundle.get('billing') == 'metered':
                for old in legacy_proxy_ids(account, bundle['launch_model'], accounts):
                    out.setdefault(old, bid)
    return out


def migrate_overrides(overrides, aliases):
    """Copy of the sparse preferences with old-id bundle choices also written under the auto id.

    An explicit choice already stored under the auto id wins. Old keys are kept (harmless, and a
    rollback keeps working); callers may drop them after a one-time persisted migration.
    """
    result = copy.deepcopy(overrides or {})
    for account, spec in (result.get('accounts') or {}).items():
        rows = (spec or {}).get('bundles')
        if not isinstance(rows, dict):
            continue
        for old, new in aliases.items():
            if old in rows and new not in rows and new.startswith(f'{AUTO_PREFIX}{account}:'):
                rows[new] = copy.deepcopy(rows[old])
    return result


def resolve_bundle_id(bid, known_ids, aliases):
    """Ledger continuity lookup: the id itself, else its auto alias, else None (= no continuity)."""
    if bid in known_ids:
        return bid
    new = aliases.get(bid)
    return new if new in known_ids else None


# --- `seen` persistence (last detection time per auto bundle) -------------------------------------
def read_seen(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    rows = value.get('seen') if isinstance(value, dict) else None
    return {k: float(v) for k, v in (rows or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}


def write_seen(path, seen, now=None):
    """Atomic write; prunes entries older than 4x the stale limit so the file cannot grow forever."""
    path = Path(path)
    cutoff = (now if now is not None else max(seen.values(), default=0)) - 4 * STALE_LIMIT_SECONDS
    rows = {k: v for k, v in seen.items() if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps({'seen': rows}, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    tmp.replace(path)
    return rows
