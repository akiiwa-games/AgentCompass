"""An account (or model) held because its provider kept retrying without progress shows as unavailable in the account
list, with the provider's own message and when the router tries it again (2026-11-09, opencode free usage)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import router_data
import panel_quota

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'scripts'))
import account_fleet
import axis_scores
import host_policy
import model_latch
import provider_inventory
import provider_inventory as real_inventory
import usage_reading
import provider_retry

MESSAGE = 'Free usage exceeded, subscribe to Go'


class ProviderRetryCardTests(unittest.TestCase):
    def card(self, holds, row=None):
        router = Mock()
        inventory = Mock(quota_sources=real_inventory.quota_sources)
        accounts = {'accounts': {'opencode': {'bundles': {}}},
                    '_discovery': {'accounts': {'opencode': row or {'family': 'opencode', 'status': 'ready'}}}}
        router.usage_snapshot.return_value = {'accounts': {}}
        with patch.object(router_data, 'modules', return_value=(Mock(), router)), patch.object(usage_reading, 'usage_snapshot', router.usage_snapshot),              patch.object(account_fleet, 'load_accounts', return_value=accounts),              patch.object(axis_scores, 'five_axis_overlay'),              patch.object(host_policy, 'load_policy', return_value=None),              patch.object(host_policy, 'account_allowed', return_value=True),              patch.object(provider_retry, 'active', return_value=holds), \
             patch.dict(sys.modules, {'provider_inventory': inventory}), \
             patch.object(router_data, '_read_json', return_value={}), \
             patch.object(router_data, 'pool_reservations', return_value={}), \
             patch.object(router_data.router_priority, 'targets', return_value=[]), \
             patch.object(router_data.plugins, 'load', return_value={}):
            return router_data.account_cards()[0]

    def test_an_account_hold_marks_the_ready_account_unavailable_with_the_provider_message(self):
        card = self.card({'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE, 'retry_at': None}})
        self.assertEqual(card['kind'], 'warn')
        self.assertTrue(card['retry_hold'])
        self.assertIn(MESSAGE, card['badge'])
        self.assertIn('提供方持续重试，整个账号已排除派发', card['badge'])
        self.assertIn('agentcompass hold --clear opencode', card['badge'])   # no retry time: it says how to clear it
        self.assertNotIn('成功派发', card['badge'])
        self.assertNotEqual(panel_quota.card_title(card), card['label'])   # the card itself says it is in trouble

    def test_a_model_hold_names_the_model_and_its_next_retry(self):
        retry = datetime.now(timezone.utc) + timedelta(minutes=4)
        card = self.card({'opencode/glm-5.3-flash-free': {'account': 'opencode', 'model': 'glm-5.3-flash-free',
                                                          'message': MESSAGE, 'retry_at': retry.isoformat()}})
        self.assertEqual(card['kind'], 'warn')
        self.assertIn(MESSAGE, card['badge'])
        self.assertIn(retry.astimezone().strftime('%m-%d %H:%M'), card['badge'])

    def test_a_failed_turn_hold_says_the_provider_errored(self):
        message = 'Free usage limit reached. Connect a provider or subscribe to OpenCode Go to keep going, or switch models.'
        card = self.card({'opencode': {'account': 'opencode', 'model': None, 'message': message, 'retry_at': None,
                                       'failure_code': 'provider_turn_failed'}})
        self.assertIn('提供方报错，整个账号已排除派发', card['badge'])
        self.assertIn(message, card['badge'])

    def test_an_account_hold_widened_from_guesses_shows_its_retry_time(self):
        # Review 2 R2: only a classifier `account` answer is indefinite; a widened one backs off.
        retry = datetime.now(timezone.utc) + timedelta(minutes=2)
        card = self.card({'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE, 'scope': 'unknown',
                                       'scope_source': 'widened', 'retry_at': retry.isoformat()}})
        self.assertIn('整个账号已排除派发', card['badge'])
        self.assertIn(retry.astimezone().strftime('%m-%d %H:%M'), card['badge'])

    def test_the_clear_shows_a_sentence_in_the_status_bar_not_the_bare_result(self):
        # Review 2 R6: the status bar showed "1" (True) after Clear.
        panel = Mock()
        for cleared in (True, False):
            panel.data_module.return_value.clear_retry_holds.return_value = cleared
            panel_quota.QuotaPage.clear_retry_hold(panel, 'opencode')
            work = panel.run.call_args.args[0]
            text = work()
            self.assertIsInstance(text, str)
            self.assertEqual(text, panel_quota._tr('已清除提供方失败标记' if cleared else '没有需要清除的提供方失败标记'))

    def test_the_card_clear_button_shows_only_while_held_and_clears_the_account(self):
        clear = Mock()
        widgets = {'clear': clear, 'clear_layout': 'pack', 'windows': [], 'refreshable': False}
        card = {'account': 'opencode', 'label': 'OpenCode', 'kind': 'warn', 'badge': 'x', 'windows': [],
                'retry_hold': True}
        panel_quota.QuotaPage.fill_quota_card(widgets, card)
        clear.pack.assert_called_once_with(side='left')
        panel_quota.QuotaPage.fill_quota_card(widgets, {**card, 'retry_hold': False})
        clear.pack_forget.assert_called_once_with()
        grid = Mock()
        panel_quota.QuotaPage.fill_quota_card({'clear': grid, 'clear_layout': 'grid', 'windows': [], 'refreshable': False},
                                            card)
        grid.grid.assert_called_once_with()
        with patch.object(router_data, 'modules', return_value=(Mock(), Mock())),                 patch.object(provider_retry, 'clear') as clear_holds:
            router_data.clear_retry_holds('opencode')
        self.assertEqual(clear_holds.call_args.args, ('opencode',))

    def test_a_pool_hold_names_the_pool_and_the_clear_link_clears_that_pool(self):
        # 2026-11-09: OpenCode Go and the Zen free models are separate quotas under one provider.
        pool = {'id': 'opencode', 'prefix': 'opencode', 'provider': 'opencode', 'models': ['opencode/beam-free']}
        holds = {'opencode#opencode': {'account': 'opencode', 'model': None, 'pool': pool, 'message': MESSAGE,
                                       'retry_at': None, 'failure_code': 'provider_turn_failed'}}
        card = self.card(holds)
        self.assertEqual((card['kind'], card['retry_hold'], card['retry_clear_pool']), ('warn', True, 'opencode'))
        self.assertIn('提供方报错，额度池 opencode 已排除派发', card['badge'])
        self.assertIn('agentcompass hold --clear opencode#opencode', card['badge'])
        self.assertNotIn('整个账号', card['badge'])
        retry = datetime.now(timezone.utc) + timedelta(minutes=2)
        go = {'id': 'opencode-go', 'prefix': 'opencode-go', 'provider': 'opencode', 'models': []}
        both = self.card({**holds, 'opencode#opencode-go': {'account': 'opencode', 'model': None, 'pool': go,
                                                            'message': 'Go limit', 'retry_at': retry.isoformat()}})
        self.assertIn('额度池 opencode-go 已排除派发：Go limit', both['badge'])
        self.assertEqual(both['retry_clear_pool'], 'opencode')   # one pool at a time, the one the link names
        whole = self.card({**holds, 'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE}})
        self.assertIsNone(whole['retry_clear_pool'])   # the whole account is held: the link clears everything

        clear = Mock()
        widgets = {'clear': clear, 'clear_layout': 'pack', 'windows': [], 'refreshable': False}
        panel_quota.QuotaPage.fill_quota_card(widgets, {**card, 'label': 'OpenCode'})
        self.assertEqual(clear.hold_pool, 'opencode')
        clear.configure.assert_called_with(text=panel_quota._tr('清除 {0}').format('opencode'))
        panel_quota.QuotaPage.fill_quota_card(widgets, {**card, 'label': 'OpenCode', 'retry_clear_pool': None})
        self.assertIsNone(clear.hold_pool)
        clear.configure.assert_called_with(text=panel_quota._tr('清除'))

        panel = Mock()
        panel_quota.QuotaPage.clear_retry_hold(panel, 'opencode', 'opencode')
        panel.run.call_args.args[0]()
        panel.data_module.return_value.clear_retry_holds.assert_called_once_with('opencode', 'opencode')
        with patch.object(router_data, 'modules', return_value=(Mock(), Mock())),                 patch.object(provider_retry, 'clear') as clear_holds:
            router_data.clear_retry_holds('opencode', 'opencode')
        self.assertEqual(clear_holds.call_args.args, ('opencode',))
        self.assertEqual(clear_holds.call_args.kwargs['pool'], 'opencode')

    def test_the_model_list_marks_the_models_of_a_held_pool_only(self):
        pool = {'id': 'opencode', 'prefix': 'opencode', 'provider': 'opencode', 'models': ['opencode/beam-free']}
        holds = {'opencode#opencode': {'account': 'opencode', 'model': None, 'pool': pool, 'message': MESSAGE,
                                       'retry_at': None}}
        zen = [('opencode', 'z', {'provider': 'opencode', 'model': 'iceman-free', 'launch_model': 'opencode/iceman-free'})]
        go = [('opencode', 'g', {'provider': 'opencode', 'model': 'kimi-k3', 'launch_model': 'opencode-go/kimi-k3'})]
        text = router_data.retry_model_text(holds, 'opencode', zen)
        self.assertIn('额度池 opencode 提供方持续重试，已排除派发', text)
        self.assertIsNone(router_data.retry_model_text(holds, 'opencode', go))
        # A hold of the whole account badges the row, not each model.
        self.assertIsNone(router_data.retry_model_text(
            {'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE}}, 'opencode', zen))

    def test_no_hold_leaves_the_card_alone_and_another_problem_keeps_its_text(self):
        card = self.card({})
        self.assertEqual((card['kind'], card['retry_hold']), ('ok', False))
        failing = self.card({'opencode': {'account': 'opencode', 'model': None, 'message': MESSAGE}},
                            row={'family': 'opencode', 'status': 'auth_failed', 'reason': 'credentials_rejected'})
        self.assertIn('credentials_rejected', failing['badge'])
        self.assertIn(MESSAGE, failing['badge'])


def test_the_model_list_marks_a_held_model_and_badges_a_held_account(tmp_path, monkeypatch):
    from test_model_catalog import settings
    import provider_retry
    target = settings(tmp_path, monkeypatch)
    store = target.parent / provider_retry.FILE
    monkeypatch.setenv('ROUTER_PROVIDER_RETRY', str(store))
    monkeypatch.setattr(router_data, 'account_badge', lambda *args: ('就绪', 'ok'))
    at = datetime.now(timezone.utc)
    bundle = {'account': 'codex', 'provider': 'codex', 'model': 'gpt-6.1-sol'}
    entry = provider_retry.record(bundle, {'failure_code': 'provider_retry_stuck', 'provider_message': MESSAGE,
                                           'at': at.isoformat()},
                                  dispatch_id='d1', classify=lambda *args: ('model', {'source': 'system_one'}))
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    sol = next(model for model in row['models'] if model['model'] == 'gpt-6.1-sol')
    retry = datetime.fromisoformat(entry['retry_at']).astimezone().strftime('%m-%d %H:%M')
    assert sol['launch_unavailable'] == f'提供方持续重试，已排除派发，下次重试 {retry}'
    assert router_data.clear_retry_holds('codex') is True
    assert not any(model.get('launch_unavailable') for model in next(
        row for row in router_data.harness_model_rows() if row['harness'] == 'codex')['models'])
    entry = provider_retry.record(bundle, {'failure_code': 'provider_retry_stuck', 'provider_message': MESSAGE,
                                           'at': at.isoformat()},
                                  dispatch_id='d1b', classify=lambda *args: ('model', {'source': 'system_one'}))
    assert not target.exists()   # a hold is not a setting
    provider_retry.record({**bundle, 'model': 'gpt-6.1-terra'}, {'failure_code': 'provider_retry_stuck',
                          'provider_message': MESSAGE, 'at': at.isoformat()},
                          dispatch_id='d2', classify=lambda *args: ('account', {'source': 'system_one'}))
    row = next(row for row in router_data.harness_model_rows() if row['harness'] == 'codex')
    assert row['kind'] == 'warn' and MESSAGE in row['badge']
    assert json.loads(store.read_text(encoding='utf-8'))['entries']['codex']['retry_at'] is None


def test_account_cards_and_model_rows_surface_live_model_exclusions_and_unreadable_lists(tmp_path, monkeypatch):
    from test_model_catalog import settings
    import live_models
    import provider_retry
    target = settings(tmp_path, monkeypatch)
    live_store = target.parent / live_models.FILE
    retry_store = target.parent / provider_retry.FILE
    monkeypatch.setenv('AGENTCOMPASS_LIVE_MODELS', str(live_store))
    monkeypatch.setenv('ROUTER_PROVIDER_RETRY', str(retry_store))
    monkeypatch.setattr(router_data, 'account_badge', lambda *args: ('就绪', 'ok'))
    monkeypatch.setattr(router_data, 'pool_reservations', lambda **kw: {})
    monkeypatch.setattr(router_data.router_priority, 'targets', lambda *args, **kw: [])
    monkeypatch.setattr(router_data.plugins, 'load', lambda: {})
    alloc, _ = router_data.modules()
    rtr_mock = Mock()
    rtr_mock.usage_snapshot.return_value = {'accounts': {}}
    monkeypatch.setattr(router_data, 'modules', lambda: (alloc, rtr_mock))
    monkeypatch.setattr(usage_reading, 'usage_snapshot', rtr_mock.usage_snapshot)
    monkeypatch.setattr(provider_inventory, 'current', lambda: {'accounts': {
        'antigravity': {'family': 'antigravity', 'status': 'ready', 'identity': 'id-agy'},
        'codex': {'family': 'codex', 'status': 'ready', 'identity': 'id-codex'},
    }})
    (account_fleet.REFERENCES / 'accounts.json').write_text(json.dumps({
        'scoring': {'safety_margin_pct': 5},
        'families': {'rows': []},
        'models': {
            'gemini-4-argon-high': {'label': 'Gemini 4 Argon (High)'},
            'gemini-3.8-flash-high': {'label': 'Gemini 3.8 Flash (High)'},
            'gpt-6.1-sol': {'label': 'GPT-6.1 Sol'},
        },
        'accounts': {
            'antigravity': {
                'verified': True,
                'bundles': {
                    'argon_high': {'provider': 'antigravity-resilient', 'model': 'gemini-4-argon-high', 'effort': 'high'},
                    'flash_high': {'provider': 'antigravity-resilient', 'model': 'gemini-3.8-flash-high', 'effort': 'high'},
                },
            },
            'codex': {
                'verified': True,
                'bundles': {
                    'sol_high': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'},
                },
            },
        },
    }), encoding='utf-8')
    router_data._invalidate_accounts_cache()
    now = datetime.now(timezone.utc)

    # 1. Provider no longer offers gemini-4-argon-high on antigravity-resilient, and codex live list is unavailable.
    live_store.write_text(json.dumps({
        'updated_at': now.isoformat(),
        'ttl_seconds': 60,
        'providers': {
            'antigravity-resilient': {
                'ok': True,
                'status': 'ready',
                'fetched_at': now.isoformat(),
                'models': [{'id': 'gemini-3.8-flash-high'}],
            },
            'codex': {
                'ok': False,
                'status': 'unavailable',
                'error': 'model_list_unavailable',
                'fetched_at': now.isoformat(),
                'models': [],
            },
        },
    }), encoding='utf-8')
    live_models._MEM_CACHE.clear()

    cards = {c['account']: c for c in router_data.account_cards()}
    assert cards['antigravity']['kind'] == 'warn'
    assert 'provider no longer offers gemini-4-argon-high' in cards['antigravity']['badge']
    assert cards['codex']['kind'] == 'warn'
    assert 'model list unavailable' in cards['codex']['badge']

    rows = {(r['account'], r['harness']): r for r in router_data.harness_model_rows()}
    agy_row = rows[('antigravity', 'antigravity-resilient')]
    argon_group = next(m for m in agy_row['models'] if 'argon' in m['model'])
    assert 'provider no longer offers gemini-4-argon-high' in argon_group['launch_unavailable']
    flash_group = next(m for m in agy_row['models'] if 'flash' in m['model'])
    assert 'launch_unavailable' not in flash_group
    codex_row = rows[('codex', 'codex')]
    assert codex_row['kind'] == 'warn'
    assert 'model list unavailable' in codex_row['badge']

    # 2. A provider Paseo does not know (a LiteLLM proxy account) is neither warned about nor excluded.
    live_store.write_text(json.dumps({'providers': {
        'antigravity-resilient': {'ok': True, 'status': 'ready', 'fetched_at': now.isoformat(),
                                  'models': [{'id': 'gemini-3.8-flash-high'}, {'id': 'gemini-4-argon-high'}]},
        'codex': {'ok': False, 'status': 'unknown_provider', 'error': 'unknown_provider',
                  'fetched_at': now.isoformat(), 'models': []},
    }}), encoding='utf-8')
    live_models._MEM_CACHE.clear()
    cards = {c['account']: c for c in router_data.account_cards()}
    assert (cards['antigravity']['kind'], cards['codex']['kind']) == ('ok', 'ok'), cards
    rows = {(r['account'], r['harness']): r for r in router_data.harness_model_rows()}
    assert rows[('codex', 'codex')]['kind'] == 'ok'
    assert not any(m.get('launch_unavailable') for r in rows.values() for m in r['models'])

    # 3. INVALID_MODEL is one mechanism: the model latch. Its note is the model row's only note.
    latch_store = target.parent / 'model-unavailable.json'
    monkeypatch.setenv('ROUTER_MODEL_LATCH', str(latch_store))
    model_latch.record({'account': 'antigravity', 'provider': 'antigravity-resilient',
                              'model': 'gemini-4-argon-high'}, 'INVALID_MODEL', source='d-invalid-tray')
    live_models.invalidate('antigravity-resilient')
    assert provider_retry.active() == {}
    with patch.object(live_models, 'fetch_from_paseo', return_value={'ok': True, 'providers': {
            'antigravity-resilient': {'ok': True, 'models': [{'id': 'gemini-3.8-flash-high'}]}}}):
        rows = {(r['account'], r['harness']): r for r in router_data.harness_model_rows()}
        argon_group = next(m for m in rows[('antigravity', 'antigravity-resilient')]['models'] if 'argon' in m['model'])
        latch_note = router_data.launch_unavailable_text(model_latch.active(), 'antigravity',
                                                         [('antigravity', 'argon_high', {'model': 'gemini-4-argon-high'})])
        assert latch_note and argon_group['launch_unavailable'] == latch_note
        thread = router_data._LIVE_REFRESH['thread']
        if thread is not None:
            thread.join(5)


def test_the_tray_never_calls_the_bridge_on_its_own_thread(tmp_path, monkeypatch):
    """A hung Paseo held harness_model_rows for ~10 s on the Tk thread (review, 2026-11-10). The tray reads the cache
    and refreshes it on a background thread."""
    import threading
    import time
    from test_model_catalog import settings
    import live_models
    target = settings(tmp_path, monkeypatch)
    live_store = target.parent / live_models.FILE
    monkeypatch.setenv('AGENTCOMPASS_LIVE_MODELS', str(live_store))
    alloc, _ = router_data.modules()
    accounts = {'accounts': {'antigravity': {'verified': True, 'bundles': {
        'argon_high': {'provider': 'antigravity-resilient', 'model': 'gemini-4-argon-high'}}}}}
    release, callers = threading.Event(), []

    def hung_bridge(providers, **_):
        callers.append(threading.current_thread())
        release.wait(10)
        return {'ok': True, 'providers': {'antigravity-resilient': {'ok': True, 'models': [{'id': 'gemini-3.8-flash-high'}]}}}

    with patch.object(live_models, 'fetch_from_paseo', hung_bridge):
        started = time.monotonic()
        assert router_data.live_model_state(accounts) == {'providers': {}}
        assert router_data.live_model_state(accounts) == {'providers': {}}  # one refresh at a time
        assert time.monotonic() - started < 1.0
        release.set()
        router_data._LIVE_REFRESH['thread'].join(5)
    assert len(callers) == 1 and callers[0] is not threading.current_thread()
    live_models._MEM_CACHE.clear()
    doc = router_data.live_model_state(accounts)
    assert doc['providers']['antigravity-resilient']['models'] == [{'id': 'gemini-3.8-flash-high'}]


if __name__ == '__main__':
    unittest.main()
