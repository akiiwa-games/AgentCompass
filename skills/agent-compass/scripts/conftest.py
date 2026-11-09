"""Release unittest module fixtures when the suite is run through pytest.

Pytest invokes setUpModule but does not run unittest's module cleanup stack.
Without this bridge, patched account loaders and host policies leak to later files.
"""
import unittest

import pytest


@pytest.fixture(scope='module', autouse=True)
def unittest_module_cleanups():
    yield
    unittest.doModuleCleanups()


@pytest.fixture(autouse=True)
def disabled_score_cache(monkeypatch):
    """Tests never touch the machine's real score cache and count scorer calls; cache tests opt back in."""
    monkeypatch.setenv('ROUTER_SCORE_CACHE', 'off')


@pytest.fixture(autouse=True)
def offline_public_data(monkeypatch):
    """Tests read the shipped references and never fetch public data; test_public_data opts back in."""
    monkeypatch.setenv('AGENTCOMPASS_PUBLIC_DATA', 'off')
    monkeypatch.setenv('AGENTCOMPASS_ANTIGRAVITY_PLAN', 'off')


@pytest.fixture(autouse=True)
def isolated_launch_failures(monkeypatch):
    """Tests never read or write the machine's launch-failure cooldowns; test_launch_failures opts back in."""
    monkeypatch.setenv('ROUTER_LAUNCH_FAILURES', 'off')


@pytest.fixture(autouse=True)
def isolated_model_latches(monkeypatch):
    """Tests never read or write the machine's latched models; model-latch tests point it at their own file."""
    monkeypatch.setenv('ROUTER_MODEL_LATCH', 'off')
