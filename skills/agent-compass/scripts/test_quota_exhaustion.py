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
    usage = {'available': True, 'fetched_at': '2026-10-30T12:00:00Z', 'windows': [{'used_pct': 30}]}
    assert q.blocked('account', usage, path=path)
    usage['fetched_at'] = datetime.now(timezone.utc).isoformat()
    usage['stale'] = True
    assert q.blocked('account', usage, path=path)
    usage['stale'] = False
    usage['windows'][0]['used_pct'] = 100
    assert q.blocked('account', usage, path=path)
    usage['windows'][0]['used_pct'] = 10
    assert not q.blocked('account', usage, path=path)
    assert not q.blocked('account', path=path)


def test_manual_clear_does_not_replay_an_old_failure(tmp_path):
    path = tmp_path / 'quota.json'
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    q.record('account', 'quota exhausted', at=old, path=path)
    q.clear('account', path=path)
    assert not q.record('account', 'quota exhausted', at=old, path=path)
    assert not q.blocked('account', path=path)


def test_zero_remaining_blocks_even_without_a_configured_floor_or_reset():
    import allocator
    now = datetime.now(timezone.utc)
    row = {'status': 'fresh', 'fetched_at': now.isoformat()}
    assert allocator.below_launch_floor({}, {'remaining_pct_before_reset': 0}, row, now)
    assert not allocator.below_launch_floor({}, {'remaining_pct_before_reset': 20}, row, now)
    assert not allocator.below_launch_floor({}, {}, row, now)
