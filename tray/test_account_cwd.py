import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import allocator
import account_fleet
import cwd_policy
import router_data
from test_router_data_models import settings
import pytest
from router_test_support import profile


def test_regex_normalizes_slashes_and_case_and_fails_closed_for_invalid_config():
    assert cwd_policy.matches('^E:/P4(/|$)', r'e:\p4\game')
    assert not cwd_policy.matches('^E:/P4(/|$)', 'E:/P40/game')
    assert cwd_policy.matches('', 'C:/anything')
    assert not cwd_policy.matches('[', 'E:/P4')
    with pytest.raises(ValueError):
        cwd_policy.validate('[')


def test_override_save_and_cwd_regex_persistence(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    assert router_data.read_account_cwd('codex') == ''
    router_data.save_account_cwd('codex', '^E:/(P4|PROJECTS)(/|$)')
    data = router_data._read_json(target)['accounts']['codex']
    assert data['cwd_regex'] == '^E:/(P4|PROJECTS)(/|$)'
    assert account_fleet.load_accounts()['accounts']['codex']['cwd_regex'] == data['cwd_regex']
    before = target.read_bytes()
    with pytest.raises(ValueError):
        router_data.save_account_cwd('codex', '[')
    assert target.read_bytes() == before
    rows = [row for row in router_data.harness_model_rows() if row['account'] == 'codex']
    assert {row['cwd_regex'] for row in rows} == {data['cwd_regex']}


def test_plan_explains_regex_exclusion():
    scoring = json.loads(account_fleet.reference_fleet_path().read_text(encoding='utf-8'))['scoring']
    accounts = {'scoring': scoring, 'models': {'fixture-model': {}}, 'families': {'rows': []}, 'accounts': {
        'fixture-account': {'verified': True, 'cwd_regex': '^E:/P4(/|$)',
                            'bundles': {'fixture-bundle': {'model': 'fixture-model', 'effort': 'high'}}}}}
    plan = allocator.plan({'objective': 'Implement a parser', 'work_kind': 'general_implementation',
                           'expected_hours': .1, 'cwd': 'D:/work/AgentCompass', 'profile': profile()},
                          {'accounts': {}}, datetime(2026, 10, 28, tzinfo=timezone.utc),
                          accounts=accounts, capabilities={'routes': {'general_implementation': {}, 'other_or_mixed': {}}}, overrides=[], host_policy_config={'allowed_accounts': list(accounts['accounts'])})
    row = next(row for row in plan['ranking'] if row['id'] == 'fixture-bundle')
    assert 'outside account cwd restriction (^E:/P4(/|$))' in row['excluded']
