"""Model settings preferences use temporary files, never the user's host policy."""
import copy
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import allocator
import accounts_override
import router_data


def document():
    return {'scoring': {'safety_margin_pct': 5, 'profile': {'subjective': {'weight': .49, 'vector_weight': .34, 'route_weight': .17}}},
            'models': {'gpt-6.1-sol': {'subjective': {'divergence': .5, 'global_rigor': .6}}},
            'accounts': {'codex': {'verified': False, 'bundles': {
                'sol_medium': {'model': 'gpt-6.1-sol', 'effort': 'medium'},
                'sol_high': {'model': 'gpt-6.1-sol', 'effort': 'high'},
                'pi_sol_high': {'model': 'gpt-6.1-sol', 'provider': 'pi', 'effort': 'high'},
                'experimental': {'model': 'other', 'disabled': True}}}}}


def test_migration_preserves_explicit_choices_and_reports_ambiguity():
    base = document()
    base['accounts']['codex']['bundles']['experimental']['disabled'] = 'Reserved for the primary quota account'
    legacy = copy.deepcopy(base)
    legacy['accounts']['codex']['bundles']['sol_high']['disabled'] = 'User selected off'
    legacy['accounts']['codex']['bundles']['sol_medium']['disabled'] = False
    legacy['accounts']['codex']['bundles']['experimental'].pop('disabled')
    legacy['scoring']['profile']['subjective']['weight'] = .7
    migrated = accounts_override.migrate(legacy, base)
    assert migrated['schema_version'] == 2
    assert migrated['scoring'] == {'profile': {'subjective': {'weight': .7}}}
    assert migrated['accounts']['codex']['bundles'] == {
        'sol_medium': {'disabled': False}, 'sol_high': {'disabled': True}}
    assert migrated['migration_ambiguous_enabled'] == [{'account': 'codex', 'bundle': 'experimental'}]
    accounts_override.apply(base, migrated)
    assert base['accounts']['codex']['bundles']['experimental']['disabled'] is True
    assert 'disabled' not in base['accounts']['codex']['bundles']['pi_sol_high']


def test_migration_preserves_custom_provider_definitions_and_extensions():
    base = document()
    original = copy.deepcopy(base)
    custom = {'backend_kind': 'litellm', 'endpoint': 'https://example.invalid',
              'api_key_env': 'PROXY_KEY', 'models': ['gpt-5.6-sol'], 'allowed_cwd': ['E:/workspace'],
              'bundles': {'custom_model': {'model': 'gpt-5.6-sol', 'disabled': False}}}
    original['accounts']['local-proxy'] = custom
    original['litellm_proxies'] = {'another-proxy': {'api_key_env': 'SECOND_KEY'}}
    migrated = accounts_override.migrate(original, base)
    assert migrated['accounts']['local-proxy'] == custom
    assert migrated['litellm_proxies'] == original['litellm_proxies']


def test_omitted_disabled_never_removes_new_default():
    base = document()
    accounts_override.apply(base, {'accounts': {'codex': {'bundles': {'experimental': {'model': 'other'}}}}})
    assert base['accounts']['codex']['bundles']['experimental']['disabled'] is True
    accounts_override.apply(base, {'accounts': {'codex': {'bundles': {'experimental': {'disabled': False}}}}})
    assert base['accounts']['codex']['bundles']['experimental']['disabled'] is False


def settings(tmp_path, monkeypatch):
    reference = tmp_path / 'references'
    reference.mkdir()
    (reference / 'accounts.json').write_text(json.dumps(document()))
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(allocator, 'REFERENCES', reference)
    monkeypatch.setenv('ROUTER_ACCOUNTS_OVERRIDE', str(state / 'accounts-override.json'))
    monkeypatch.setenv('ROUTER_CAPACITY', str(state / 'capacity.json'))
    monkeypatch.setattr(allocator.provider_inventory, 'apply', lambda data: None)
    monkeypatch.setattr(allocator.host_policy, 'sync_discovered', lambda data: {})
    monkeypatch.setattr(allocator.host_policy, 'load_policy', lambda: None)
    monkeypatch.setattr(allocator.host_policy, 'policy_path', lambda: (state / 'host-policy.json', False))
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
    live = allocator.load_accounts()['accounts']['codex']['bundles']
    assert live['sol_medium']['disabled'] and live['sol_high']['disabled']
    assert not live['pi_sol_high'].get('disabled')


def test_four_ability_ratings_roundtrip_and_economy_is_not_editable(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    ratings = {'divergence': .8, 'global_rigor': .7, 'module_precision': .9, 'capability': .34}
    router_data.save_strategy({}, 'gpt-6.1-sol', {**ratings, 'economy': 0})
    saved = json.loads(target.read_text())['models']['gpt-6.1-sol']['subjective']
    assert saved == ratings
    info = router_data.model_table()['gpt-6.1-sol']
    assert info['subjective'] == ratings
    assert info['default_subjective'] == {'divergence': .5, 'global_rigor': .6}
    assert router_data.radar_chart.dna_dimensions(info)['capability'] == .34
    # Legacy three-field callers must not erase a saved fourth rating.
    router_data.save_strategy({}, 'gpt-6.1-sol', {k: v for k, v in ratings.items() if k != 'capability'})
    assert router_data.model_table()['gpt-6.1-sol']['subjective']['capability'] == .34
    router_data.save_model_subjective('gpt-6.1-sol', .8, .7, .9, capability=0)
    assert router_data.model_table()['gpt-6.1-sol']['subjective']['capability'] == 0
    router_data.save_model_subjective('gpt-6.1-sol', .8, .7, .9)
    assert router_data.model_table()['gpt-6.1-sol']['subjective']['capability'] == 0


def test_legacy_disabled_strings_load_and_save_as_boolean_state(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    for version in (1, 2):
        for old_value in ('Legacy explanation', ''):
            original = {'schema_version': version, 'accounts': {'codex': {'bundles': {
                'sol_high': {'disabled': old_value}, 'sol_medium': {'disabled': False}}}},
                'harness_overrides': {'codex': {'pi': {'disabled': old_value}}}}
            target.write_text(json.dumps(original))
            live = allocator.load_accounts()['accounts']['codex']['bundles']
            assert live['sol_high']['disabled'] is True
            assert live['sol_medium']['disabled'] is False
            assert live['pi_sol_high']['disabled'] is True
            assert live['pi_sol_high']['_model_disabled'] is False
            rows = router_data.harness_model_rows()
            assert not next(row for row in rows if row['harness'] == 'pi')['enabled']
            # An unrelated save must not carry legacy explanations back to disk.
            router_data.save_tuning({'safety_margin_pct': 7})
            saved = json.loads(target.read_text())
            assert saved['accounts']['codex']['bundles'] == {
                'sol_high': {'disabled': True}, 'sol_medium': {'disabled': False}}
            assert saved['harness_overrides']['codex']['pi']['disabled'] is True
            assert old_value == '' or old_value not in target.read_text()


def test_direct_file_toggle_normalizes_other_flags_and_writes_explicit_state(tmp_path, monkeypatch):
    settings(tmp_path, monkeypatch)
    target = tmp_path / 'custom-accounts.json'
    data = document()
    data['accounts']['codex']['bundles']['experimental']['disabled'] = 'Legacy explanation'
    data['accounts']['codex']['bundles']['sol_high']['disabled'] = ''
    target.write_text(json.dumps(data))
    router_data.toggle_bundle('codex', 'sol_high', target)
    saved = json.loads(target.read_text())['accounts']['codex']['bundles']
    assert saved['sol_high']['disabled'] is False
    assert saved['experimental']['disabled'] is True
    router_data.toggle_bundle('codex', 'sol_high', target)
    assert json.loads(target.read_text())['accounts']['codex']['bundles']['sol_high']['disabled'] is True
    data = json.loads(target.read_text())
    data['accounts']['codex']['bundles']['experimental']['disabled'] = 'Legacy explanation'
    target.write_text(json.dumps(data))
    router_data.save_tuning({'safety_margin_pct': 7}, target)
    assert json.loads(target.read_text())['accounts']['codex']['bundles']['experimental']['disabled'] is True


def test_harness_suspension_keeps_model_memory_and_other_harness(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    router_data.toggle_harness('pi', 'codex', False)
    live = allocator.load_accounts()['accounts']['codex']['bundles']
    assert live['pi_sol_high']['disabled']
    assert not live['sol_high'].get('disabled')
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'pi')
    assert not row['enabled'] and row['models'][0]['enabled']
    router_data.toggle_harness('pi', 'codex', True)
    assert not allocator.load_accounts()['accounts']['codex']['bundles']['pi_sol_high'].get('disabled')
    assert 'bundles' not in json.loads(target.read_text()).get('accounts', {}).get('codex', {})


def test_enabling_one_harness_on_a_denied_account_keeps_its_siblings_off(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    monkeypatch.setattr(allocator.host_policy, 'account_allowed', lambda *args: False)
    with mock.patch.object(router_data, 'toggle_account_host') as account_toggle:
        router_data.toggle_harness('pi', 'codex', True)
    account_toggle.assert_called_once_with('codex')
    preferences = json.loads(target.read_text())['harness_overrides']['codex']
    assert preferences == {'codex': {'disabled': True}, 'pi': {'disabled': False}}


def test_legacy_backup_precedes_migration_and_tuning_restore_preserves_choices(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    legacy = document()
    legacy['accounts']['codex']['bundles']['sol_medium']['disabled'] = True
    original = json.dumps(legacy).encode()
    target.write_bytes(original)
    router_data.save_tuning({'safety_margin_pct': 7})
    assert any(path.read_bytes() == original for path in (target.parent / 'backups').iterdir())
    migrated = json.loads(target.read_text())
    assert migrated['schema_version'] == 2 and 'models' not in migrated
    assert router_data.read_tuning()['subjective.vector_weight'] == .34
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


def test_sparse_subjective_merges_without_losing_unmodified_dimensions(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    target.write_text(json.dumps({'schema_version': 2, 'models': {'gpt-6.1-sol': {'subjective': {'divergence': .9}}}}))
    subjective = allocator.load_accounts()['models']['gpt-6.1-sol']['subjective']
    assert subjective == {'divergence': .9, 'global_rigor': .6}


def test_strategy_save_validates_everything_before_one_atomic_write(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    ratings = {'divergence': .8, 'global_rigor': .7, 'module_precision': .9}
    router_data.save_strategy({'safety_margin_pct': 8}, 'gpt-6.1-sol', ratings)
    saved = json.loads(target.read_text())
    assert saved['scoring'] == {'safety_margin_pct': 8}
    assert saved['models']['gpt-6.1-sol']['subjective'] == ratings
    before = target.read_bytes()
    import pytest
    with pytest.raises(ValueError):
        router_data.save_strategy({'safety_margin_pct': 9}, 'gpt-6.1-sol', {'divergence': 'bad'})
    assert target.read_bytes() == before


def test_only_discovered_ready_execution_profiles_with_bundles_are_shown(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    status = {'second': 'disabled'}
    def discover(data):
        data['accounts']['codex-work'] = {'verified': False, 'bundles': {}}
        data['accounts']['codex']['bundles']['second_high'] = {'model': 'gpt-6.1-sol', 'provider': 'codex-work'}
        data['_discovery'] = {'accounts': {'codex': {'family': 'codex', 'status': 'ready'},
                                          'codex-work': {'family': 'codex', 'quota_account': 'codex', 'status': status['second']}}}
    monkeypatch.setattr(allocator.provider_inventory, 'apply', discover)
    monkeypatch.setattr(allocator.host_policy, 'load_policy', lambda: {
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
    live = allocator.load_accounts()['accounts']['codex']['bundles']
    assert live['second_high']['_harness_disabled']
    assert not live['sol_high'].get('disabled')
