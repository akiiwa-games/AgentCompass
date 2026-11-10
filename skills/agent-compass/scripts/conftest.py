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


@pytest.fixture(autouse=True)
def isolated_provider_retries(monkeypatch):
    """Tests never read or write the machine's provider-retry holds; provider-retry tests point it at their own file."""
    monkeypatch.setenv('ROUTER_PROVIDER_RETRY', 'off')


@pytest.fixture(autouse=True)
def isolated_live_models(monkeypatch):
    """Tests never read or write the machine's live-model cache; live-model tests point it at their own file."""
    monkeypatch.setenv('ROUTER_LIVE_MODELS', 'off')


@pytest.fixture(autouse=True)
def bare_tasks_get_a_profile(monkeypatch):
    """The router plans only a task with a classifier profile (its category demands). Most tests write a task with a few
    fields; this gives such a task the profile `router_test_support.profile_for` derives. A task whose own profile names no
    `axis_demand` gets the demand `router_test_support.demand_for` derives; a test of the missing-profile or missing-demand error calls
    `allocator.normalize_task.__wrapped__` or `validate_profile` and is untouched."""
    import allocator
    import router_test_support
    real = allocator.normalize_task

    def normalize(raw, *args, **kwargs):
        task = dict(raw or {})
        if task.get('profile') is None:
            task['profile'] = router_test_support.profile_for(task)
        elif isinstance(task['profile'], dict) and 'axis_demand' not in task['profile']:
            given = task['profile']
            task['profile'] = {**given, 'axis_demand': router_test_support.demand_for(
                given.get('reasoning_depth') or 2, given.get('interaction_style'), bool(given.get('needs_visual')))}
        return real(task, *args, **kwargs)
    normalize.__wrapped__ = real
    monkeypatch.setattr(allocator, 'normalize_task', normalize)
