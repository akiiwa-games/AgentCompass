"""The test run is hermetic (hermetic_run.py, loaded by the rootdir conftest.py): no state constant points at the user's real ~/.agent-compass, a write
there is refused and fails the test, and a process the run did not start cannot be terminated. 2026-11-11: tests sent
"quit" to the user's real Usage Float and wrote the real router-window / cli-updates / theatre logs."""
import os
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


def test_a_write_to_the_real_state_is_refused_and_fails_the_test():
    before = len(hermetic.VIOLATIONS)
    target = hermetic.REAL_HOME / '.agent-compass' / 'hermetic-guard-probe.txt'
    with pytest.raises(hermetic.HermeticViolation):
        target.write_text('must never be written', encoding='utf-8')
    with pytest.raises(hermetic.HermeticViolation):
        os.replace(hermetic.TEST_HOME / 'nothing', hermetic.REAL_HOME / '.agent-compass' / 'controls' / 'x.json')
    assert not target.exists()
    assert len(hermetic.VIOLATIONS) == before + 2
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


def test_a_child_the_run_started_may_still_be_stopped():
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        psutil.Process(child.pid).terminate()
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()
