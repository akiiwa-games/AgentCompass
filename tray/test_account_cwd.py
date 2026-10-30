import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import allocator
import cwd_policy
import router_data
from test_router_data_models import settings
import pytest


def test_regex_normalizes_slashes_and_case_and_fails_closed_for_invalid_config():
    assert cwd_policy.matches('^E:/P4(/|$)', r'e:\p4\game')
    assert not cwd_policy.matches('^E:/P4(/|$)', 'E:/P40/game')
    assert cwd_policy.matches('', 'C:/anything')
    assert not cwd_policy.matches('[', 'E:/P4')
    with pytest.raises(ValueError):
        cwd_policy.validate('[')


def test_legacy_prefix_prefill_and_override_save(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    router_data._atomic_json(target, {'schema_version': 2, 'accounts': {'codex': {'allowed_cwd': ['E:/P4']}}})
    assert router_data.read_account_cwd('codex') == '^E:/P4(/|$)'
    router_data.save_account_cwd('codex', '^E:/(P4|PROJECTS)(/|$)')
    data = router_data._read_json(target)['accounts']['codex']
    assert 'allowed_cwd' not in data
    assert data['cwd_regex'] == '^E:/(P4|PROJECTS)(/|$)'
    assert allocator.load_accounts()['accounts']['codex']['cwd_regex'] == data['cwd_regex']
    before = target.read_bytes()
    with pytest.raises(ValueError):
        router_data.save_account_cwd('codex', '[')
    assert target.read_bytes() == before
    rows = [row for row in router_data.harness_model_rows() if row['account'] == 'codex']
    assert {row['cwd_regex'] for row in rows} == {data['cwd_regex']}


def test_regex_and_legacy_prefix_intersect_in_allocator():
    spec = {'allowed_cwd': ['E:/P4'], 'cwd_regex': '/game(/|$)'}
    assert allocator.account_cwd_allowed(spec, r'e:\p4\game')
    assert not allocator.account_cwd_allowed(spec, r'e:\p4\other')
    assert not allocator.account_cwd_allowed(spec, r'e:\projects\game')


def test_plan_explains_regex_exclusion_and_legacy_intersection():
    scoring = json.loads(allocator.reference_fleet_path().read_text(encoding='utf-8'))['scoring']
    accounts = {'scoring': scoring, 'models': {'fixture-model': {'ability': 4}}, 'accounts': {
        'fixture-account': {'verified': True, 'cwd_regex': '^E:/P4(/|$)', 'allowed_cwd': ['E:/P4'],
                            'bundles': {'fixture-bundle': {'model': 'fixture-model', 'effort': 'high'}}}}}
    plan = allocator.plan({'objective': 'Implement a parser', 'work_kind': 'general_implementation',
                           'expected_hours': .1, 'cwd': 'E:/PROJECTS/AgentCompass'},
                          {'accounts': {}}, datetime(2026, 10, 28, tzinfo=timezone.utc),
                          accounts=accounts, capabilities={}, overrides=[], host_policy_config={'allowed_accounts': list(accounts['accounts'])})
    row = next(row for row in plan['ranking'] if row['id'] == 'fixture-bundle')
    assert 'outside account cwd restriction (^E:/P4(/|$))' in row['excluded']
    assert any('cwd_regex and legacy allowed_cwd' in warning for warning in plan['decision']['warnings'])
