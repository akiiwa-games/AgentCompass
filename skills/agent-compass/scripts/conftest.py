"""Autouse isolation fixtures for the agent-compass scripts suite."""
import pytest


@pytest.fixture(autouse=True)
def disabled_score_cache(monkeypatch):
    """Tests never touch the machine's real score cache and count scorer calls; cache tests opt back in."""
    monkeypatch.setenv('ROUTER_SCORE_CACHE', 'off')


@pytest.fixture(autouse=True)
def offline_lookups(monkeypatch):
    """Tests never look up the Antigravity tier or the published reference data over the network, and read the shipped
    references; antigravity_plan and public_data tests pass their own environment."""
    monkeypatch.setenv('AGENTCOMPASS_ANTIGRAVITY_PLAN', 'off')
    monkeypatch.setenv('AGENTCOMPASS_PUBLIC_DATA', 'off')


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
    monkeypatch.setenv('AGENTCOMPASS_LIVE_MODELS', 'off')


@pytest.fixture(autouse=True)
def bare_tasks_get_a_profile(monkeypatch):
    """The router plans only a task with a classifier profile (its category demands). Most tests write a task with a few
    fields; this gives such a task the profile `router_test_support.profile_for` derives. A test of the missing-profile
    error calls `task_intake.normalize_task.__wrapped__` or `validate_profile` and is untouched."""
    import task_intake
    import router_test_support
    real = task_intake.normalize_task

    def normalize(raw, *args, **kwargs):
        task = dict(raw or {})
        if task.get('profile') is None:
            task['profile'] = router_test_support.profile_for(task)
        return real(task, *args, **kwargs)
    normalize.__wrapped__ = real
    monkeypatch.setattr(task_intake, 'normalize_task', normalize)
