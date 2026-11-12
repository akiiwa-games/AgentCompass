"""Model settings preferences use temporary files, never the user's host policy."""
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import allocator
import account_fleet
import host_policy
import provider_inventory
import accounts_override
import router_data


def document():
    return {'scoring': {'safety_margin_pct': 5},
            'accounts': {'codex': {'verified': False, 'bundles': {
                'sol_medium': {'model': 'gpt-6.1-sol', 'effort': 'medium'},
                'sol_high': {'model': 'gpt-6.1-sol', 'effort': 'high'},
                'pi_sol_high': {'model': 'gpt-6.1-sol', 'provider': 'pi', 'effort': 'high'},
                'experimental': {'model': 'other', 'disabled': True}}}}}


def test_omitted_disabled_never_removes_new_default():
    base = document()
    accounts_override.apply(base, {'schema_version': 2, 'accounts': {'codex': {'bundles': {'experimental': {'model': 'other'}}}}})
    assert base['accounts']['codex']['bundles']['experimental']['disabled'] is True
    accounts_override.apply(base, {'schema_version': 2, 'accounts': {'codex': {'bundles': {'experimental': {'disabled': False}}}}})
    assert base['accounts']['codex']['bundles']['experimental']['disabled'] is False


def settings(tmp_path, monkeypatch):
    reference = tmp_path / 'references'
    reference.mkdir()
    (reference / 'accounts.json').write_text(json.dumps(document()))
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(account_fleet, 'REFERENCES', reference)
    monkeypatch.setenv('AGENTCOMPASS_ACCOUNTS_OVERRIDE', str(state / 'accounts-override.json'))
    monkeypatch.setenv('ROUTER_CAPACITY', str(state / 'capacity.json'))
    monkeypatch.setattr(provider_inventory, 'apply', lambda data: None)
    monkeypatch.setattr(host_policy, 'sync_discovered', lambda data: {})
    monkeypatch.setattr(host_policy, 'load_policy', lambda: None)
    monkeypatch.setattr(host_policy, 'policy_path', lambda: (state / 'host-policy.json', False))
    monkeypatch.setattr(router_data, 'STATE_DIR', state)
    monkeypatch.setattr(router_data, 'modules', lambda: (allocator, mock.Mock()))
    return state / 'accounts-override.json'


def test_first_write_is_sparse_and_partial_model_toggle_is_harness_scoped(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    router_data.toggle_bundle('codex', 'sol_high')
    assert json.loads(target.read_text()) == {'schema_version': 2, 'accounts': {'codex': {'bundles': {'sol_high': {'disabled': True}}}}}
    rows = router_data.harness_model_rows()
    codex = next(row for row in rows if row['harness'] == 'codex')
    assert codex['enabled'] is True  # verified=False must not uncheck user policy
    sol = next(model for model in codex['models'] if model['model'] == 'gpt-6.1-sol')
    assert sol['partial'] and not sol['enabled']
    assert router_data.toggle_harness_model('codex', 'codex', 'gpt-6.1-sol') is True
    assert router_data.toggle_harness_model('codex', 'codex', 'gpt-6.1-sol') is False
    live = account_fleet.load_accounts()['accounts']['codex']['bundles']
    assert live['sol_medium']['disabled'] and live['sol_high']['disabled']
    assert not live['pi_sol_high'].get('disabled')



def test_enabled_unverified_account_explains_exclusion(tmp_path, monkeypatch):
    settings(tmp_path, monkeypatch)
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    assert row['host_enabled'] and row['enabled']
    assert row['kind'] == 'warn'
    assert 'CLI' in row['badge'] and 'setup' in row['badge']


def test_legacy_flag_is_visible_and_checkbox_clears_it(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    target.write_text(json.dumps({'schema_version': 2, 'accounts': {'codex': {'verified': False, 'cwd_regex': 'work'}}}), encoding='utf-8')
    row = next(row for row in router_data.harness_model_rows() if row['account'] == 'codex' and row['harness'] == 'codex')
    assert row['kind'] == 'warn' and '旧的关闭标记残留' in row['badge']
    monkeypatch.setattr(router_data, 'host_policy_state', lambda: {'accounts': ['codex', 'other'], 'allowed': None, 'path': target.parent / 'host-policy.json', 'policy': None})
    with mock.patch.object(router_data, 'toggle_account_host') as account_toggle:
        router_data.toggle_harness('codex', 'codex', False)
    saved = json.loads(target.read_text(encoding='utf-8'))
    assert 'verified' not in saved['accounts']['codex']
    assert saved['accounts']['codex']['cwd_regex'] == 'work'
    # Cleanup does not widen the click: on a multi-harness account a row toggle stays row-scoped,
    # so the sibling row and the account's host policy are untouched.
    account_toggle.assert_not_called()
    assert saved['harness_overrides']['codex'] == {'codex': {'disabled': True}}
    rows = {row['harness']: row for row in router_data.harness_model_rows() if row['account'] == 'codex'}
    assert not rows['codex']['enabled'] and rows['pi']['enabled']


def test_legacy_flag_on_a_single_row_account_is_cleared_and_the_click_sets_host_policy(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    target.write_text(json.dumps({'schema_version': 2, 'accounts': {'codex': {'verified': False}}}), encoding='utf-8')
    rows = [row for row in router_data.harness_model_rows() if row['account'] == 'codex' and row['harness'] == 'codex']
    monkeypatch.setattr(router_data, 'harness_model_rows', lambda: rows)
    monkeypatch.setattr(router_data, 'host_policy_state', lambda: {'accounts': ['codex', 'other'], 'allowed': None, 'path': target.parent / 'host-policy.json', 'policy': None})
    router_data.toggle_harness('codex', 'codex', False)
    assert 'verified' not in json.loads(target.read_text(encoding='utf-8')).get('accounts', {}).get('codex', {})
    policy = json.loads((target.parent / 'host-policy.json').read_text(encoding='utf-8'))
    assert 'codex' not in policy['allowed_accounts'] and 'other' in policy['allowed_accounts']


def test_unavailable_account_stays_visible_with_reason(tmp_path, monkeypatch):
    settings(tmp_path, monkeypatch)
    def discover(data):
        data['_discovery'] = {'accounts': {'codex': {'family': 'codex', 'status': 'auth_failed', 'reason': 'expired login'}}}
    monkeypatch.setattr(provider_inventory, 'apply', discover)
    row = next(row for row in router_data.harness_model_rows() if row['account'] == 'codex')
    assert row['host_enabled'] and row['kind'] == 'warn'
    assert 'expired login' in row['badge']
    assert 'CLI' in row['badge']

def test_old_shape_override_and_string_disabled_are_rejected(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    for bad in (
        {'schema_version': 1, 'accounts': {'codex': {'bundles': {'sol_high': {'disabled': True}}}}},
        {'schema_version': 2, 'accounts': {'codex': {'bundles': {'sol_high': {'disabled': 'Legacy explanation'}}}}},
        {'schema_version': 2, 'harness_overrides': {'codex': {'pi': {'disabled': 'off'}}}},
    ):
        target.write_text(json.dumps(bad), encoding='utf-8')
        with pytest.raises(ValueError, match=r'accounts-override\.json'):
            account_fleet.load_accounts()


def test_direct_file_toggle_writes_explicit_boolean_state(tmp_path, monkeypatch):
    settings(tmp_path, monkeypatch)
    target = tmp_path / 'custom-accounts.json'
    data = document()
    target.write_text(json.dumps(data))
    router_data.toggle_bundle('codex', 'sol_high', target)
    saved = json.loads(target.read_text())['accounts']['codex']['bundles']
    assert saved['sol_high']['disabled'] is True
    assert saved['experimental']['disabled'] is True
    router_data.toggle_bundle('codex', 'sol_high', target)
    assert json.loads(target.read_text())['accounts']['codex']['bundles']['sol_high']['disabled'] is False
    router_data.save_tuning({'safety_margin_pct': 7}, target)
    assert json.loads(target.read_text())['accounts']['codex']['bundles']['experimental']['disabled'] is True


def test_harness_suspension_keeps_model_memory_and_other_harness(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    router_data.toggle_harness('pi', 'codex', False)
    live = account_fleet.load_accounts()['accounts']['codex']['bundles']
    assert live['pi_sol_high']['disabled']
    assert not live['sol_high'].get('disabled')
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'pi')
    assert not row['enabled'] and row['models'][0]['enabled']
    router_data.toggle_harness('pi', 'codex', True)
    assert not account_fleet.load_accounts()['accounts']['codex']['bundles']['pi_sol_high'].get('disabled')
    assert 'bundles' not in json.loads(target.read_text()).get('accounts', {}).get('codex', {})


def test_enabling_one_harness_on_a_denied_account_keeps_its_siblings_off(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    monkeypatch.setattr(host_policy, 'account_allowed', lambda *args: False)
    with mock.patch.object(router_data, 'toggle_account_host') as account_toggle:
        router_data.toggle_harness('pi', 'codex', True)
    account_toggle.assert_called_once_with('codex')
    preferences = json.loads(target.read_text())['harness_overrides']['codex']
    assert preferences == {'codex': {'disabled': True}, 'pi': {'disabled': False}}


def test_backup_and_tuning_restore_preserves_choices(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    initial = {'schema_version': 2, 'accounts': {'codex': {'bundles': {'sol_medium': {'disabled': True}}}}}
    original = json.dumps(initial).encode()
    target.write_bytes(original)
    router_data.save_tuning({'safety_margin_pct': 7})
    assert any(path.read_bytes() == original for path in (target.parent / 'backups').iterdir())
    saved = json.loads(target.read_text())
    assert saved['schema_version'] == 2 and 'models' not in saved
    assert router_data.read_tuning()['safety_margin_pct'] == 7
    router_data.restore_tuning()
    assert router_data.read_tuning()['safety_margin_pct'] == 5
    assert json.loads(target.read_text())['accounts']['codex']['bundles']['sol_medium']['disabled']


def test_account_policy_toggle_does_not_set_unverified_account_ready(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    monkeypatch.setattr(router_data, 'host_policy_state', lambda: {
        'accounts': ['codex', 'dot'], 'allowed': {'codex', 'dot'}, 'path': tmp_path / 'policy.json', 'policy': None})
    with mock.patch.object(router_data, 'save_host_policy') as save:
        router_data.toggle_account_host('codex')
    assert save.call_args.args[0] == ['dot']
    assert not target.exists()


def test_only_discovered_ready_execution_profiles_with_bundles_are_shown(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    status = {'second': 'disabled'}
    def discover(data):
        data['accounts']['codex-work'] = {'verified': False, 'bundles': {}}
        data['accounts']['codex']['bundles']['second_high'] = {'model': 'gpt-6.1-sol', 'provider': 'codex-work'}
        data['_discovery'] = {'accounts': {'codex': {'family': 'codex', 'status': 'ready'},
                                          'codex-work': {'family': 'codex', 'quota_account': 'codex', 'status': status['second']}}}
    monkeypatch.setattr(provider_inventory, 'apply', discover)
    monkeypatch.setattr(host_policy, 'load_policy', lambda: {
        'allowed_accounts': ['codex', 'codex-work', 'codex-team'], 'known_accounts': ['codex', 'codex-work', 'codex-team']})
    rows = {(row['account'], row['harness']): row for row in router_data.harness_model_rows()}
    assert ('codex-work', 'codex') not in rows and ('codex-work', 'pi') not in rows
    assert ('codex-team', 'codex') not in rows
    status['second'] = 'ready'
    rows = {(row['account'], row['harness']): row for row in router_data.harness_model_rows()}
    assert rows['codex-work', 'codex']['models'][0]['bundle_refs'] == [('codex', 'second_high')]
    assert 'second_high' not in rows['codex', 'codex']['models'][0]['bundle_ids']
    router_data.toggle_harness_model('codex-work', 'codex', 'gpt-6.1-sol', False)
    preferences = json.loads(target.read_text())
    assert preferences['accounts']['codex']['bundles']['second_high']['disabled']
    with mock.patch.object(router_data, 'toggle_account_host'):
        router_data.toggle_harness('codex', 'codex-work', False)
    live = account_fleet.load_accounts()['accounts']['codex']['bundles']
    assert live['second_high']['_harness_disabled']
    assert not live['sol_high'].get('disabled')


def test_discovered_model_checkbox_matches_routing_and_writes_only_the_boolean(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    reference = account_fleet.REFERENCES / 'accounts.json'
    doc = json.loads(reference.read_text())
    doc['superseded'] = {'gpt-5.6-sol': {'by': 'gpt-6.1-sol', 'basis': 'test'}}
    reference.write_text(json.dumps(doc))
    inventory = {'accounts': {'codex': {'family': 'codex', 'status': 'ready',
                                        'models': ['gpt-6.1-sol', 'gpt-5.6-sol', 'gpt-9-new']}}}
    monkeypatch.setattr(provider_inventory, 'apply',
                        lambda data: provider_inventory._apply_auto_bundles(data, inventory, False))

    def states():
        rows = router_data.harness_model_rows()
        tray = {m['model']: m['enabled'] for row in rows if (row['account'], row['harness']) == ('codex', 'codex')
                for m in row['models'] if m.get('routed', True)}
        live = account_fleet.bundles(account_fleet.load_accounts())
        routing = {b['model']: not b.get('disabled') for b in live.values() if b.get('auto') and b['harness'] == 'codex'}
        return tray, routing

    tray, routing = states()
    assert routing == {'gpt-5.6-sol': False, 'gpt-9-new': True}  # superseded unchecked, unknown new model checked
    assert {m: tray[m] for m in routing} == routing
    router_data.toggle_harness_model('codex', 'codex', 'gpt-5.6-sol', True)
    router_data.toggle_harness_model('codex', 'codex', 'gpt-9-new', False)
    tray, routing = states()
    assert routing == {'gpt-5.6-sol': True, 'gpt-9-new': False}
    assert {m: tray[m] for m in routing} == routing
    saved = json.loads(target.read_text())['accounts']['codex']['bundles']
    assert saved == {'auto:codex:codex:gpt-5.6-sol@high': {'disabled': False},
                     'auto:codex:codex:gpt-9-new@high': {'disabled': True}}


def test_family_shaped_discovered_model_keeps_its_own_checkbox(tmp_path, monkeypatch):
    """A family guess is its own checkbox. Unchecking it does not uncheck the series target.

    account_fleet.row_key keeps a family or unknown name; canonical folds it only for scoring.
    router-internals: a level the user unchecked belongs to that row's own model.
    """
    target = settings(tmp_path, monkeypatch)
    reference = account_fleet.REFERENCES / 'accounts.json'
    doc = json.loads(reference.read_text(encoding='utf-8'))
    doc['models'] = {'gpt-6-astra': {'api_family': 'codex'}}
    reference.write_text(json.dumps(doc), encoding='utf-8')
    inventory = {'accounts': {'codex': {'family': 'codex', 'status': 'ready', 'models': ['gpt-6-nova']}}}
    monkeypatch.setattr(provider_inventory, 'apply',
                        lambda data: provider_inventory._apply_auto_bundles(data, inventory, False))

    def groups():
        rows = router_data.harness_model_rows()
        return {m['model']: m for row in rows if (row['account'], row['harness']) == ('codex', 'codex')
                for m in row['models'] if m.get('routed', True)}

    before = groups()
    assert 'gpt-6-nova' in before and before['gpt-6-nova']['enabled'] is True
    assert 'gpt-6-astra' not in before
    assert router_data.toggle_harness_model('codex', 'codex', 'gpt-6-nova', False) is False
    saved = json.loads(target.read_text(encoding='utf-8'))['accounts']['codex']['bundles']
    assert saved == {'auto:codex:codex:gpt-6-nova@high': {'disabled': True}}
    live = account_fleet.bundles(account_fleet.load_accounts())
    assert not live['sol_high'].get('disabled')
    assert groups()['gpt-6.1-sol']['enabled'] is True
