"""Why a new turn to an existing worker is refused now, composed only from the owners of each condition.

The conditions are the ones the allocator applies to a candidate and the spawn gate applies to a turn, read once:
the account or bundle switched off (`host_policy`, the model-management checkbox, a legacy meter), the account's
discovery state (`provider_inventory.unavailable_reason`), a provider hold (`provider_retry`), a model
latch (`model_latch`), the confirmed-exhaustion latch (`quota_exhaustion`) and the quota windows the bundle draws
(`quota_pools.launch_blockers` on the usage reading). `setup/router_gate_hook.py` reads `disabled_cause` and
`window_refusal` and `worker_sessions.continuation_verdict` reads `worker_refusal`, so neither restates a rule.
"""

from datetime import datetime, timezone

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


def disabled_cause(account, candidates, accounts, policy):
    """(kind, detail) when the account, or every bundle the target can be, is switched off, else None. Kinds: `legacy`
    (the account's leftover meter text), `host_policy` (account not allowed on this host), `bundles` (ids all unchecked
    in model management). `candidates` are the account's matching bundle rows; `policy` is the loaded host policy."""
    legacy = (accounts.get('accounts') or {}).get(account, {}).get('legacy_meter')
    if isinstance(legacy, str) and legacy.strip():
        return 'legacy', legacy
    if policy is not None and not host_policy.account_allowed(policy, account):
        return 'host_policy', account
    if candidates and all(c.get('disabled') for c in candidates):
        return 'bundles', [c['bundle_id'] for c in candidates]
    return None


def window_refusal(account, accounts, now, usage_cache_path, records, bundles=None, model=None):
    """(bundle, window id, why) of the first quota window a launch on `account` draws that refuses it now, else None.

    `bundles` are the resolved bundles of the target; without them the launch is probed as `model` on `account` (an
    unknown model draws every window). `records` are the active in-flight reservations, already without the caller's
    own. `accounts` is the loaded fleet; usage is read as routing reads it (`route_judge.usage_snapshot`, then
    `provider_inventory.merge_usage`).
    """
    import route_judge
    usage = route_judge.usage_snapshot(usage_cache_path, [account], now, inventory=accounts.get('_discovery') or {})
    usage = provider_inventory.merge_usage(usage, accounts, now, names={account})
    probes = [dict(b, id=b.get('bundle_id') or b.get('id')) for b in bundles or ()] or [
        {'account': account, 'model': model or ''}]
    priced = axis_scores.five_axis_overlay(accounts)      # once for every probe
    for probe in probes:
        for wid, why in quota_pools.launch_blockers(probe, priced, usage, now, records=records):
            return probe.get('id') or probe.get('model') or '?', wid, why
    return None


def worker_bundle(agent_id):
    """(dispatch id, launched bundle) of a worker this router launched, from its Paseo record's dispatch label; else None."""
    doc = registry.paseo_agent_record(agent_id)
    labels = doc.get('labels') if isinstance(doc, dict) else None
    dispatch_id = labels.get('router.dispatch_id') if isinstance(labels, dict) else None
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return None
    try:
        row = registry.inspect(dispatch_id)
    except ValueError:
        return None
    bundle = row.get('bundle')
    return (dispatch_id, bundle) if isinstance(bundle, dict) and bundle.get('account') else None


def worker_refusal(agent_id, now=None):
    """Why a new turn to this worker would be refused now, in words for its parent; None when nothing refuses it or the
    worker is not one this router launched (the gate also leaves a target it cannot resolve alone)."""
    located = worker_bundle(agent_id)
    if located is None:
        return None
    dispatch_id, bundle = located
    account = bundle['account']
    now = now or datetime.now(timezone.utc)
    accounts = account_fleet.load_accounts()
    candidates = [dict(row, bundle_id=bundle_id) for bundle_id in [bundle.get('bundle_id') or bundle.get('id')]
                  for row in [((accounts.get('accounts') or {}).get(account, {}).get('bundles') or {}).get(bundle_id)]
                  if isinstance(row, dict)]
    off = disabled_cause(account, candidates, accounts, host_policy.load_policy())
    if off:
        kind, detail = off
        return {'legacy': f'account {account} is switched off ({detail})',
                'host_policy': f'account {account} is disabled on this host by its host policy',
                'bundles': f'bundle {", ".join(detail)} is unchecked in model management'}[kind] + (
            "; re-enabling is the user's choice in AgentCompass settings, so roll the follow-up instead")
    unavailable = provider_inventory.unavailable_reason(accounts.get('_discovery') or {}, account, bundle.get('provider'))
    if unavailable:
        return f'account {account} is not ready ({unavailable})'
    held = provider_retry.reason(bundle, provider_retry.active(now=now))
    if held:
        return f'account {account} is held after a provider failure ({held})'
    latched = model_latch.reason(bundle, model_latch.active(now=now))
    if latched:
        return f'model {model_latch.launch_model(bundle)} on {account} is held ({latched})'
    row = (usage_cache.read(None, None)[0] or {}).get('providers', {}).get(account)
    windows = sorted(quota_pools.bundle_pools(dict(bundle, id=bundle.get('bundle_id') or bundle.get('id')), accounts))
    if quota_exhaustion.blocked(account, row if isinstance(row, dict) else None, windows=windows):
        return f'quota of account {account} was confirmed exhausted'
    records = [r for r in registry.reservations(now=now, read_only=True)
               if not isinstance(r, dict) or (r.get('dispatch_id') != dispatch_id and r.get('agent_id') != agent_id)]
    found = window_refusal(account, accounts, now, None, records, bundles=[bundle])
    if found:
        what, wid, why = found
        return f'quota window {wid} of {account} ({what}) is unavailable ({why})'
    return None
