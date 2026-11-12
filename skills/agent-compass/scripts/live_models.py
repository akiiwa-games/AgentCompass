"""Live Paseo provider model lists cached briefly before ranking and launch.

Contract:
- Before ranking (and in provider discovery), candidate Paseo providers are queried through the Paseo dispatch bridge
  (`operation: 'provider_models'`, which calls `client.listProviderModels(provider)`).
- Readings are cached in `<state_dir>/paseo-live-models.json` (and in memory) with a 60-second TTL (`TTL_SECONDS = 60`).
  Why 60s: a single `roll` runs `plan` and `prepare` (and batch `roll` runs multiple briefs back-to-back), so 60s avoids
  repeating bridge calls within a roll while still picking up upstream provider catalog changes within one minute.
- Model-id matching is strictly structural: exact model `id` and Paseo `aliases` (with structural `<provider>/<id>`
  prefix normalization), never substring or keyword rules.
- When a provider's live list is readable (`ok: True`) and omits a bundle's launch model, the bundle is excluded with
  `"provider no longer offers <model>"` and surfaced in `plan`/`roll` warnings and tray account cards.
- When a provider's live list cannot be read (`ok: False`, e.g. Paseo down or provider disabled), no bundle is excluded
  on that basis; `"model list unavailable"` is surfaced in warnings and tray account cards, falling back to existing
  routing and the `model_latch` / `provider_retry` holds.
- Which providers are Paseo's is Paseo's own provider snapshot, not a name list here: every candidate bundle's provider
  is asked, and one the snapshot does not list comes back `status: unknown_provider` (e.g. a LiteLLM proxy account),
  which gives neither a warning nor an exclusion.
- Only `roll`/`plan` (ranking) refresh the cache. The tray only reads it and refreshes it off the Tk thread.
- A refresh merges into the file under its lock; `invalidate()` stamps the provider so a refresh whose fetch began
  before the invalidation does not write the evicted list back.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import override_store

FILE = 'paseo-live-models.json'
# 60s TTL: covers `plan` -> `prepare` -> `claim` and batch rolls without adding bridge latency per step, while
# refreshing within a minute when a provider's live catalog changes.
TTL_SECONDS = 60
# A bridge round trip takes ~0.5 s here; a hung Paseo must not hold a roll long, and an unread list never excludes.
FETCH_TIMEOUT_SECONDS = 3
# The bridge's status for a provider Paseo's snapshot does not list (not a Paseo provider): no warning, no exclusion.
UNKNOWN_PROVIDER = 'unknown_provider'

_MEM_CACHE: dict[str, dict] = {}


def ttl_seconds() -> int:
    raw = os.environ.get('AGENTCOMPASS_LIVE_MODELS_TTL_SECONDS', '').strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return TTL_SECONDS


def state_dir() -> Path:
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass')


def store_path(base_dir: Path | None = None) -> Path:
    override = os.environ.get('ROUTER_LIVE_MODELS', '').strip()
    if override and override.lower() not in ('off', '0', 'false', 'on', '1', 'true'):
        return Path(override)
    return (base_dir or state_dir()) / FILE


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def launch_model(bundle: dict | None) -> str:
    if not isinstance(bundle, dict):
        return ''
    val = bundle.get('launch_model') or bundle.get('model') or ''
    return val.strip() if isinstance(val, str) else ''


def bundle_provider(bundle: dict | None, account: str | None = None) -> str:
    if not isinstance(bundle, dict):
        return (account or '').strip()
    val = bundle.get('provider') or bundle.get('account') or account or ''
    return val.strip() if isinstance(val, str) else ''


def is_paseo_bundle(bundle: dict | None, account: str | None = None, spec: dict | None = None) -> bool:
    """A bundle whose provider is asked. Whether Paseo knows that provider is the bridge's answer, not decided here."""
    return isinstance(bundle, dict) and bool(bundle_provider(bundle, account))


def is_unknown_provider(entry: dict | None) -> bool:
    return isinstance(entry, dict) and entry.get('status') == UNKNOWN_PROVIDER


def candidate_providers(accounts: dict | None, allowed=None) -> list[str]:
    """Sorted Paseo provider ids referenced by active candidate accounts."""
    if not isinstance(accounts, dict):
        return []
    acc_map = accounts.get('accounts')
    if not isinstance(acc_map, dict):
        return []
    found: set[str] = set()
    for acct, spec in acc_map.items():
        if not isinstance(spec, dict) or spec.get('enabled') is False:
            continue
        if allowed is not None and not allowed(acct):
            continue
        if spec.get('discovery_status') in ('not_installed', 'missing_credentials', 'disabled'):
            continue
        if spec.get('verified') is False:
            continue
        bundles = spec.get('bundles')
        if not isinstance(bundles, dict):
            continue
        for bundle in bundles.values():
            if not isinstance(bundle, dict) or not _bundle_enabled(bundle):
                continue
            if bundle.get('discovery_unavailable'):
                continue
            if not is_paseo_bundle(bundle, acct, spec):
                continue
            prov = bundle_provider(bundle, acct)
            if prov:
                found.add(prov)
    return sorted(found)


def _structural_ids(provider: str, value) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    item = value.strip()
    if not item:
        return ()
    prov = provider.strip() if isinstance(provider, str) else ''
    prefix = f'{prov}/' if prov else ''
    if prefix and item.startswith(prefix):
        bare = item[len(prefix):].strip()
        return (item, bare) if bare else (item,)
    if prefix:
        return (item, f'{prefix}{item}')
    return (item,)


def offered_model_ids(provider: str, provider_entry: dict | None) -> frozenset[str]:
    """Structural set of exact `id` and Paseo `aliases` values offered by `provider_entry`."""
    if not isinstance(provider_entry, dict) or provider_entry.get('ok') is not True:
        return frozenset()
    models = provider_entry.get('models')
    if not isinstance(models, list):
        return frozenset()
    offered: set[str] = set()
    for item in models:
        if isinstance(item, str):
            offered.update(_structural_ids(provider, item))
        elif isinstance(item, dict):
            offered.update(_structural_ids(provider, item.get('id')))
            for alias_id in item.get('aliases') or ():
                offered.update(_structural_ids(provider, alias_id))
    return frozenset(offered)


def offers_model(provider: str, model_id: str, provider_entry: dict | None) -> bool:
    """True iff `model_id` structurally matches an exact `id` or Paseo `aliases` entry."""
    candidates = set(_structural_ids(provider, model_id))
    if not candidates:
        return False
    return bool(candidates & offered_model_ids(provider, provider_entry))


def _default_fetch_from_paseo(providers: list[str], cwd: str | None = None, timeout: int = FETCH_TIMEOUT_SECONDS) -> dict:
    import dispatch_flow
    payload: dict = {'operation': 'provider_models', 'providers': list(providers), 'timeout_seconds': timeout}
    if cwd:
        payload['cwd'] = cwd
    return dispatch_flow.host_call(payload)


fetch_from_paseo = _default_fetch_from_paseo


def enabled() -> bool:
    router_flag = os.environ.get('ROUTER_LIVE_MODELS', '').strip().lower()
    if router_flag in ('off', '0', 'false'):
        return False
    if router_flag:
        return True
    if fetch_from_paseo is not _default_fetch_from_paseo:
        return True
    if os.environ.get('AGENTCOMPASS_LIVE_MODELS', '').strip().lower() in ('off', '0', 'false'):
        return False
    return True


def _bundle_enabled(bundle: dict) -> bool:
    """A bundle the user unchecked is `disabled: True` at runtime (`enabled: False` in raw specs); a
    disabled-by-default bundle still launches when named, so it keeps its live check."""
    if bundle.get('enabled') is False:
        return False
    return not (bundle.get('disabled') and not bundle.get('disabled_by_default'))


def _read_store(path: Path) -> dict:
    mem = _MEM_CACHE.get(str(path))
    try:
        stat = path.stat()
    except OSError:
        _MEM_CACHE.pop(str(path), None)
        return {'providers': {}}
    if mem and mem.get('_mtime_ns') == stat.st_mtime_ns:
        return copy.deepcopy(mem['doc'])
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {'providers': {}}
    if not isinstance(raw, dict) or not isinstance(raw.get('providers'), dict):
        return {'providers': {}}
    _MEM_CACHE[str(path)] = {'_mtime_ns': stat.st_mtime_ns, 'doc': copy.deepcopy(raw)}
    return raw


def _write_store(path: Path, doc: dict) -> None:
    """Atomic replacement; the caller holds `override_store.locked(path)` across its read-modify-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    override_store.write(path, doc)
    try:
        stat = path.stat()
        _MEM_CACHE[str(path)] = {'_mtime_ns': stat.st_mtime_ns, 'doc': copy.deepcopy(doc)}
    except OSError:
        _MEM_CACHE.pop(str(path), None)


def _entry_fresh(entry: dict | None, now: datetime, ttl: int) -> bool:
    if not isinstance(entry, dict) or 'ok' not in entry:
        return False
    fetched = _parse_time(entry.get('fetched_at'))
    if fetched is None:
        return False
    return (now - fetched) < timedelta(seconds=ttl)


def _normalize_entry(raw_entry: dict | None, fetched_at_iso: str, fallback_error: str = 'model_list_unavailable') -> dict:
    if not isinstance(raw_entry, dict):
        return {
            'ok': False,
            'status': 'unavailable',
            'error': fallback_error,
            'fetched_at': fetched_at_iso,
            'models': [],
        }
    if raw_entry.get('ok') is True and isinstance(raw_entry.get('models'), list):
        models = []
        for item in raw_entry['models']:
            if isinstance(item, str) and item.strip():
                models.append({'id': item.strip()})
            elif isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'].strip():
                normalized = {'id': item['id'].strip()}
                aliases = [a.strip() for a in (item.get('aliases') or ()) if isinstance(a, str) and a.strip()]
                if aliases:
                    normalized['aliases'] = aliases
                if isinstance(item.get('label'), str) and item['label'].strip():
                    normalized['label'] = item['label'].strip()
                models.append(normalized)
        return {
            'ok': True,
            'status': str(raw_entry.get('status') or 'ready'),
            'fetched_at': str(raw_entry.get('fetched_at') or fetched_at_iso),
            'models': models,
        }
    return {
        'ok': False,
        'status': str(raw_entry.get('status') or 'unavailable'),
        'error': str(raw_entry.get('error') or fallback_error),
        'fetched_at': str(raw_entry.get('fetched_at') or fetched_at_iso),
        'models': [],
    }


def read_cached(*, base_dir: Path | None = None, now: datetime | None = None, allow_stale: bool = False) -> dict:
    """Read cached live-model entries without querying Paseo."""
    path = store_path(base_dir)
    if not path.exists():
        return {'providers': {}}
    doc = _read_store(path)
    if allow_stale:
        return doc
    now = now or datetime.now(timezone.utc)
    ttl = ttl_seconds()
    fresh = {
        prov: entry
        for prov, entry in (doc.get('providers') or {}).items()
        if _entry_fresh(entry, now, ttl)
    }
    return {'updated_at': doc.get('updated_at'), 'ttl_seconds': ttl, 'providers': fresh}


def load_or_refresh(
    providers: list[str] | None = None,
    *,
    accounts: dict | None = None,
    allowed=None,
    base_dir: Path | None = None,
    now: datetime | None = None,
    force: bool = False,
    fetch=None,
) -> dict:
    """Return live model lists for `providers` (or candidate providers in `accounts`), refreshing stale entries."""
    if not enabled() and fetch is None:
        return {'providers': {}}
    now = now or datetime.now(timezone.utc)
    ttl = ttl_seconds()
    path = store_path(base_dir)
    doc = _read_store(path)
    stored_providers = doc.get('providers') if isinstance(doc.get('providers'), dict) else {}
    target_providers = list(dict.fromkeys(
        providers if providers is not None else candidate_providers(accounts, allowed=allowed)
    ))
    if not target_providers:
        return {'updated_at': doc.get('updated_at'), 'ttl_seconds': ttl, 'providers': dict(stored_providers)}
    missing = [
        prov for prov in target_providers
        if force or not _entry_fresh(stored_providers.get(prov), now, ttl)
    ]
    if not missing:
        return {
            'updated_at': doc.get('updated_at'),
            'ttl_seconds': ttl,
            'providers': {prov: stored_providers[prov] for prov in target_providers if prov in stored_providers},
        }
    fetcher = fetch or fetch_from_paseo
    ts = now.isoformat()
    started = datetime.now(timezone.utc)  # wall clock, as invalidate() stamps it
    try:
        reply = fetcher(missing)
    except Exception:
        reply = {'ok': False, 'error': 'host_connection_failed'}
    fetched: dict[str, dict] = {}
    if isinstance(reply, dict) and reply.get('ok') is True and isinstance(reply.get('providers'), dict):
        raw_map = reply['providers']
        for prov in missing:
            raw_entry = raw_map.get(prov)
            if isinstance(raw_entry, dict):
                raw_entry = {**raw_entry, 'fetched_at': ts}
            fetched[prov] = _normalize_entry(raw_entry, ts)
    else:
        err_code = str((reply or {}).get('error') or 'model_list_unavailable') if isinstance(reply, dict) else 'model_list_unavailable'
        for prov in missing:
            fetched[prov] = _normalize_entry(None, ts, fallback_error=err_code)
    # The fetch ran outside the lock (it can take seconds). Re-read under the lock and merge only what this call read,
    # so a concurrent refresh of other providers survives, and leave out a provider invalidated after this fetch began.
    merged = {**stored_providers, **fetched}
    try:
        with override_store.locked(path):
            _MEM_CACHE.pop(str(path), None)
            current = _read_store(path)
            on_disk = dict(current['providers'])
            invalidated = current.get('invalidated') if isinstance(current.get('invalidated'), dict) else {}
            for prov, entry in fetched.items():
                stamps = [t for t in (_parse_time(invalidated.get(prov)), _parse_time(invalidated.get('*'))) if t is not None]
                evicted = max(stamps) if stamps else None
                if evicted is None or evicted <= started:
                    on_disk[prov] = entry
            out_doc = {'updated_at': ts, 'ttl_seconds': ttl, 'providers': on_disk}
            if invalidated:
                out_doc['invalidated'] = invalidated
            _write_store(path, out_doc)
        # This call still answers with what it read; the file keeps the newer state.
        merged = {**on_disk, **fetched}
    except (OSError, TimeoutError):
        pass
    return {
        'updated_at': ts,
        'ttl_seconds': ttl,
        'providers': {prov: merged[prov] for prov in target_providers if prov in merged},
    }


def invalidate(provider: str | None = None, *, base_dir: Path | None = None) -> None:
    """Evict `provider` (or all providers when None) from the live-models cache."""
    path = store_path(base_dir)
    _MEM_CACHE.pop(str(path), None)
    try:
        with override_store.locked(path):
            _MEM_CACHE.pop(str(path), None)
            doc = _read_store(path)
            providers = doc['providers']
            invalidated = doc.get('invalidated') if isinstance(doc.get('invalidated'), dict) else {}
            stamp = datetime.now(timezone.utc).isoformat()
            # Stamp even when nothing is cached yet, so a refresh already in flight cannot write the evicted list back.
            for prov in (list(providers) + ['*'] if provider is None else [provider]):
                providers.pop(prov, None)
                invalidated[prov] = stamp
            doc['invalidated'] = invalidated
            _write_store(path, doc)
    except (OSError, TimeoutError):
        pass


def reason(bundle: dict | None, live_doc: dict | None, account: str | None = None, spec: dict | None = None) -> str | None:
    """Exclusion text if the bundle's provider live list was read and omits its launch model, else None."""
    if not isinstance(bundle, dict) or not isinstance(live_doc, dict):
        return None
    if not is_paseo_bundle(bundle, account, spec):
        return None
    providers = live_doc.get('providers')
    if not isinstance(providers, dict):
        return None
    prov = bundle_provider(bundle, account)
    entry = providers.get(prov)
    if not isinstance(entry, dict) or entry.get('ok') is not True:
        # Unreadable live list (Paseo down, provider disabled): never exclude on that basis.
        return None
    mod = launch_model(bundle)
    if mod and not offers_model(prov, mod, entry):
        return f'provider no longer offers {mod}'
    return None


def unavailable_detail(entry: dict | None) -> str | None:
    """Human-readable unavailability text when a provider's live model list could not be read."""
    if not isinstance(entry, dict) or entry.get('ok') is not False or is_unknown_provider(entry):
        return None
    err_code = str(entry.get('error') or entry.get('status') or 'unavailable').strip()
    if err_code and err_code not in ('model_list_unavailable', 'unavailable'):
        return f'model list unavailable ({err_code})'
    return 'model list unavailable'


def warnings(live_doc: dict | None, accounts: dict | None = None, allowed=None, covered=()) -> list[str]:
    """Warnings for `plan`/`roll` output: excluded unoffered models and unreadable live model lists.

    `covered` holds `account/model` keys another warning already names (a `model_latch` latch from the same refusal),
    so one incident gives one line."""
    if not isinstance(live_doc, dict):
        return []
    providers = live_doc.get('providers')
    if not isinstance(providers, dict) or not providers:
        return []
    out: list[str] = []
    seen_warnings: set[str] = set()

    def add(line: str) -> None:
        if line not in seen_warnings:
            seen_warnings.add(line)
            out.append(line)

    acc_map = (accounts or {}).get('accounts') if isinstance(accounts, dict) else None
    if isinstance(acc_map, dict):
        for acct, spec in sorted(acc_map.items()):
            if not isinstance(spec, dict) or spec.get('enabled') is False:
                continue
            if allowed is not None and not allowed(acct):
                continue
            if spec.get('discovery_status') in ('not_installed', 'missing_credentials', 'disabled'):
                continue
            if spec.get('verified') is False:
                continue
            bundles = spec.get('bundles')
            if not isinstance(bundles, dict):
                continue
            unoffered_mods: list[str] = []
            for bid, bundle in bundles.items():
                if not isinstance(bundle, dict) or not _bundle_enabled(bundle):
                    continue
                if bundle.get('discovery_unavailable'):
                    continue
                if not is_paseo_bundle(bundle, acct, spec):
                    continue
                prov = bundle_provider(bundle, acct)
                entry = providers.get(prov)
                if not isinstance(entry, dict):
                    continue
                if entry.get('ok') is False:
                    detail = unavailable_detail(entry)
                    if detail:
                        label = f'{acct} ({prov})' if prov != acct else acct
                        add(f'{label}: {detail}')
                elif entry.get('ok') is True:
                    mod = launch_model(bundle)
                    if (mod and not offers_model(prov, mod, entry) and mod not in unoffered_mods
                            and f'{acct}/{mod}' not in covered):
                        unoffered_mods.append(mod)
            if len(unoffered_mods) == 1:
                mod = unoffered_mods[0]
                add(f'{acct}/{mod}: provider no longer offers {mod}; excluded from dispatch')
            elif len(unoffered_mods) > 1:
                clauses = '; '.join(f'provider no longer offers {m}' for m in unoffered_mods)
                add(f'{acct}: {clauses}; only these models are excluded from dispatch')
        return out

    for prov, entry in sorted(providers.items()):
        detail = unavailable_detail(entry)
        if detail:
            add(f'{prov}: {detail}')
    return out
