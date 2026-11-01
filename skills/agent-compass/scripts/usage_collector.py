"""Collect provider usage once and publish it where the router reads it.

Reads the accounts this machine's host policy allows, writes the usage cache (~/.agent-compass/usage-cache.json, or under
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
import allocator
import host_policy
import usage_cache
import usage_core
import venice_usage

STATE = usage_cache.state_dir()


def discovered_subscriptions() -> list[str]:
    """Every account the core can read on this machine: Claude and Codex homes, Pi, LiteLLM proxies, Grok, agy."""
    usage = usage_core
    items: list[str] = []
    extra_pi = ('pi',) if usage.read_pi_codex_auth()[0] is not None and usage.pi_linked_codex_provider() is None else ()
    for pid in (*usage.discover_claude_homes().keys(), *usage.discover_codex_accounts().keys(), *extra_pi,
                *usage.discover_litellm_proxies().keys(), 'grok', 'antigravity'):
        if pid not in items:
            items.append(pid)
    return items


def collect():
    usage = usage_core
    venice_usage.refresh(STATE)
    # Refresh opted-in admission before filtering newly discovered subscriptions.
    accounts = allocator.load_accounts()
    # A permitted account that draws on another's quota needs that meter even when the source's execution is disabled.
    sources = allocator.provider_inventory.quota_sources(accounts)
    defaults = discovered_subscriptions()
    subscriptions = host_policy.usage_providers(host_policy.load_policy(), defaults + [n for n in sources if n not in defaults])
    subscriptions = list(dict.fromkeys(sources.get(name, name) for name in subscriptions))
    selected = usage.active_providers(subscriptions)
    observations = usage.fetch_all(selected, force=False)
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
    args = parser.parse_args(argv)
    if not args.once and os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW(None, False, 'Local\\AgentCompassUsageCollector')
        if ctypes.get_last_error() == 183:
            return 0
    while True:
        try:
            result = collect()
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
