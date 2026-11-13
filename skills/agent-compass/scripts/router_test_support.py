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
    import decision_ledger
    import env_vars
    import host_policy
    import provider_inventory

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
               AGENTCOMPASS_ACCOUNTS_OVERRIDE=str(root / 'accounts-override.json'),
               ROUTER_SCORER_CONFIG=str(root / 'scorer-config.json'), AGENTCOMPASS_PUBLIC_DATA='off',
               AGENTCOMPASS_ANTIGRAVITY_PLAN='off', AGENTCOMPASS_LIVE_MODELS='off')
    # Point tests at the fleet fixture so they do not depend on the current public data.
    import account_fleet
    env['ROUTER_ACCOUNTS_FILE'] = str(account_fleet.reference_fleet_path())
    stack.enter_context(patch.dict(os.environ, env, clear=True))
    stack.enter_context(patch.object(Path, 'home', return_value=root))
    # user_env otherwise falls back to the Windows registry, outside the fixture.
    stack.enter_context(patch.object(env_vars, 'user_env', side_effect=os.environ.get))
    stack.enter_context(patch.object(decision_ledger, 'LEDGER_WRITES', True))
    if discovery:
        stack.enter_context(patch.object(provider_inventory, 'current',
                                        return_value={'accounts': {}, 'launchers': {}}))
    if policy:
        stack.enter_context(patch.object(host_policy, 'load_policy', return_value=None))
    return root


def usage_windows(now, accounts):
    """Quota observations from account -> (window, remaining percent, reset hours)."""
    return {'read_ok': True, 'accounts': {name: {'windows': [
        {'id': wid, 'remaining_pct_before_reset': rem, 'observed_remaining_pct': rem,
         'seconds_until_reset': hours * 3600, 'resets_at': (now + timedelta(hours=hours)).isoformat()}
        for wid, rem, hours in rows]} for name, rows in accounts.items()}}


DEPTH = {'routine': 1, 'hard': 2, 'coupled': 3, 'frontier': 4}
STYLE_OF_KIND = {'exploration_analysis': 'exploration', 'research_packet': 'exploration', 'visual_analysis': 'exploration',
                 'text_in_text_out': 'text_only', 'web_research': 'text_only', 'dot_execution': 'text_only',
                 'direction_reasoning': 'advisory', 'complex_build_plan': 'advisory'}


def profile(**changes):
    """A valid classifier profile for a bounded implementation task, with `changes` applied (`axis_demand` may be a
    partial dict). Tests that need a task the router can plan start from this."""
    base = {'work_kind': 'general_implementation', 'reasoning_depth': 2, 'code_complexity': 2,
            'needs_visual': False, 'needs_gui': False,
            'local_only': False, 'wants_fast': False, 'read_only_review': False,
            'access': 'write', 'expected_hours': 1.0, 'hours_confidence': 'medium', 'urgency': 'normal',
            'interaction_style': 'engineering', 'multi_task': False, 'task_count': '1',
            'axis_demand': {'divergence': 1, 'coding': 2, 'aesthetics': 0, 'tool_operation': 0}}
    axis = {**base['axis_demand'], **(changes.pop('axis_demand', None) or {})}
    return {**base, **changes, 'axis_demand': axis}


def profile_for(task):
    """The profile a classifier would give a task a test wrote with only a few fields (work_kind, difficulty, needs,
    expected_hours): the fields the test names are kept, the rest is `profile()`."""
    needs = task.get('needs') or {}
    kind = task.get('work_kind') or 'general_implementation'
    depth = DEPTH.get(task.get('difficulty') or 'hard', 2)
    visual = bool(needs.get('visual') or needs.get('gui') or task.get('needs_visual') or task.get('needs_gui'))
    gui = bool(needs.get('gui') or task.get('needs_gui'))
    style = STYLE_OF_KIND.get(kind, 'engineering')
    hours = task.get('expected_hours') or task.get('hours')
    return profile(
        work_kind=kind, reasoning_depth=depth, needs_visual=visual, needs_gui=gui, interaction_style=style,
        local_only=bool(needs.get('local_only') or task.get('local_only')),
        **({'expected_hours': float(hours)} if hours else {}),
        **({'code_complexity': 0, 'access': 'read-only'} if style in ('text_only', 'advisory') else {}),
        axis_demand=demand_for(depth, style, visual))


def demand_for(depth, style, visual):
    """The category demand a test fixture gives a profile that names only its depth, style and visual need."""
    return {'divergence': 2 if style in ('exploration', 'advisory') else 1,
            'coding': max(max(1, depth - 1), 0 if style in ('text_only', 'advisory') else 2), 'aesthetics': 0,
            'tool_operation': 3 if visual else 0}
