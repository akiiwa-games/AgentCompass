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
