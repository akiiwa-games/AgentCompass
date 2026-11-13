"""What stands in the way of a new turn to a worker's account and bundle, composed only from the owners of each condition.

Two readers, two contracts:

- The spawn gate (`setup/router_gate_hook.py`) enforces on a new turn to an existing worker: a disabled account or
  bundle (`host_policy.disabled_cause`), the confirmed-exhaustion latch (`latched`) and a quota window the bundle draws
  that refuses a launch (`window_refusal`), with the worker's own reservation left out (`without_own`).
- The continuation verdict (`worker_sessions.continuation_verdict`, via `worker_refusal`) says `roll` for those same
  conditions and adds advice the gate does not enforce: the worker's last turn ended in a provider failure, the account
  is not ready (`provider_inventory.unavailable_reason`), a provider hold on its account, quota pool or model
  (`provider_retry`), or a model latch (`model_latch`). A parent that sends the follow-up anyway is admitted by the gate
  whenever the enforced conditions pass.

The verdict's own reads are read-only and bounded: the registry on a read-only connection with a short wait, the exhaustion
latch without clearing it, and the failed turn from the watcher's receipt, so it does not depend on the hold or the
settlement that follows the failure. It records no hold and calls no classifier. It loads the fleet through
`account_fleet.load_accounts`, which may update host policy and holds as it does for every other caller. A worker it cannot
resolve is an error (`unknown`), not an all-clear.
"""

from datetime import datetime, timezone
from pathlib import Path

import account_fleet
import axis_scores
import dispatch_registry as registry
import host_policy
import model_latch
import provider_inventory
import provider_retry
import quota_exhaustion
import quota_pools
import usage_cache
import usage_reading


def usage_row(account, usage_cache_path, home):
    """The account's usage reading from the collector's cache (or ROUTER_USAGE_CACHE), else None."""
    providers = (usage_cache.read(usage_cache_path, home)[0] or {}).get('providers')
    row = providers.get(account) if isinstance(providers, dict) else None
    return row if isinstance(row, dict) else None


def latched(account, windows, usage_cache_path, home, read_only=False):
    """True when a confirmed quota failure latches the whole account or any of `windows` (the ids a launch draws)."""
    return quota_exhaustion.blocked(account, usage_row(account, usage_cache_path, home), windows=windows,
                                    read_only=read_only)


def without_own(records, dispatch_id=None, agent_id=None):
    """`records` (active reservations) without the worker's own. A reservation is the worker's own when it names this
    worker's agent id, or names no agent id at all and carries the dispatch id the worker is labelled with. One that
    names a different agent id is never excluded, whatever its dispatch id."""
    def own(record):
        recorded = record.get('agent_id')
        return bool(agent_id and recorded == agent_id) or bool(dispatch_id and not recorded
                                                              and record.get('dispatch_id') == dispatch_id)
    return [r for r in records if not isinstance(r, dict) or not own(r)]


def window_refusal(account, accounts, now, usage_cache_path, records, bundles=None, model=None):
    """(bundle, window id, why) of the first quota window a launch on `account` draws that refuses it now, else None.

    `bundles` are the resolved bundles of the target; without them the launch is probed as `model` on `account` (an
    unknown model draws every window). `records` are the active in-flight reservations, already without the caller's
    own (`without_own`). `accounts` is the loaded fleet; usage is read as routing reads it
    (`usage_reading.usage_snapshot`, then `provider_inventory.merge_usage`).
    """
    usage = usage_reading.usage_snapshot(usage_cache_path, [account], now, inventory=accounts.get('_discovery') or {})
    usage = provider_inventory.merge_usage(usage, accounts, now, names={account})
    probes = [dict(b, id=b.get('bundle_id') or b.get('id')) for b in bundles or ()] or [
        {'account': account, 'model': model or ''}]
    priced = axis_scores.five_axis_overlay(accounts)      # once for every probe
    for probe in probes:
        for wid, why in quota_pools.launch_blockers(probe, priced, usage, now, records=records):
            return probe.get('id') or probe.get('model') or '?', wid, why
    return None


def worker_row(agent_id, stored):
    """The dispatch row of this worker among `stored` rows, else None. The newest row carrying the agent id wins; the
    Paseo record's `router.dispatch_id` label names it when no row carries the agent id yet."""
    mine = [r for r in stored if isinstance(r, dict) and r.get('agent_id') == agent_id]
    if mine:
        return max(mine, key=lambda r: r.get('created_at') or '')
    doc = registry.paseo_agent_record(agent_id)
    labels = doc.get('labels') if isinstance(doc, dict) else None
    labelled = labels.get('router.dispatch_id') if isinstance(labels, dict) else None
    return next((r for r in stored if isinstance(r, dict) and labelled and r.get('dispatch_id') == labelled), None)


def own_failure(row):
    """The provider's message when this worker's last turn ended in a provider failure, `True` when it did without a
    message, else None. Read from the settled row's failure code, or until the row settles from the watcher's receipt:
    it does not wait for the hold the failure records."""
    code, message = row.get('failure_code'), None
    if not code:
        terminal = (registry.watcher_receipt(row['dispatch_id']) or {}).get('terminal')
        terminal = terminal if isinstance(terminal, dict) else {}
        code, message = terminal.get('failure_code'), terminal.get('provider_message')
    if code not in provider_retry.FAILURE_CODES:
        return None
    return message if isinstance(message, str) and message else True


def worker_refusal(agent_id, now=None, home=None):
    """Why a new turn to this worker is not advised now, in words for its parent; None when nothing refuses it.

    Raises ValueError('worker_not_resolvable') for a worker with no dispatch row of this router: nothing here can say
    its account is free, and the gate would resolve it differently. sqlite3.OperationalError when the registry is
    busy for longer than a moment.
    """
    now = now or datetime.now(timezone.utc)
    home = Path(home) if home else Path.home()
    stored = registry.read_dispatches()
    row = worker_row(agent_id, stored)
    bundle = row.get('bundle') if row else None
    if not isinstance(bundle, dict) or not bundle.get('account'):
        raise ValueError('worker_not_resolvable')
    account, dispatch_id = bundle['account'], row['dispatch_id']
    failure = own_failure(row)
    if failure:
        return ('its last turn ended in a provider failure' + (f' ({failure})' if failure is not True else '')
                + '; whether the provider holds the account is classified separately')
    accounts = account_fleet.load_accounts()
    candidates = [r for r in account_fleet.bundle_rows(accounts, bundle.get('bundle_id') or bundle.get('id'))
                  if r['account'] == account]
    off = host_policy.disabled_cause(account, candidates, accounts, host_policy.load_policy())
    if off:
        return (host_policy.disabled_text(account, off, 'en')
                + "; re-enabling is the user's choice in AgentCompass settings, so roll the follow-up instead")
    unavailable = provider_inventory.unavailable_reason(accounts.get('_discovery') or {}, account, bundle.get('provider'))
    if unavailable:
        return f'account {account} is not ready ({unavailable})'
    held = provider_retry.reason(bundle, provider_retry.active(now=now))
    if held:
        return f'account {account} is held after a provider failure ({held})'
    latch = model_latch.reason(bundle, model_latch.active(now=now))
    if latch:
        return f'model {model_latch.launch_model(bundle)} on {account} is held ({latch})'
    windows = sorted(quota_pools.bundle_pools(dict(bundle, id=bundle.get('bundle_id') or bundle.get('id')), accounts))
    if latched(account, windows, None, home, read_only=True):
        return f'quota of account {account} was confirmed exhausted'
    records = without_own(registry.held_records(stored, now), dispatch_id, agent_id)
    found = window_refusal(account, accounts, now, None, records, bundles=[bundle])
    if found:
        what, wid, why = found
        return f'quota window {wid} of {account} ({what}) is unavailable ({why})'
    return None
