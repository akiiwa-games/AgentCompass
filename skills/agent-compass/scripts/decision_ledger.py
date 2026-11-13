"""The router ledger: where it lives, reading and appending decision records, and the capacity alerts written with them."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

import dispatch_registry
import env_vars
import scorer_client
import subscription_judge
import usage_reading
from worker_context import own_session_ids


LEDGER_ENV = 'ROUTER_LEDGER'
ALERT_REMAINING_PCT = 20      # warn when the account carrying the work is at or below this
IDLE_REMAINING_PCT = 80       # informational: another account that is mostly unused
BURN_LOOKBACK_SECONDS = 24 * 3600
BURN_MIN_SPAN_SECONDS = 1800


def ledger_path():
    configured = env_vars.user_env(LEDGER_ENV)
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return Path.home() / '.agent-compass' / 'router-ledger.jsonl'


LEDGER_WRITES = True    # `plan --dry-run` turns this off so a trial run leaves no decision on record


def append_ledger(entry):
    """Append one decision record. Auditing must never break routing, so failures are swallowed."""
    if not LEDGER_WRITES:
        return False
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
        return True
    except (OSError, ValueError, TypeError):
        return False


LEDGER_TAIL_BYTES = 8 * 1024 * 1024    # plan lines carry a decision snapshot; only the newest part of a big file is read


def read_ledger(path=None, limit=500):
    path = path or ledger_path()
    try:
        size = path.stat().st_size
        if size > LEDGER_TAIL_BYTES:
            with path.open('rb') as stream:
                stream.seek(size - LEDGER_TAIL_BYTES)
                text = stream.read().decode('utf-8', errors='replace')
            text = text.partition('\n')[2]       # the first line is cut mid-record
        else:
            text = path.read_text(encoding='utf-8-sig')
        lines = text.splitlines()[-limit:]
    except (OSError, UnicodeError):
        return []
    records = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            records.append(row)
    return records


def usage_digest(usage):
    """Per-account observation times and window usage, enough to compute a burn rate later. A pooled window (a Magpie
    pool reports `accounts: N`) keeps its account count, so a replay prices it as N accounts' worth."""
    digest = {}
    for name, row in (usage.get('accounts') or {}).items():
        windows = []
        for w in row.get('windows') or []:
            item = {'id': w.get('id'), 'used_pct': w.get('used_pct'), 'resets_at': w.get('resets_at')}
            if isinstance(w.get('accounts'), int) and not isinstance(w.get('accounts'), bool) and w['accounts'] > 1:
                item['accounts'] = w['accounts']
            windows.append(item)
        digest[name] = {'status': row.get('status'), 'fetched_at': row.get('fetched_at'), 'windows': windows}
    return digest


def burn_projection(account, window, records, now, current_fetched=None):
    """Observed %/hour of one window across ledger snapshots that share its reset time."""
    points = {}
    if current_fetched is not None and usage_reading.finite_number(window.get('used_pct')):
        points[current_fetched] = window['used_pct']
    for record in records:
        row = (record.get('usage') or {}).get(account) or {}
        fetched = usage_reading.timestamp(row.get('fetched_at'))
        if fetched is None or not 0 <= (now - fetched).total_seconds() <= BURN_LOOKBACK_SECONDS:
            continue
        for item in row.get('windows') or []:
            if (isinstance(item, dict) and item.get('id') == window.get('id')
                    and item.get('resets_at') == window.get('resets_at')
                    and usage_reading.finite_number(item.get('used_pct'))):
                points[fetched] = item['used_pct']
    if len(points) < 2:
        return None
    first, last = min(points), max(points)
    span = (last - first).total_seconds()
    rise = points[last] - points[first]
    if span < BURN_MIN_SPAN_SECONDS or rise <= 0:
        return None
    rate = rise / (span / 3600)
    return {'pct_per_hour': round(rate, 2), 'span_hours': round(span / 3600, 2),
            'hours_to_exhaustion': round((100 - points[last]) / rate, 2)}


def capacity_alerts(usage, carrying, records, now, model='', account_specs=None):
    """Deterministic warnings for the account(s) that will carry work. They never block dispatch."""
    alerts = []
    accounts = usage.get('accounts') or {}
    account_specs = account_specs if isinstance(account_specs, dict) else {}
    for name in carrying:
        row = accounts.get(name)
        if not isinstance(row, dict) or row.get('reason') == 'account_missing':
            alerts.append({'code': 'telemetry_missing', 'severity': 'info', 'account': name,
                           'message': f'{name}: no usage observation; unknown is not exhausted.'})
            continue
        if row.get('flag_conflict'):
            alerts.append({'code': 'stale_flag_conflict', 'severity': 'warn', 'account': name,
                           'message': f'{name}: collector reports stale=false but the observation is '
                                      f'{row["observation_age_seconds"] / 86400:.1f} days old.'})
        window = usage_reading.tightest_window(row, model, account_specs.get(name))
        if window is None:
            if not row.get('flag_conflict'):
                alerts.append({'code': 'telemetry_not_current', 'severity': 'info', 'account': name,
                               'message': f'{name}: no current window observation.'})
            continue
        remaining = window['remaining_pct_before_reset']
        days = window['seconds_until_reset'] / 86400
        if remaining <= ALERT_REMAINING_PCT:
            alerts.append({'code': 'low_remaining', 'severity': 'warn', 'account': name,
                           'window': window['id'],
                           'message': f'{name}/{window["id"]}: {remaining:.0f}% left, resets in {days:.1f} days.'})
        burn = burn_projection(name, window, records, now, usage_reading.timestamp(row.get('fetched_at')))
        if burn and burn['hours_to_exhaustion'] * 3600 < window['seconds_until_reset']:
            alerts.append({'code': 'exhaustion_before_reset', 'severity': 'warn', 'account': name,
                           'window': window['id'], 'burn': burn,
                           'message': f'{name}/{window["id"]}: burning {burn["pct_per_hour"]}%/h over '
                                      f'{burn["span_hours"]}h; empty in ~{burn["hours_to_exhaustion"]}h but '
                                      f'resets in {days:.1f} days.'})
    if any(a['code'] in ('low_remaining', 'exhaustion_before_reset') for a in alerts):
        metered = usage_reading.metered_names()
        for name, row in accounts.items():
            is_metered = (
                name.lower() in metered
                or row.get('billing') == 'metered'
                or any(w.get('id') == 'spend' for w in row.get('windows') or [])
            )
            window = (usage_reading.tightest_window(row) if name not in carrying and not is_metered
                      and row.get('status') == 'fresh' else None)
            if window and window['remaining_pct_before_reset'] >= IDLE_REMAINING_PCT:
                shown = ', '.join(f'{w["id"]} {w["remaining_pct_before_reset"]:.0f}% ({w["seconds_until_reset"] / 86400:.1f}d)'
                                  for w in usage_reading.applicable_windows(row))
                alerts.append({'code': 'idle_alternative', 'severity': 'info', 'account': name,
                               'message': f'{name}: {shown} left. Informational: percentages '
                                          'are not token-equivalent across providers; ask before relaxing a pin.'})
    return alerts


def account_cadence_alert(records, now, account, spec):
    """Optional account-configured dispatch cadence; no implied quota knowledge."""
    cadence = spec.get('cadence') or {}
    seconds, limit = cadence.get('window_seconds'), cadence.get('limit')
    if not usage_reading.finite_number(seconds, 1, 365 * 86400) or not usage_reading.finite_number(limit, 1, 1000000):
        return None
    recent = [r for r in records if (r.get('bundle') or {}).get('account') == account
              and r.get('stage') in ('select', 'record') and r.get('choice') not in (None, 'none')
              and (t := usage_reading.timestamp(r.get('at'))) and 0 <= (now - t).total_seconds() <= seconds]
    if len(recent) < limit:
        return None
    return {'code': 'advisor_cadence', 'severity': 'info', 'account': account,
            'message': f'{len(recent)} dispatches in the configured {seconds / 3600:g}h cadence window; '
                       'dispatch frequency does not measure remaining quota.'}


def decision_snapshot(result, task=None):
    """The ranking and component scores this decision was made with, for the ledger. Replays read it as written."""
    if not isinstance(result.get('ranking'), list):
        return None
    import ledger_snapshot
    try:
        return ledger_snapshot.decision_snapshot(result, task=task)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def finalize(result, payload, requested, started, task=None, records=None):
    """Attach capacity alerts and write the ledger record. Returns the result unchanged otherwise."""
    now = datetime.now(timezone.utc)
    records = read_ledger() if records is None else records
    usage = result.get('usage') or {}
    alerts = list(result.get('alerts') or [])
    bundle = result.get('selected_bundle') or {}
    if bundle.get('account'):
        import account_fleet
        try:
            spec = account_fleet.load_accounts().get('accounts', {}).get(bundle['account']) or {}
        except (OSError, ValueError):
            spec = {}
        alerts += capacity_alerts(usage, [bundle['account']], records, now, bundle.get('model', ''),
                                  {bundle['account']: spec})
        cadence = account_cadence_alert(records, now, bundle['account'], spec)
        alerts += [cadence] if cadence else []
    judge = result.get('judge')
    if judge and requested != 'auto':
        row = (usage.get('accounts') or {}).get(judge['account'], {})
        window = usage_reading.tightest_window(row, judge['model'], subscription_judge.judge_account_spec(judge['account']))
        if window and window['remaining_pct_before_reset'] <= subscription_judge.LOW_REMAINING_PCT:
            alerts.append({'code': 'pinned_judge_account_low', 'severity': 'warn', 'account': judge['account'],
                           'message': f'--judge {requested} is pinned but {judge["account"]} has '
                                      f'{window["remaining_pct_before_reset"]:.0f}% left in {window["id"]}.'})
    decision = result.get('decision') or {}
    if decision.get('needs_user_decision'):
        alerts.append({'code': 'needs_user_decision', 'severity': 'warn', 'account': bundle.get('account'),
                       'message': (decision.get('reason') or 'The allocator cannot choose without a user decision.')[:300]})
    if decision.get('needs_judgment'):
        alerts.append({'code': 'needs_judgment', 'severity': 'info', 'account': bundle.get('account'),
                       'message': f'Top two bundles are within {decision.get("margin")}; run the select stage to break the tie.'})
    result['alerts'] = alerts
    task = task if isinstance(task, dict) else {}
    allocation_task = result.pop('_allocation_task', task)
    needs_row = allocation_task.get('needs') if isinstance(allocation_task.get('needs'), dict) else {}
    unavailable_row = result.pop('_unavailable', None)
    objective = task.get('objective')
    chain = task.get('chain') if isinstance(task.get('chain'), dict) else {}
    hours_val = (decision.get('task') or {}).get('hours') or task.get('expected_hours')
    diff_val = (decision.get('task') or {}).get('difficulty') or task.get('difficulty')
    expected_usd = result.get('expected_usd') if isinstance(result.get('expected_usd'), (int, float)) else bundle.get('expected_usd')
    if expected_usd is not None and isinstance(bundle, dict) and bundle:
        bundle.setdefault('expected_usd', expected_usd)
        result.setdefault('expected_usd', expected_usd)
    if LEDGER_WRITES and result.get('stage') in ('plan', 'select', 'record'):
        result.update(dispatch_registry.issue(result, task, now, allocation_task=allocation_task))
    append_ledger({
        'v': 1, 'at': now.isoformat(), 'stage': result.get('stage'), 'source': result.get('source'),
        'choice': result.get('choice'), 'specialty': result.get('specialty'),
        **({'task_id': task['task_id']} if task.get('task_id') else {}),
        **({'request_hash': task.get('request_hash') or task_request_hash(task)} if isinstance(task, dict) else {}),
        **{k: result[k] for k in ('decision_id', 'task_id', 'parent_id', 'task_hash', 'bound') if k in result},
        **({'host_session_id': task['host_session_id']} if task.get('host_session_id') else {}),
        # Who wrote the row (parent_id is only the parent link): the spawn gate binds Workflow and schedule
        # authorization to these, so a plan or record made in one session never authorizes another.
        **({'session_ids': ids} if (ids := own_session_ids()) else {}),
        'constraint': result.get('constraint'), 'basis': (result.get('basis') or '')[:400] or None,
        **({'trigger': result['trigger']} if result.get('trigger') else {}),
        **({'reason_category': result['reason_category']} if result.get('reason_category') else {}),
        **({'host_policy_violation': result['host_policy_violation']} if 'host_policy_violation' in result else {}),
        'judge': (judge or {}).get('model'), 'judge_pinned': requested != 'auto' if judge else None,
        'bundle': {k: bundle.get(k) for k in ('provider', 'account', 'model', 'effort', 'bundle_id', 'expected_usd', 'fast')
                   if bundle.get(k) is not None} if bundle else None,
        'chain': chain.get('id'), 'action': decision.get('action'), 'margin': decision.get('margin'),
        'hours': hours_val,
        'expected_usd': expected_usd,
        'kind': (decision.get('task') or {}).get('kind') or task.get('work_kind'),
        'difficulty': diff_val,
        **({'difficulty_source': (decision.get('task') or {})['difficulty_source']}
           if (decision.get('task') or {}).get('difficulty_source') else {}),
        **({'scorer': result['scorer'].get('source'),
            **({'scorer_type': result['scorer']['scorer_type']} if result['scorer'].get('scorer_type') else {}),
            **({'scorer_ms': result['scorer']['duration_ms']} if result['scorer'].get('duration_ms') is not None else {})}
           if isinstance(result.get('scorer'), dict) else {}),
        **({'profile': {k: v for k, v in decision['task']['profile'].items() if k != 'reason'}}
           if isinstance((decision.get('task') or {}).get('profile'), dict) else {}),
        'local_only': bool(needs_row.get('local_only')) or None,
        **({'local_only_reason': str(needs_row.get('local_only_reason'))[:200]} if needs_row.get('local_only_reason') else {}),
        **({'unavailable': list(unavailable_row)} if unavailable_row else {}),
        'needs_user_decision': decision.get('needs_user_decision') or None,
        'router_choice': result.get('router_choice'), 'agrees': result.get('agrees'),
        **({'classifier_skipped': result['classifier_skipped']} if 'classifier_skipped' in result else {}),
        **({'keep_whole': kept['reason'], 'task_count': kept.get('task_count')}
           if (kept := next((a for a in alerts if a.get('code') == 'kept_whole'), None)) else {}),
        'objective': objective[:200] if isinstance(objective, str) else None,
        'reason': (result.get('reason') or decision.get('reason') or result.get('error') or '')[:600] or None,
        'payload_bytes': len(json.dumps(payload, ensure_ascii=False).encode()) if payload else None,
        'duration_ms': int((time.monotonic() - started) * 1000),
        'usage': usage_digest(usage), 'alerts': [a['code'] for a in alerts],
        **({'decision_snapshot': snapshot} if (snapshot := decision_snapshot(result, allocation_task)) else {})})
    return result


def task_request_hash(task):
    if not isinstance(task, dict):
        return ''
    existing = task.get('request_hash') or task.get('task_hash')
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    payload = {k: v for k, v in scorer_client.scorer_task_fields(task).items() if k in ('objective', 'context', 'environment')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:24] if payload else ''
