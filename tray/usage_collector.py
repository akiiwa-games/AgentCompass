"""Headless client of Usage Float's direct subscription collector.

Runs --once to refresh active local providers into ~/.usage-float/usage-cache.json
and write a diagnostic receipt at ~/.route-manager/usage-collector.json.
"""
from pathlib import Path
import argparse
import datetime
import json
import os
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugins

sys.path.insert(0, str(plugins.plugin_root('usage-float')))
# Import the same policy implementation used by the installed router.
SKILL_SCRIPTS = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'skills/route-agent-work/scripts'
sys.path.insert(0, str(SKILL_SCRIPTS))
import host_policy
import allocator

STATE = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.route-manager').expanduser()
# Families Usage Float reads directly; account names come from discovery.
COLLECTED_FAMILIES = ('claude', 'codex', 'pi', 'grok', 'antigravity')


def inventory_subscriptions() -> list[str]:
    """The router's discovered accounts the collector can read, for collectors without a discovery API."""
    rows = (allocator.provider_inventory.current().get('accounts') or {})
    return [name for name, row in rows.items()
            if (row.get('family') in COLLECTED_FAMILIES or row.get('backend_kind') == 'litellm')
            and row.get('quota_account') in (None, name)]


def discovered_subscriptions(usage) -> list[str]:
    discover_claude = getattr(usage, 'discover_claude_homes', None)
    discover_codex = getattr(usage, 'discover_codex_accounts', None)
    discover_litellm = getattr(usage, 'discover_litellm_proxies', None)
    read_pi = getattr(usage, 'read_pi_codex_auth', None)
    pi_linked = getattr(usage, 'pi_linked_codex_provider', None)
    if callable(discover_claude) and callable(discover_codex):
        items: list[str] = []
        extra_litellm = tuple(discover_litellm().keys()) if callable(discover_litellm) else ()
        extra_pi = ('pi',) if (callable(read_pi) and read_pi()[0] is not None and (not callable(pi_linked) or pi_linked() is None)) else ()
        for pid in (*discover_claude().keys(), *discover_codex().keys(), *extra_pi, *extra_litellm, 'grok', 'antigravity'):
            if pid not in items:
                items.append(pid)
        return items
    return inventory_subscriptions()


def collect():
    import usage_float as usage
    # Refresh opted-in admission before filtering newly discovered subscriptions.
    accounts = allocator.load_accounts()
    # A permitted account that draws on another's quota needs that meter even when the source's execution is disabled.
    sources = allocator.provider_inventory.quota_sources(accounts)
    defaults = discovered_subscriptions(usage)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not args.once and os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW(None, False, 'Local\\RouteManagerUsageCollector')
        if ctypes.get_last_error() == 183:
            return
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
                raise SystemExit(1)
        if args.once:
            return
        time.sleep(300)


if __name__ == '__main__':
    main()
