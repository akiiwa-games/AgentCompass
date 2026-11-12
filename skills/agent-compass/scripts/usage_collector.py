"""Collect provider usage once and publish it where the router reads it.

Reads every account with local credentials (narrowed only by an explicit `usage_providers` list in host policy; dispatch
exclusions in `allowed_accounts` do not suppress usage), writes the usage cache (~/.agent-compass/usage-cache.json, or under
ROUTER_STATE_DIR) and prints a receipt (also saved as usage-collector.json next to it). Needs neither the Usage Float
plugin nor the tray. `agentcompass usage --show` prints what routing reads from that cache without collecting.
"""
from pathlib import Path
import argparse
import datetime
import json
import os
import socket
import sys
import time

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import account_fleet
import provider_inventory
import host_policy
import usage_cache
import usage_core
import venice_usage

STATE = usage_cache.state_dir()


def discovered_subscriptions() -> list[str]:
    """Every account the core can read on this machine: Claude and Codex homes, Pi, metered USD accounts, Grok, agy."""
    usage = usage_core
    items: list[str] = []
    extra_pi = ('pi',) if usage.read_pi_codex_auth()[0] is not None and usage.pi_linked_codex_provider() is None else ()
    for pid in (*usage.discover_claude_homes().keys(), *usage.discover_codex_accounts().keys(), *extra_pi,
                *usage.discover_metered_usd_accounts().keys(), 'grok', 'antigravity'):
        if pid not in items:
            items.append(pid)
    return items


def _declared_quota_sources() -> dict[str, str]:
    sources = dict(account_fleet.configured_quota_sources())
    override = account_fleet.accounts_override_path()
    if override.is_file():
        try:
            doc = json.loads(override.read_text(encoding='utf-8-sig'))
            sources.update(provider_inventory.quota_sources(doc))
        except (OSError, ValueError):
            pass
    return sources


def _permitted_meters(policy, sources: dict[str, str], candidates: list[str]) -> list[str]:
    allowed = host_policy.usage_providers(policy, candidates + [n for n in sources if n not in candidates])
    return list(dict.fromkeys(sources.get(name, name) for name in allowed))


def resolve_targets(providers=None) -> list[str]:
    """Resolve permitted quota-meter provider IDs via load_accounts, quota_sources and host policy."""
    # Refresh opted-in admission before filtering newly discovered subscriptions.
    accounts = account_fleet.load_accounts()
    # A permitted account that draws on another's quota needs that meter even when the source's execution is disabled.
    sources = provider_inventory.quota_sources(accounts)
    permitted = _permitted_meters(host_policy.load_policy(), sources, discovered_subscriptions())
    if providers is None:
        return permitted
    allowed_set = set(permitted)
    requested = [
        sources.get(pid, pid)
        for pid in dict.fromkeys(usage_core.provider_fetch_id(item) for item in providers)
    ]
    return [pid for pid in dict.fromkeys(requested) if pid in allowed_set]


def filter_permitted_items(items) -> list[str]:
    """Filter display items only when host policy explicitly narrows usage_providers."""
    items = list(items)
    if not items:
        return []
    try:
        policy = host_policy.load_policy()
    except host_policy.HostPolicyError:
        return items
    if policy is None or 'usage_providers' not in policy:
        return items
    sources = _declared_quota_sources()
    candidates = list(dict.fromkeys(usage_core.provider_fetch_id(item) for item in items))
    permitted = set(_permitted_meters(policy, sources, candidates))
    return [
        item for item in items
        if sources.get(usage_core.provider_fetch_id(item), usage_core.provider_fetch_id(item)) in permitted
    ]


def read_usage(providers) -> list[dict]:
    """Read cached usage for policy-permitted providers without network calls or cache writes."""
    permitted = filter_permitted_items(providers)
    return usage_core.read_cached_usage(permitted) if permitted else []


def refresh_providers(providers, *, force: bool = True):
    """Collect usage for requested display items/providers using the collector's target resolution."""
    selected = usage_core.active_providers(resolve_targets(providers))
    return usage_core.fetch_all(selected, force=force) if selected else []


def collect(*, force: bool = False):
    usage = usage_core
    venice_usage.refresh(STATE)
    subscriptions = resolve_targets()
    selected = usage.active_providers(subscriptions)
    observations = usage.fetch_all(selected, force=force)
    try:
        import provider_retry
        # Holds a worker the bridge stopped for a stuck provider retry, so the tray shows it before the next roll.
        provider_retry.ingest()
    except Exception as error:  # noqa: BLE001 - never costs the usage reading
        with (STATE / 'usage-collector.log').open('a', encoding='utf-8') as stream:
            stream.write(f'{datetime.datetime.now().isoformat()} provider_retry {type(error).__name__}\n')
    result = {'hostname': socket.gethostname(), 'collection_mode': 'direct',
              'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'cache_path': str(usage.CACHE_PATH), 'excluded': [],
              'providers': {p.provider_id: {'available': p.available, 'stale': p.stale, 'fetched_at': p.fetched_at,
                                         'window_count': len(p.windows)} for p in observations},
              'not_detected': [p for p in subscriptions if p not in selected],
              'requested_providers': subscriptions}
    STATE.mkdir(parents=True, exist_ok=True)
    target = STATE / 'usage-collector.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    temporary.replace(target)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog='agentcompass usage', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--once', action='store_true', help='Collect one time and exit (what `agentcompass usage` always does); without it the tray helper repeats every 5 minutes.')
    parser.add_argument('--force', action='store_true', help='Skip poll/retry throttling (Claude 429 rate-limit backoff still applies).')
    args = parser.parse_args(argv)
    if not args.once and os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW(None, False, 'Local\\AgentCompassUsageCollector')
        if ctypes.get_last_error() == 183:
            return 0
    first_pass = True
    while True:
        try:
            result = collect(force=bool(args.force and first_pass))
            first_pass = False
            if sys.stdout is not None:
                print(json.dumps(result), flush=True)
        except Exception as error:
            STATE.mkdir(parents=True, exist_ok=True)
            # Exception text can include remote data; record its class, not its contents.
            with (STATE / 'usage-collector.log').open('a', encoding='utf-8') as stream:
                detail = str(error) if isinstance(error, host_policy.HostPolicyError) else type(error).__name__
                stream.write(f'{datetime.datetime.now().isoformat()} {detail}\n')
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(300)


if __name__ == '__main__':
    raise SystemExit(main())
