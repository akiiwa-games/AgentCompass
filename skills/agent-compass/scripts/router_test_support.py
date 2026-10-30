"""Hermetic router fixtures shared by contract tests and their subprocesses."""
from contextlib import ExitStack
from datetime import timedelta
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def temporary_root(add_cleanup):
    directory = tempfile.TemporaryDirectory(prefix='router-tests-')
    add_cleanup(directory.cleanup)
    return Path(directory.name)


def isolated_router_state(add_cleanup, *, discovery=True, policy=True):
    """Install isolated defaults; individual tests may override any boundary."""
    import allocator
    import route_judge

    stack = ExitStack()
    add_cleanup(stack.close)
    root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='router-tests-')))
    # Inherited daemon/provider paths survive HOME changes unless removed explicitly.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('ROUTER_', 'AGENTCOMPASS_', 'ROUTE_MANAGER_', 'PASEO_', 'DOT2API_'))
           and key not in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'JEV_API_KEY')}
    env.update(HOME=str(root), USERPROFILE=str(root), ROUTER_PROVIDER_HOME=str(root),
               ROUTER_STATE_DIR=str(root / '.agent-compass'), PASEO_HOME=str(root / '.paseo'),
               ROUTER_DISPATCH_DB=str(root / 'dispatch.sqlite3'),
               ROUTER_LEDGER=str(root / 'ledger.jsonl'), ROUTER_USAGE_CACHE=str(root / 'usage.json'),
               ROUTER_CAPACITY=str(root / 'capacity.json'), ROUTER_OVERRIDES=str(root / 'overrides.json'),
               ROUTER_ACCOUNTS_OVERRIDE=str(root / 'accounts-override.json'),
               ROUTER_SCORER_CONFIG=str(root / 'scorer-config.json'))
    # A pinned override hides accounts.local.json. Point tests at the checked-in fixture
    # so they do not depend on the neutral template or on this machine's home fleet.
    reference = Path(__file__).resolve().parent / 'fixtures' / 'reference-accounts.json'
    if reference.is_file():
        env['ROUTER_ACCOUNTS_FILE'] = str(reference)
    stack.enter_context(patch.dict(os.environ, env, clear=True))
    stack.enter_context(patch.object(Path, 'home', return_value=root))
    # user_env otherwise falls back to the Windows registry, outside the fixture.
    stack.enter_context(patch.object(route_judge, 'user_env', side_effect=os.environ.get))
    stack.enter_context(patch.object(route_judge, 'LEDGER_WRITES', True))
    if discovery:
        stack.enter_context(patch.object(allocator.provider_inventory, 'current',
                                        return_value={'accounts': {}, 'launchers': {}}))
    if policy:
        stack.enter_context(patch.object(allocator.host_policy, 'load_policy', return_value=None))
    return root


def usage_windows(now, accounts):
    """Quota observations from account -> (window, remaining percent, reset hours)."""
    return {'read_ok': True, 'accounts': {name: {'windows': [
        {'id': wid, 'remaining_pct_before_reset': rem, 'observed_remaining_pct': rem,
         'seconds_until_reset': hours * 3600, 'resets_at': (now + timedelta(hours=hours)).isoformat()}
        for wid, rem, hours in rows]} for name, rows in accounts.items()}}
