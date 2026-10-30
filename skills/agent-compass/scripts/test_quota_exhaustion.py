from datetime import datetime, timedelta, timezone
import quota_exhaustion as q


def test_only_confirmed_quota_errors_latch_and_persist(tmp_path):
    path = tmp_path / 'quota.json'
    assert not q.record('account', 'HTTP 429: temporary rate limit', path=path)
    assert not q.record('account', 'Network timeout', path=path)
    assert q.record('account', 'Individual quota reached', at='2026-10-30T13:00:00Z', path=path)
    assert q.blocked('account', path=path)
    assert 'Individual quota' not in path.read_text()
    assert not q.blocked('another', path=path)


def test_old_or_failed_quota_cannot_release_but_fresh_positive_can(tmp_path):
    path = tmp_path / 'quota.json'
    q.record('account', 'quota_exceeded', at='2026-10-30T13:00:00Z', path=path)
    usage = {'available': True, 'fetched_at': '2026-10-30T12:00:00Z', 'windows': [{'id': 'usage', 'used_pct': 30}]}
    assert q.blocked('account', usage, path=path)
    usage['fetched_at'] = datetime.now(timezone.utc).isoformat()
    usage['stale'] = True
    assert q.blocked('account', usage, path=path)
    usage['stale'] = False
    usage['windows'][0]['used_pct'] = 100
    assert q.blocked('account', usage, path=path, windows=['usage'])
    usage['windows'][0]['used_pct'] = 10
    assert not q.blocked('account', usage, path=path, windows=['usage'])
    assert not q.blocked('account', path=path, windows=['usage'])


def test_manual_clear_does_not_replay_an_old_failure(tmp_path):
    path = tmp_path / 'quota.json'
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    q.record('account', 'quota exhausted', at=old, path=path)
    q.clear('account', path=path)
    assert not q.record('account', 'quota exhausted', at=old, path=path)
    assert not q.blocked('account', path=path)


def test_only_the_exhausted_window_latches_and_a_fresh_reading_releases_it(tmp_path):
    path = tmp_path / 'quota.json'
    assert q.record('agy', 'Individual quota reached', windows=['3p-5h', '3p-weekly'],
                    at='2026-10-30T13:00:00Z', path=path)
    gemini, claude = ['gemini-5h', 'gemini-weekly'], ['3p-5h', '3p-weekly']
    assert not q.blocked('agy', path=path) and not q.blocked('agy', path=path, windows=gemini)
    assert q.blocked('agy', path=path, windows=claude)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    reading = {'available': True, 'fetched_at': datetime.now(timezone.utc).isoformat(), 'windows': [
        {'id': 'gemini-weekly', 'used_pct': 95}, {'id': '3p-weekly', 'used_pct': 100, 'resets_at': future},
        {'id': '3p-5h', 'used_pct': 33}]}
    # The fresh reading narrows the latch to the window it shows exhausted.
    assert q.exhausted_windows('agy', reading, path=path) == {'3p-weekly'}
    assert q.blocked('agy', reading, path=path, windows=['3p-weekly'])
    assert not q.blocked('agy', reading, path=path, windows=['3p-5h'])
    reading['windows'][1]['used_pct'] = 10
    reading['fetched_at'] = datetime.now(timezone.utc).isoformat()
    assert q.exhausted_windows('agy', reading, path=path) == set()
    assert not q.blocked('agy', path=path, windows=claude)


def test_account_wide_latch_narrows_to_exhausted_windows_and_reset_releases(tmp_path):
    path = tmp_path / 'quota.json'
    q.record('agy', 'quota exhausted', at='2026-10-30T13:00:00Z', path=path)
    assert q.blocked('agy', path=path, windows=['gemini-5h'])
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    reading = {'available': True, 'fetched_at': datetime.now(timezone.utc).isoformat(), 'windows': [
        {'id': 'gemini-5h', 'used_pct': 1}, {'id': '3p-weekly', 'used_pct': 100}, {'id': '3p-5h', 'used_pct': 100,
                                                                                 'resets_at': past}]}
    assert q.exhausted_windows('agy', reading, path=path) == {'3p-weekly'}
    assert not q.blocked('agy', path=path, windows=['gemini-5h', '3p-5h'])


def test_zero_remaining_blocks_even_without_a_configured_floor_or_reset():
    import allocator
    now = datetime.now(timezone.utc)
    row = {'status': 'fresh', 'fetched_at': now.isoformat()}
    assert allocator.below_launch_floor({}, {'remaining_pct_before_reset': 0}, row, now)
    assert not allocator.below_launch_floor({}, {'remaining_pct_before_reset': 20}, row, now)
    assert not allocator.below_launch_floor({}, {}, row, now)


def test_routing_excludes_only_bundles_drawing_a_latched_window():
    import allocator
    accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
    accounts['accounts']['antigravity']['verified'] = True
    accounts['_discovery'] = {'accounts': {'antigravity': {
        'family': 'antigravity', 'status': 'ready', 'exhausted_windows': ['3p-weekly']}}}
    decision = allocator.plan({'objective': 'Reply with exactly OK', 'hours': 0.1}, {}, accounts=accounts,
                              overrides=[], host_policy_config=None)
    rows = {row['id']: row for row in decision['ranking']}
    latched = '3p-weekly: quota confirmed exhausted; refresh quota or replan'
    assert latched in rows['agy_opus55_high']['excluded']
    assert latched not in rows['argon_high']['excluded']
    assert latched not in rows['flash_high']['excluded']
