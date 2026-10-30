import copy
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_catalog
import router_data
from test_router_data_models import settings


def test_success_and_failure_are_cached_without_repeated_probes(tmp_path, monkeypatch):
    path = tmp_path / 'catalog.json'
    with mock.patch.object(model_catalog, 'query', return_value=[{'id': 'first', 'label': 'First'}]) as query:
        model_catalog.refresh(['codex'], path, now=10000)
        model_catalog.refresh(['codex'], path, now=10001)
        query.assert_called_once_with('codex')
    with mock.patch.object(model_catalog, 'query', side_effect=RuntimeError('offline')) as query:
        value = model_catalog.refresh(['codex'], path, now=10601)
        model_catalog.refresh(['codex'], path, now=10602)
        query.assert_called_once_with('codex')
    assert value['providers']['codex']['ok'] is False
    assert value['providers']['codex']['models'][0]['id'] == 'first'


def test_proxy_probe_uses_get_and_only_caches_model_metadata(tmp_path):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({'data': [{'id': 'model-a', 'secret': 'never-cache'}]}).encode()
    opener = mock.Mock()
    opener.open.return_value = response
    definition = {'endpoint': 'https://proxy.example.invalid/v1', 'api_key': 'fixture-secret'}
    with mock.patch.object(model_catalog.urllib.request, 'build_opener', return_value=opener):
        result = model_catalog.refresh(['llmproxy'], tmp_path / 'models.json', now=10000,
                                       definitions={'llmproxy': definition})
    request = opener.open.call_args.args[0]
    assert request.full_url == 'https://proxy.example.invalid/v1/models'
    assert request.get_method() == 'GET'
    assert request.get_header('Authorization') == 'Bearer fixture-secret'
    assert opener.open.call_args.kwargs['timeout'] == 8
    cache = (tmp_path / 'models.json').read_text()
    assert 'fixture-secret' not in cache and 'never-cache' not in cache
    assert result['providers']['llmproxy']['models'] == [{'id': 'model-a', 'label': 'model-a'}]


def test_manual_refresh_ignores_cache_age(tmp_path):
    path = tmp_path / 'models.json'
    with mock.patch.object(model_catalog, 'query', return_value=[]) as query:
        model_catalog.refresh(['codex'], path, now=10000)
        model_catalog.refresh(['codex'], path, now=10001, force=True)
    assert query.call_count == 2


def test_initial_probe_failure_is_cached_until_retry_interval(tmp_path):
    with mock.patch.object(model_catalog, 'query', side_effect=RuntimeError('offline')) as query:
        model_catalog.refresh(['codex'], tmp_path / 'cache.json', now=10000)
        model_catalog.refresh(['codex'], tmp_path / 'cache.json', now=10001)
        query.assert_called_once_with('codex')


def test_probe_registry_dispatch_defaults_and_explicit_static_override():
    assert model_catalog.probe_definition('custom', {'base_url': 'https://example.invalid'})['type'] == 'openai_compatible'
    assert model_catalog.probe_definition('codex')['type'] == 'paseo_provider'
    assert model_catalog.probe_definition('remote-bot')['type'] == 'static'
    definition = {'endpoint': 'https://example.invalid', 'model_probe': 'static',
                  'configured_models': [{'id': 'one', 'label': 'One'}]}
    assert model_catalog.query('any-provider', definition=definition) == [{'id': 'one', 'label': 'One'}]
    with mock.patch.dict(model_catalog.PROBE_TYPES, {'test_type': lambda spec: [{'id': spec['provider']}] } ):
        assert model_catalog.query('plugin', definition={'model_probe': 'test_type'}) == [{'id': 'plugin'}]


def test_union_marks_unrouted_and_missing_but_a_failed_probe_falls_back(tmp_path, monkeypatch):
    settings(tmp_path, monkeypatch)
    rows = router_data.harness_model_rows()
    accounts = router_data.modules()[0].load_accounts()
    catalog = {'providers': {'codex': {'ok': True, 'models': [{'id': 'new-model-high', 'label': 'New Model (High)'}]}}}
    decorated = router_data.decorate_model_rows(copy.deepcopy(rows), accounts, catalog)
    codex = next(row for row in decorated if row['harness'] == 'codex')
    by_model = {model['model']: model for model in codex['models']}
    assert by_model['gpt-6.1-sol']['availability'] == 'missing'
    assert by_model['new-model']['routed'] is False
    assert by_model['new-model']['enabled'] is False
    failed = router_data.decorate_model_rows(copy.deepcopy(rows), accounts, {'providers': {'codex': {'ok': False}}})
    codex = next(row for row in failed if row['harness'] == 'codex')
    assert len(codex['models']) == 2
    assert all(group['availability'] == 'unknown' for group in codex['models'])


def test_efforts_collapse_and_new_configured_bundles_become_selectable(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    rows = router_data.harness_model_rows()
    accounts = router_data.modules()[0].load_accounts()
    catalog = {'providers': {'codex': {'ok': True, 'models': [{'id': 'gpt-6.1-sol-high'}, {'id': 'gpt-6.1-sol-medium'}]}}}
    codex = next(row for row in router_data.decorate_model_rows(copy.deepcopy(rows), accounts, catalog) if row['harness'] == 'codex')
    assert len([model for model in codex['models'] if model['model'] == 'gpt-6.1-sol']) == 1
    assert codex['models'][0]['availability'] == 'detected'


def test_new_bundle_is_discovered_without_a_hardcoded_model_list(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    (target.parent / 'model-catalog.json').write_text(json.dumps({'providers': {
        'codex': {'ok': True, 'models': [{'id': 'new-model', 'label': 'New Model'}]}}}))
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    assert next(model for model in row['models'] if model['model'] == 'new-model')['routed'] is False
    reference = router_data._accounts_path()
    document = json.loads(reference.read_text())
    document['accounts']['codex']['bundles']['new_high'] = {'model': 'new-model', 'effort': 'high'}
    reference.write_text(json.dumps(document))
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    assert next(model for model in row['models'] if model['model'] == 'new-model')['routed'] is True
    router_data.toggle_harness_model('codex', 'codex', 'new-model', True)
    assert json.loads(target.read_text())['accounts']['codex']['bundles']['new_high']['disabled'] is False


def test_bulk_selection_changes_all_efforts_once_and_skips_unrouted(tmp_path, monkeypatch):
    target = settings(tmp_path, monkeypatch)
    (target.parent / 'model-catalog.json').write_text(json.dumps({'providers': {
        'codex': {'ok': True, 'models': [{'id': 'new-model', 'label': 'New Model'}]}}}))
    router_data.set_harness_models('codex', 'codex', 'clear')
    live = router_data.modules()[0].load_accounts()['accounts']['codex']['bundles']
    assert live['sol_high']['disabled'] and live['sol_medium']['disabled']
    assert not live['pi_sol_high'].get('disabled')
    router_data.set_harness_models('codex', 'codex', 'invert')
    live = router_data.modules()[0].load_accounts()['accounts']['codex']['bundles']
    assert live['sol_high']['disabled'] is False and live['experimental']['disabled'] is False
    router_data.set_harness_models('codex', 'codex', 'all')
    assert router_data.modules()[0].load_accounts()['accounts']['codex']['bundles']['sol_medium']['disabled'] is False
    assert 'new-model' not in json.loads(target.read_text())['accounts']['codex']['bundles']
