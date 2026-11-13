"""Provider holds per quota pool (2026-11-09): one Paseo provider can hold several independent quotas. OpenCode's Zen free
models (`opencode/...`) and OpenCode Go (`opencode-go/...`) are separate products under provider `opencode`;
Antigravity's Gemini models and its third-party (Claude/GPT) models draw different windows. A hold on one pool leaves
the others dispatchable; an account with a single pool is held as a whole, as before."""
from datetime import datetime, timedelta, timezone
import json

import pytest

import account_fleet
import auto_bundles
import provider_retry

MESSAGE = 'Free usage limit reached. Connect a provider or subscribe to OpenCode Go to keep going, or switch models.'
QUOTA = 'You have reached the quota for these models. It resets in 4 hours.'
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
ZEN = ('opencode/deepseek-v4.1-flash-free', 'opencode/glm-5.3-flash-free')
GO = ('opencode-go/glm-5.3', 'opencode-go/kimi-k3')
GEMINI_POOL, THIRD_PARTY_POOL = 'gemini-5h+gemini-weekly', '3p-5h+3p-weekly'


@pytest.fixture
def accounts():
    """The shipped fleet, with OpenCode Go connected: Paseo lists its models under provider `opencode` (mocked)."""
    fleet = account_fleet.load_accounts(account_fleet.reference_fleet_path())
    catalog = {'providers': {'opencode': {'ok': True, 'type': 'paseo_provider', 'checked_at': 1,
                                          'models': [{'id': m} for m in (*ZEN, *GO)]}}}
    return auto_bundles.merge_into(fleet, auto_bundles.derive(fleet, catalog, 2))


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / provider_retry.FILE
    monkeypatch.setenv('ROUTER_PROVIDER_RETRY', str(path))
    return path


def bundle(accounts, launch):
    return next(b for b in account_fleet.bundles(accounts).values() if provider_retry.launch_model(b) == launch)


def hold(accounts, launch, scope, dispatch_id, at=NOW, message=MESSAGE):
    return provider_retry.record(bundle(accounts, launch), {'failure_code': 'provider_turn_failed',
                                                            'provider_message': message, 'at': at.isoformat()},
                                 dispatch_id=dispatch_id, accounts=accounts,
                                 classify=lambda *args: (scope, {'source': 'system_one'}))


def held_out(accounts, launch, at=NOW):
    return provider_retry.reason(bundle(accounts, launch), provider_retry.active(now=at))


def test_discovery_places_go_models_beside_zen_ones_and_the_prefix_splits_the_pools(accounts):
    zen, go = (provider_retry.pool_of(bundle(accounts, m), accounts) for m in (ZEN[0], GO[0]))
    assert (zen['id'], zen['prefix'], zen['provider']) == ('opencode', 'opencode', 'opencode')
    assert (go['id'], go['prefix'], set(go['models'])) == ('opencode-go', 'opencode-go', set(GO))
    assert set(ZEN) <= set(zen['models']) and not set(GO) & set(zen['models'])


def test_an_opencode_zen_quota_hold_leaves_go_models_dispatchable(accounts, store):
    entry = hold(accounts, ZEN[0], 'account', 'd1')
    assert (entry['model'], entry['retry_at'], entry['pool']['id']) == (None, None, 'opencode')
    assert set(provider_retry.active(now=NOW)) == {'opencode#opencode'}
    assert provider_retry.indefinite(entry)
    for zen in ZEN:
        reason = held_out(accounts, zen, NOW + timedelta(days=30))
        assert reason and 'quota pool opencode' in reason and 'agentcompass hold --clear opencode#opencode' in reason
    for go in GO:
        assert held_out(accounts, go) is None
    # A Zen model that appears after the hold draws on the same pool: its prefix says so.
    assert provider_retry.reason({'account': 'opencode', 'provider': 'opencode', 'model': 'beam-free',
                                  'launch_model': 'opencode/beam-free'}, provider_retry.active(now=NOW))
    assert provider_retry.reason({'account': 'opencode', 'provider': 'opencode', 'model': 'qwen4',
                                  'launch_model': 'opencode-go/qwen4'}, provider_retry.active(now=NOW)) is None


def test_a_go_hold_leaves_zen_dispatchable(accounts, store):
    hold(accounts, GO[1], 'account', 'd1', message='Monthly limit reached for this model on OpenCode Go.')
    assert set(provider_retry.active(now=NOW)) == {'opencode#opencode-go'}
    assert all(held_out(accounts, m) for m in GO)
    assert not any(held_out(accounts, m) for m in ZEN)


def test_an_antigravity_third_party_hold_leaves_gemini_dispatchable_and_the_other_way(accounts, store):
    hold(accounts, 'claude-opus-5-5-high', 'account', 'd1', message=QUOTA)
    assert set(provider_retry.active(now=NOW)) == {f'antigravity#{THIRD_PARTY_POOL}'}
    for third in ('claude-opus-5-5-low', 'claude-sonnet-5-5-high'):
        assert held_out(accounts, third)
    for gemini in ('gemini-3.8-flash-high', 'gemini-4-argon-high'):
        assert held_out(accounts, gemini) is None
    # A Gemini model the windows' scope binds, discovered after the hold, stays out of the third-party pool.
    assert provider_retry.reason({'account': 'antigravity', 'provider': 'antigravity-resilient',
                                  'model': 'gemini-5-pro-high', 'launch_model': 'gemini-5-pro-high'},
                                 provider_retry.active(now=NOW)) is None

    provider_retry.clear('antigravity')
    hold(accounts, 'gemini-4-argon-high', 'account', 'd2', message=QUOTA)
    assert set(provider_retry.active(now=NOW)) == {f'antigravity#{GEMINI_POOL}'}
    assert held_out(accounts, 'gemini-3.8-flash-high')
    assert provider_retry.reason({'account': 'antigravity', 'provider': 'antigravity-resilient',
                                  'model': 'gemini-5-pro-high', 'launch_model': 'gemini-5-pro-high'},
                                 provider_retry.active(now=NOW))
    assert held_out(accounts, 'claude-sonnet-5-5-high') is None


def test_single_pool_accounts_are_held_whole_as_before(accounts, store):
    # Codex runs on two providers (codex, and pi under its own `openai-codex/` prefix), one prefix each: one pool.
    for launch in ('gpt-6.1-sol', 'openai-codex/gpt-6-astra'):
        assert provider_retry.pool_of(bundle(accounts, launch), accounts) is None
    # Claude's Fable window binds only Fable, but every model draws the shared weekly window: one pool.
    assert provider_retry.pool_of(bundle(accounts, 'claude-sonnet-5-5'), accounts) is None
    entry = hold(accounts, 'gpt-6.1-sol', 'account', 'd1', message=QUOTA)
    assert ('pool' not in entry, entry['model']) == (True, None)
    assert set(provider_retry.active(now=NOW)) == {'codex'}
    assert held_out(accounts, 'openai-codex/gpt-6-astra')


def test_unknown_widens_to_the_pool_and_never_across_pools(accounts, store):
    hold(accounts, ZEN[0], 'unknown', 'd1')
    hold(accounts, GO[0], 'unknown', 'd2')   # another pool: says nothing about Zen
    assert set(provider_retry.active(now=NOW)) == {f'opencode/{ZEN[0]}', f'opencode/{GO[0]}'}
    entry = hold(accounts, ZEN[1], 'unknown', 'd3')
    assert (entry['scope_source'], entry['pool']['id'], entry['models']) == ('widened', 'opencode', sorted(ZEN))
    assert entry['retry_at']   # a widened guess backs off, it is never indefinite
    assert set(provider_retry.active(now=NOW)) == {'opencode#opencode', f'opencode/{ZEN[0]}', f'opencode/{GO[0]}'}
    assert held_out(accounts, GO[1]) is None
    # Model holds remember their pool.
    doc = json.loads(store.read_text(encoding='utf-8'))['entries']
    assert (doc[f'opencode/{ZEN[0]}']['pool_id'], doc[f'opencode/{GO[0]}']['pool_id']) == ('opencode', 'opencode-go')


def test_clearing_a_pool_clears_its_models_and_keeps_the_other_pool(accounts, store):
    hold(accounts, 'claude-opus-5-5-high', 'account', 'd1', message=QUOTA)
    hold(accounts, 'claude-sonnet-5-5-low', 'model', 'd2', message=QUOTA)
    hold(accounts, 'gemini-4-argon-high', 'account', 'd3', message=QUOTA)
    hold(accounts, 'gemini-3.8-flash-high', 'transient', 'd4', message='503')
    assert provider_retry.clear_key(f'antigravity#{THIRD_PARTY_POOL}') is True
    assert set(provider_retry.active(now=NOW)) == {f'antigravity#{GEMINI_POOL}',
                                                    'antigravity/gemini-3.8-flash-high'}
    assert provider_retry.clear_key(f'antigravity#{THIRD_PARTY_POOL}') is False
    assert provider_retry.clear('antigravity', pool=GEMINI_POOL) is True
    assert provider_retry.active(now=NOW) == {}


def test_a_completed_probe_lifts_its_own_pool_only(accounts, store):
    hold(accounts, ZEN[0], 'account', 'd1')
    hold(accounts, GO[0], 'account', 'd2')
    probe = bundle(accounts, ZEN[1])
    assert provider_retry.clear('opencode', ZEN[1], include_account=True, bundle=probe,
                                launched_at=(NOW + timedelta(minutes=1)).isoformat()) is True
    assert set(provider_retry.active(now=NOW)) == {'opencode#opencode-go'}


def test_a_good_reading_or_a_re_login_lifts_each_pool_hold_on_its_own_evidence(accounts, store, monkeypatch):
    monkeypatch.setattr(provider_retry.account_state, '_latch_context', lambda account: ('cred-a', None))
    hold(accounts, 'claude-opus-5-5-high', 'account', 'd1', message=QUOTA)
    hold(accounts, 'claude-sonnet-5-5-low', 'model', 'd2', message=QUOTA)
    hold(accounts, 'gemini-4-argon-high', 'account', 'd3', at=NOW + timedelta(hours=1), message=QUOTA)
    hold(accounts, 'gemini-3.8-flash-high', 'model', 'd4', at=NOW + timedelta(hours=1), message='overloaded')
    # A good reading between the two holds lifts the older pool (and its model holds), not the newer one.
    good = (NOW + timedelta(minutes=30)).isoformat()
    assert provider_retry.lift_after_reading('antigravity', last_good=good, credential_key='cred-a') is True
    assert set(provider_retry.active(now=NOW)) == {f'antigravity#{GEMINI_POOL}',
                                                    'antigravity/gemini-3.8-flash-high'}
    assert provider_retry.lift_after_reading('antigravity', last_good=good, credential_key='cred-a') is False
    # Another credential lifts what was taken under the old one.
    assert provider_retry.lift_after_reading('antigravity', credential_key='cred-b') is True
    assert provider_retry.active(now=NOW) == {}


def test_windows_scoped_only_by_the_usage_reading_split_pools_too():
    """Usage readers pass a provider-declared `models` list through (usage_core.window_models); with no configured
    scope, the reading alone splits the account."""
    fleet = {'models': {}, 'aliases': {}, 'families': {'rows': []}, 'accounts': {'acme': {'windows': {'alpha-day': {}, 'beta-day': {}},
                                                                 'bundles': {'a': {'model': 'alpha-1'},
                                                                             'b': {'model': 'beta-1'}}}}}
    observed = {'alpha-day': {'id': 'alpha-day', 'models': ['alpha']}, 'beta-day': {'id': 'beta-day', 'models': ['beta']}}
    pool = provider_retry.pool_of({'account': 'acme', 'model': 'alpha-1'}, fleet, observed)
    assert (pool['id'], pool['models']) == ('alpha-day', ['alpha-1'])
    assert provider_retry.in_pool({'account': 'acme', 'model': 'alpha-2'}, pool)
    assert not provider_retry.in_pool({'account': 'acme', 'model': 'beta-2'}, pool)
    assert provider_retry.pool_of({'account': 'acme', 'model': 'alpha-1'}, fleet) is None   # no scope: one pool


def test_an_account_hold_from_a_single_pool_account_narrows_to_its_pool_once_go_appears(store):
    # Review 049 item 3: today's `opencode` hold (Zen only, one pool) must not keep blocking Go connected after it.
    fleet = account_fleet.load_accounts(account_fleet.reference_fleet_path())
    entry = hold(fleet, 'opencode/deepseek-v4.1-flash-free', 'account', 'd1')
    assert ('pool' not in entry, set(provider_retry.active(now=NOW))) == (True, {'opencode'})
    hold(fleet, 'claude-opus-5-5-high', 'model', 'd2', message=QUOTA)
    # Nothing to narrow while the account still has one pool.
    assert provider_retry.scope_account_holds(fleet) == []
    catalog = {'providers': {'opencode': {'ok': True, 'type': 'paseo_provider', 'checked_at': 1,
                                          'models': [{'id': m} for m in (*ZEN, *GO)]}}}
    with_go = auto_bundles.merge_into(fleet, auto_bundles.derive(fleet, catalog, 2))
    assert provider_retry.scope_account_holds(with_go) == ['opencode#opencode']
    held = provider_retry.active(now=NOW + timedelta(days=30))
    assert set(held) == {'opencode#opencode'}   # still indefinite; the model hold elsewhere has expired
    scoped = held['opencode#opencode']
    assert (scoped['scope'], scoped['retry_at'], scoped['source'], scoped['scoped_from']) == ('account', None, 'd1',
                                                                                              'opencode')
    assert all(held_out(with_go, m) for m in ZEN)
    assert not any(held_out(with_go, m) for m in GO)
    assert provider_retry.scope_account_holds(with_go) == []   # once
    assert provider_retry.clear_key('opencode#opencode') is True


def test_loading_the_accounts_narrows_a_legacy_account_hold(store, monkeypatch, tmp_path):
    # A hold written before pools were kept (no `pool`) on Antigravity, which has two pools: loading narrows it.
    monkeypatch.setenv('ROUTER_USAGE_CACHE', str(tmp_path / 'usage.json'))
    store.write_text(json.dumps({'dispatches': {}, 'entries': {'antigravity': {
        'account': 'antigravity', 'provider': 'antigravity-resilient', 'model': None, 'scope': 'account',
        'failure_code': 'provider_turn_failed', 'message': QUOTA, 'observed_at': NOW.isoformat(), 'retry_at': None,
        'models': ['claude-opus-5-5-high'], 'source': 'd-old'}}}), encoding='utf-8')
    accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
    assert set(provider_retry.active(now=NOW)) == {f'antigravity#{THIRD_PARTY_POOL}'}
    assert held_out(accounts, 'claude-sonnet-5-5-high')
    assert held_out(accounts, 'gemini-4-argon-high') is None


def test_unreadable_accounts_hold_the_whole_account(store):
    entry = provider_retry.record({'account': 'opencode', 'provider': 'opencode', 'model': 'x',
                                   'launch_model': 'opencode/x'},
                                  {'failure_code': 'provider_turn_failed', 'provider_message': MESSAGE,
                                   'at': NOW.isoformat()},
                                  dispatch_id='d1', accounts={}, classify=lambda *args: ('account', {}))
    assert 'pool' not in entry and set(provider_retry.active(now=NOW)) == {'opencode'}
