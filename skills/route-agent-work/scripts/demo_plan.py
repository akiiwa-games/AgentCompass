"""Offline reference-fleet simulation. Never issues a dispatch receipt or reads host state."""

from datetime import datetime, timedelta, timezone


def plan(document, allocator):
    task = document.get('task')
    if not isinstance(task, dict) or not task:
        raise ValueError('Input requires task.')
    # Continuations refer to live sessions; a demo always starts a fresh simulated task.
    task = {k: v for k, v in task.items() if k not in ('chain', 'worker')}
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    accounts = allocator.load_accounts(allocator.REFERENCES / 'accounts.json')
    # Illustrative observations, not a claim about current subscriptions or model availability.
    snapshot = {'claude': (55, 112), 'codex': (83, 144), 'antigravity': (63, 89), 'dot': (100, 168)}
    accounts['accounts'] = {k: v for k, v in accounts['accounts'].items() if k in snapshot}
    usage = {'status': 'demo_reference', 'read_ok': True, 'read_at': now.isoformat(), 'accounts': {}}
    for name, spec in accounts['accounts'].items():
        remaining, reset_hours = snapshot[name]
        windows = []
        for wid, window in spec.get('windows', {}).items():
            short = window.get('hours', 168) < 24
            windows.append({'id': wid, 'used_pct': 10 if short else 100 - remaining,
                            'remaining_pct_before_reset': 90 if short else remaining,
                            'seconds_until_reset': (4 if short else reset_hours) * 3600,
                            'resets_at': (now + timedelta(hours=4 if short else reset_hours)).isoformat()})
        usage['accounts'][name] = {'status': 'fresh', 'available': True, 'stale': False,
                                   'fetched_at': now.isoformat(), 'windows': windows}
    result = allocator.plan(task, usage, now, records=[], accounts=accounts, overrides=[],
                            host_policy_config={'allowed_accounts': list(snapshot)})
    result['demo'] = True
    result['notice'] = ('DEMO: synthetic quota snapshot and reference fleet aliases. No credentials, live quotas, '
                        'network calls, ledger writes or worker launches. Rankings are illustrative.')
    return result


def format_table(result):
    lines = [result.get('notice', 'Candidate ranking'), '', f"{'Rank':<5} {'Bundle':<24} {'Score':>8}  Status"]
    rank = 0
    for candidate in result.get('ranking', []):
        excluded = candidate.get('excluded') or []
        if not excluded:
            rank += 1
        score = '-' if candidate.get('score') is None else f"{candidate['score']:.4f}"
        status = '; '.join(excluded) if excluded else ('winner' if candidate['id'] == result.get('choice') else 'eligible')
        lines.append(f"{str(rank) if not excluded else '-':<5} {candidate['id']:<24} {score:>8}  {status}")
    lines.extend(['', result.get('decision', {}).get('reason', '')])
    return '\n'.join(lines)
