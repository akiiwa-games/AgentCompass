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
           and key not in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'JEV_API_KEY', 'TYPESAFE_API_KEY', 'GEMINI_API_KEY')}
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


LEVEL_OF_DIFFICULTY = {'routine': 1, 'hard': 2, 'coupled': 3, 'frontier': 3}
READONLY_KINDS = frozenset({'code_audit', 'architecture_reasoning', 'exploration_forensics', 'web_research', 'creative_ideation'})


def profile(**changes):
    """A valid classifier profile for a bounded implementation task, with `changes` applied (`axis_demand` may be a
    partial dict). Tests that need a task the router can plan start from this."""
    import task_intake
    explicit_demand = changes.pop('axis_demand', None)
    work_kind = changes.get('work_kind', 'code_implementation')
    if 'work_kind' not in changes:
        if changes.get('needs_gui') is True:
            work_kind = 'gui_control'
        elif changes.get('read_only_review') is True:
            work_kind = 'code_audit'
        elif changes.get('access') == 'read-only' and changes.get('local_only') is True:
            work_kind = 'exploration_forensics'
    primary_level = changes.get('primary_level', 2)
    secondary_kind = changes.get('secondary_kind', 'none')
    secondary_level = changes.get('secondary_level', 0 if secondary_kind == 'none' else 1)
    needs_gui = bool(changes.get('needs_gui', 'gui_control' in (work_kind, secondary_kind)))
    needs_visual = bool(changes.get('needs_visual', needs_gui or 'visual_analysis' in (work_kind, secondary_kind)))
    read_only_review = bool(changes.get('read_only_review', work_kind == 'code_audit'))
    default_access = 'read-only' if (read_only_review or work_kind == 'code_audit') else 'write'
    task_count = changes.get('task_count', '2' if changes.get('multi_task') is True else '1')
    multi_task = bool(changes.get('multi_task', task_count != '1'))
    base = {
        'work_kind': work_kind,
        'primary_level': primary_level,
        'secondary_kind': secondary_kind,
        'secondary_level': secondary_level,
        'needs_visual': needs_visual,
        'needs_gui': needs_gui,
        'local_only': False,
        'wants_fast': False,
        'read_only_review': read_only_review,
        'access': default_access,
        'expected_hours': 1.0,
        'multi_task': multi_task,
        'task_count': task_count,
    }
    merged = {**base, **changes}
    projected = task_intake.project_axis_demand(
        merged['work_kind'], merged['primary_level'], merged['secondary_kind'], merged['secondary_level'],
    )
    merged['axis_demand'] = {**projected, **(explicit_demand or {})}
    return merged


def profile_for(task):
    """The profile a classifier would give a task a test wrote with only a few fields (work_kind, difficulty, needs,
    expected_hours): the fields the test names are kept, the rest is `profile()`."""
    import task_intake
    needs = task.get('needs') or {}
    raw_kind = task.get('work_kind')
    gui = bool(needs.get('gui') or task.get('needs_gui') or raw_kind == 'gui_control')
    visual = bool(needs.get('visual') or gui or task.get('needs_visual') or raw_kind == 'visual_analysis')
    kind = (
        raw_kind if raw_kind in task_intake.WORK_KINDS
        else 'gui_control' if gui
        else 'visual_analysis' if visual
        else 'code_implementation'
    )
    lvl = LEVEL_OF_DIFFICULTY.get(task.get('difficulty') or 'hard', 2)
    hours = task.get('expected_hours') or task.get('hours')
    local = bool(needs.get('local_only') or task.get('local_only'))
    access = 'read-only' if kind in READONLY_KINDS else 'write'
    return profile(
        work_kind=kind, primary_level=lvl, needs_visual=visual, needs_gui=gui,
        local_only=local, access=access,
        **({'expected_hours': float(hours)} if hours else {}),
    )
