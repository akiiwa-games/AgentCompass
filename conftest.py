"""Hermetic test runs: see hermetic_run.py. Imported before any test module, so every state constant a module captures at
import points into the run's temporary home."""
from pathlib import Path

import pytest

import hermetic_run
from hermetic_run import TEST_HOME, VIOLATIONS


@pytest.fixture(scope='session', autouse=True)
def hermetic_home():
    """The run's own home (hermetic_run); a test that left it fails the session."""
    assert Path.home() == TEST_HOME, f'tests must run under the temporary home, not {Path.home()}'
    yield TEST_HOME


@pytest.fixture(autouse=True)
def no_real_state_or_processes(request):
    hermetic_run.CURRENT['test'] = request.node.nodeid
    before = len(VIOLATIONS)
    yield
    hermetic_run.CURRENT['test'] = '<between tests>'
    mine = VIOLATIONS[before:]
    if mine:
        pytest.fail('hermetic test run: this test reached the real state or a foreign process: '
                    + '; '.join(what for _, what in mine), pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    if VIOLATIONS and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    if VIOLATIONS:
        terminalreporter.section('hermetic violations (refused)')
        for test, what in VIOLATIONS:
            terminalreporter.write_line(f'{test}: {what}')
