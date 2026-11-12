"""The test run is hermetic (hermetic_run.py, loaded by the rootdir conftest.py): no state constant points at the user's real ~/.agent-compass, a write
there is refused and fails the test, and a process the run did not start cannot be terminated. 2026-11-11: tests sent
"quit" to the user's real Usage Float and wrote the real router-window / cli-updates / theatre logs."""
import json
import os
import site
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

import hermetic_run as hermetic


def under_test_home(path):
    return Path(path).resolve().is_relative_to(hermetic.TEST_HOME.resolve())


def test_home_and_every_state_constant_point_into_the_test_home():
    import capacity_stats
    import cli_updates
    import local_controls
    import plugins
    import router_data
    import services
    import update_status

    assert Path.home() == hermetic.TEST_HOME != hermetic.REAL_HOME
    assert under_test_home(os.path.expanduser('~/.agent-compass'))
    for name, path in {'services.STATE_DIR': services.STATE_DIR, 'local_controls.DIRECTORY': local_controls.DIRECTORY,
                       'plugins.STATE_DIR': plugins.STATE_DIR, 'router_data.STATE_DIR': router_data.STATE_DIR,
                       'capacity_stats.STATE_DIR': capacity_stats.STATE_DIR,
                       'update_status.DIRECTORY': update_status.DIRECTORY,
                       'cli_updates.state_dir()': cli_updates.state_dir()}.items():
        assert under_test_home(path), f'{name} = {path} is outside the test home'
    for name in hermetic.STATE_VARIABLES:      # unset, or (CODEX_HOME: this checkout, tray/conftest.py) not real state
        value = os.environ.get(name)
        assert not value or not hermetic._protected(value), name


def test_paseo_agent_id_and_agentcompass_worker_are_cleared_for_every_test():
    assert 'PASEO_AGENT_ID' not in os.environ
    assert 'AGENTCOMPASS_WORKER' not in os.environ
    for name in hermetic.WORKER_VARIABLES:
        assert name not in os.environ, f'{name} leaked into test environment'


def test_user_site_packages_remain_importable_after_appdata_redirection():
    assert Path(os.environ['PYTHONUSERBASE']).resolve() == hermetic.REAL_USERBASE.resolve()
    assert Path(site.getusersitepackages()).resolve() == Path(hermetic.REAL_USERSITE).resolve()
    import pystray  # noqa: F401


def test_worker_key_sanitizes_xdist_worker_id():
    assert hermetic.worker_key({}) == 'main'
    assert hermetic.worker_key({'PYTEST_XDIST_WORKER': 'gw7'}) == 'gw7'
    assert hermetic.worker_key({'PYTEST_XDIST_WORKER': 'gw 1/2'}) == 'gw_1_2'


def test_a_write_to_the_real_state_is_refused_and_fails_the_test():
    import shutil
    before = len(hermetic.VIOLATIONS)
    target = hermetic.REAL_HOME / '.agent-compass' / 'hermetic-guard-probe.txt'
    with pytest.raises(hermetic.HermeticViolation):
        target.write_text('must never be written', encoding='utf-8')
    with pytest.raises(hermetic.HermeticViolation):
        os.replace(hermetic.TEST_HOME / 'nothing', hermetic.REAL_HOME / '.agent-compass' / 'controls' / 'x.json')
    with pytest.raises(hermetic.HermeticViolation):
        shutil.copyfile(hermetic.__file__, target)
    assert not target.exists()
    assert len(hermetic.VIOLATIONS) == before + 3
    del hermetic.VIOLATIONS[before:]        # recorded as expected; the per-test check would fail this test otherwise


def test_terminating_a_process_the_run_did_not_start_is_refused():
    before = len(hermetic.VIOLATIONS)
    # The System process (pid 4 on Windows; init elsewhere): never ours, and not stoppable even if the guard were gone.
    foreign = psutil.Process(4 if os.name == 'nt' else 1)
    for action in (foreign.terminate, foreign.kill):
        with pytest.raises(hermetic.HermeticViolation):
            action()
    assert len(hermetic.VIOLATIONS) == before + 2
    del hermetic.VIOLATIONS[before:]


@pytest.mark.slow
def test_a_child_the_run_started_may_still_be_stopped():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        psutil.Process(child.pid).terminate()
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.slow
def test_xdist_workers_get_distinct_homes_and_keep_user_site_packages():
    repo_root = Path(hermetic.__file__).resolve().parent
    probe = (
        "import json, os, sys; "
        f"sys.path.insert(0, {str(repo_root)!r}); "
        "import hermetic_run as h, pystray; "
        "print(json.dumps({"
        "'home': str(h.TEST_HOME), "
        "'state': str(h.TEST_STATE_DIR), "
        "'real_home': str(h.REAL_HOME), "
        "'paseo': os.environ.get('PASEO_AGENT_ID'), "
        "'worker': os.environ.get('AGENTCOMPASS_WORKER'), "
        "'pystray': bool(pystray.__file__)"
        "}))"
    )
    results = {}
    for wid in ('gw0', 'gw1'):
        env = dict(os.environ, PYTEST_XDIST_WORKER=wid, PASEO_AGENT_ID='leaked-agent', AGENTCOMPASS_WORKER='1')
        done = subprocess.run([sys.executable, '-X', 'utf8', '-c', probe], env=env,
                              capture_output=True, text=True, timeout=20, check=True)
        results[wid] = json.loads(done.stdout.strip())
    assert results['gw0']['home'] != results['gw1']['home']
    assert results['gw0']['state'] != results['gw1']['state']
    assert 'gw0' in Path(results['gw0']['home']).name
    assert 'gw1' in Path(results['gw1']['home']).name
    for wid in ('gw0', 'gw1'):
        assert Path(results[wid]['real_home']).resolve() == hermetic.REAL_HOME.resolve()
        assert results[wid]['paseo'] is None
        assert results[wid]['worker'] is None
        assert results[wid]['pystray'] is True
