"""Models a launch reported as not existing, latched out of routing until they are seen again.

Contract: a definitive Paseo create failure whose structured error code (`failure_code`, the provider's own
`ProviderActionError` code as the daemon recorded it) says the model does not exist latches that account's model in
<state dir>/model-unavailable.json. Only the code decides; no error text is read or stored. A latch holds until the
first of:
  - a worker on that account and model starts after the latch (registry observe),
  - discovery (the tray's model-catalog.json) lists the model in a probe newer than the latch,
  - RETRY_HOURS pass, after which the next roll may pick the model again and so re-probes it,
  - the user clears it (`model_latch.py --clear <account>/<model>`).
Settings never hold a latch: the user's model checkboxes stay as they were.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import account_state
import override_store

# The same re-probe period as a latched account (account_state.LATCH_RETRY_HOURS).
RETRY_HOURS = account_state.LATCH_RETRY_HOURS
# Provider error codes that mean "this model does not exist on this provider", compared case-insensitively.
# INVALID_MODEL: paseo-antigravity-resilient's ProviderActionError when agy does not recognize the model.
MODEL_UNAVAILABLE_CODES = frozenset(('invalid_model', 'model_not_found', 'unknown_model', 'unsupported_model'))
FILE = 'model-unavailable.json'


def enabled():
    return os.environ.get('ROUTER_MODEL_LATCH', '').strip().lower() != 'off'


def store_path():
    override = os.environ.get('ROUTER_MODEL_LATCH', '').strip()
    if override and override.lower() != 'off':
        return Path(override)
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / FILE


def catalog_path():
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'model-catalog.json'


def _time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _read(path):
    try:
        doc = json.loads(override_store.retry_denied(lambda: Path(path).read_text(encoding='utf-8')))
    except (FileNotFoundError, ValueError):
        return {'models': {}}
    return doc if isinstance(doc, dict) and isinstance(doc.get('models'), dict) else {'models': {}}


def launch_model(bundle):
    return (bundle or {}).get('launch_model') or (bundle or {}).get('model')


def key(account, model):
    return f'{account}/{model}'


def means_unavailable(code):
    return isinstance(code, str) and code.strip().lower() in MODEL_UNAVAILABLE_CODES


def record(bundle, code, *, at=None, source=None, path=None):
    """Latch the bundle's model when `code` says it does not exist. Returns the entry, or None."""
    account, model = (bundle or {}).get('account'), launch_model(bundle)
    if not enabled() or not means_unavailable(code) or not isinstance(account, str) or not isinstance(model, str):
        return None
    at = at or datetime.now(timezone.utc)
    path = path or store_path()
    entry = {'account': account, 'provider': bundle.get('provider') or account, 'model': model,
             'code': code.strip(), 'observed_at': at.isoformat(), 'source': source}
    with override_store.locked(path):
        doc = _read(path)
        doc['models'][key(account, model)] = entry
        override_store.write(path, doc)
    return entry


def retry_at(entry):
    observed = _time((entry or {}).get('observed_at'))
    return observed + timedelta(hours=RETRY_HOURS) if observed else None


def redetected(entry, catalog):
    """Discovery listed the model in a probe made after the latch."""
    observed = _time(entry.get('observed_at'))
    probe = ((catalog or {}).get('providers') or {}).get(entry.get('provider'))
    if not observed or not isinstance(probe, dict) or not probe.get('ok') or probe.get('type') == 'static':
        return False
    checked = probe.get('checked_at')
    if not isinstance(checked, (int, float)) or isinstance(checked, bool) or checked <= observed.timestamp():
        return False
    return any((m.get('id') if isinstance(m, dict) else m) == entry.get('model') for m in probe.get('models') or [])


def _catalog():
    try:
        doc = json.loads(catalog_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def active(*, now=None, path=None, catalog=None):
    """Latches still in effect, keyed `account/model`. Reading never writes: an outlived latch is only ignored."""
    if not enabled():
        return {}
    now = now or datetime.now(timezone.utc)
    path = path or store_path()
    if not path.exists():
        return {}
    try:
        entries = _read(path)['models']
    except PermissionError:
        return {}
    catalog = _catalog() if catalog is None else catalog
    return {k: v for k, v in entries.items()
            if isinstance(v, dict) and (retry_at(v) or now) > now and not redetected(v, catalog)}


def entry_for(bundle, entries):
    if not entries:
        return None
    return entries.get(key((bundle or {}).get('account'), launch_model(bundle)))


def retry_text(entry):
    when = retry_at(entry)
    return when.isoformat(timespec='minutes') if when else f'after {RETRY_HOURS:g}h'


def reason(bundle, entries):
    """Exclusion text for a latched bundle, else None."""
    entry = entry_for(bundle, entries)
    if not entry:
        return None
    return (f"model launch failed: unavailable ({entry.get('code')}); next retry at {retry_text(entry)}, "
            f"or clear with `model_latch.py --clear {key(entry.get('account'), entry.get('model'))}`")


def warnings(entries, allowed=None):
    """One `plan`/`roll` line per latched model. `allowed(account)` skips accounts this host does not use."""
    return [f"{k}: model launch failed: unavailable ({v.get('code')}); excluded from dispatch until "
            f"{retry_text(v)}, a successful start, or discovery lists it again"
            for k, v in sorted((entries or {}).items()) if allowed is None or allowed(v.get('account'))]


def clear(account, model=None, *, launched_at=None, path=None):
    """Drop latches on `account` (one `model`, or all). With `launched_at`, only latches older than that launch: a
    start that began before the failure says nothing about it. No write when there is nothing to drop."""
    path = path or store_path()
    if not enabled() or not isinstance(account, str) or not path.exists():
        return False
    launched = _time(launched_at) if launched_at is not None else None
    with override_store.locked(path):
        doc = _read(path)
        keys = [k for k, v in doc['models'].items() if isinstance(v, dict) and v.get('account') == account
                and (model is None or v.get('model') == model)
                and (launched_at is None or (launched and (_time(v.get('observed_at')) or launched) < launched))]
        if not keys:
            return False
        for k in keys:
            doc['models'].pop(k)
        override_store.write(path, doc)
    return True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--clear', metavar='ACCOUNT/MODEL', help='Clear one latched model (or ACCOUNT for all) by hand')
    args = parser.parse_args()
    if args.clear:
        account, _, model = args.clear.partition('/')
        clear(account, model or None)
    print(json.dumps(_read(store_path())['models'], ensure_ascii=False, indent=2))
