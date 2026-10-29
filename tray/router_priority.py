"""Tray controls for the router's existing override store; no agents or quota probes.

Menu actions and first confirmed reset receipts use the locked, atomic store.
The allocator remains the sole interpreter of reset confirmation and retirement.
"""

from i18n import translate as _tr
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid

DURATIONS = (1, 3, 6, 12, 24)
BOOST = 0.35
INTENT = 'tray:anticipated-reset'
# Quick reset-priority targets: the fleet document's `priority_targets` ([{account, model, label?}]) when set,
# otherwise the strongest enabled model of each account with a weekly-or-longer window.
QUICK_LIMIT = 3
DEFAULT_HOURS = 24
RESET_BADGE_HOURS = 12


class RouterMissing(ValueError):
    """No route-agent-work skill for this user yet (the tray was installed before `agentcompass setup`)."""


def modules():
    scripts = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'skills/route-agent-work/scripts'
    if not (scripts/'route_judge.py').is_file():
        raise RouterMissing(_tr('未找到 route-agent-work，请先安装 router 技能'))
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module('allocator'), importlib.import_module('route_judge')


def read_document(path):
    try:
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {'v': 1, 'overrides': []}
    if not isinstance(doc, dict) or not isinstance(doc.get('overrides'), list) or any(
            not isinstance(r, dict) or not r.get('id') for r in doc['overrides']):
        raise ValueError(_tr('临时规则文件格式错误；未覆盖原文件'))
    return doc


def revision(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _display():
    try:
        import router_data
        return router_data.account_label, router_data.model_label
    except Exception:
        return (lambda name: name or ''), (lambda model: model)


def account_label(account):
    return _display()[0](account)


def model_label(model, accounts=None):
    configured = next((t.get('label') for t in (accounts or {}).get('priority_targets') or ()
                       if isinstance(t, dict) and t.get('model') == model and t.get('label')), None)
    return configured or _display()[1](model)


def window_label(wid, spec):
    hours = spec.get('hours')
    return _tr('{0}（周额度）').format(f'{wid}') if hours == 168 else _tr('{0}（{1}小时）').format(f'{wid}', f'{hours:g}') if isinstance(hours, (float, int)) else wid


def stamp(value, allocator):
    moment = allocator.parse_time(value)
    return moment.astimezone().strftime('%m-%d %H:%M') if moment else _tr('未知')


def percentage(window):
    if not isinstance(window, dict):
        return None
    value = window.get('used_pct')
    if value is None:
        remaining = window.get('remaining_pct', window.get('remaining_pct_before_reset'))
        if isinstance(remaining, (int, float)):
            value = 100-remaining
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100 else None


def active(row, now, allocator):
    until = allocator.parse_time(row.get('until')) or allocator.expected_reset_time(row)
    return until is not None and until > now


def dynamic_priority(row, allocator, accounts, usage, now):
    """Current countdown and surplus bonus before a task's consumption is deducted."""
    reset = allocator.expected_reset_time(row)
    if reset is None:
        return {'dynamic_boost': None, 'remaining_hours': None, 'countdown': ''}
    hours = max(0.0, (reset - now).total_seconds() / 3600)
    account = row.get('account')
    spec = accounts.get('accounts', {}).get(account) or {}
    observed = usage.get('accounts', {}).get(account) or {}
    policy = accounts['scoring']
    model = allocator.canonical(row['model'], accounts) if row.get('model') else None
    bonus = 0.0
    if hours > 0 and not allocator.override_reset_landed(row, usage, accounts, now):
        for wid, wspec, win in allocator.applicable_windows(spec, model, observed):
            expected = allocator.window_reset_expectation([row], account, model, wid, spec, accounts, now)
            if expected is None:
                continue
            state = allocator.window_state(wid, wspec, win, spec, now, policy['short_window_hours'],
                                           account_stale=observed.get('status') == 'stale' or observed.get('stale') is True,
                                           expected_reset_at=expected)
            if not state['known']:
                continue
            forecast = allocator.demand_forecast(spec, wid, state, policy)
            if forecast is not None:
                cost = allocator.window_cost(spec, wid, state, 0, forecast, policy, {'notes': []}, now)
                bonus = max(bonus, cost.get('reset_bonus', 0.0))
    return {'dynamic_boost': bonus, 'remaining_hours': hours,
            'countdown': _tr('预计 {0}h 后重置').format(f'{hours:.1f}')}


def overlaps(row, account, model, now, allocator, accounts):
    # An account-wide or same-model priority already applies. Do not double its
    # boost by opening another window or by adopting an existing CLI-created rule.
    return (active(row, now, allocator) and not allocator.override_reset_landed(row, None, accounts, now)
            and row.get('account') == account
            and (not row.get('model') or allocator.canonical(row['model'], accounts) == model))


def _candidates(allocator, accounts):
    """(account, model) pairs with an enabled, launchable bundle on an allowed account, in fleet order."""
    policy = allocator.host_policy.load_policy()
    pairs = []
    for bundle in allocator.bundles(accounts).values():
        account, model = bundle.get('account'), bundle.get('model')
        spec = (accounts.get('accounts') or {}).get(account) or {}
        if (not account or not model or bundle.get('disabled') or bundle.get('discovery_unavailable')
                or spec.get('verified') is False or not allocator.host_policy.account_allowed(policy, account)):
            continue
        if (account, model) not in pairs:
            pairs.append((account, model))
    return pairs


def _weekly_window(allocator, accounts, account, model):
    windows = [(wid, spec) for wid, spec, _ in allocator.applicable_windows(
        accounts['accounts'][account], model, {}) if spec.get('hours', 0) >= 24]
    return max(windows, key=lambda w: w[1]['hours'])[0] if windows else None


def targets(allocator, accounts):
    candidates = _candidates(allocator, accounts)
    configured = [t for t in accounts.get('priority_targets') or () if isinstance(t, dict)]
    if configured:
        wanted = [(t.get('account'), t.get('model')) for t in configured]
        pairs = [pair for pair in wanted if pair in candidates]
    else:
        models = accounts.get('models') or {}
        best = {}
        for account, model in candidates:
            tier = (models.get(model) or {}).get('tier') or 0
            if _weekly_window(allocator, accounts, account, model) and tier > best.get(account, (None, -1))[1]:
                best[account] = (model, tier)
        pairs = sorted(((a, m) for a, (m, _) in best.items()), key=lambda pair: -best[pair[0]][1])[:QUICK_LIMIT]
    result = []
    for account, model in pairs:
        wid = _weekly_window(allocator, accounts, account, model)
        if wid:
            result.append({'account': account, 'model': model, 'window': wid, 'label': model_label(model, accounts)})
    labels = [t['label'] for t in result]
    for t in result:
        if labels.count(t['label']) > 1:
            t['label'] = f"{t['label']} · {account_label(t['account'])}"
    return result


def baseline(allocator, accounts, usage, target, now):
    account, wid = target['account'], target['window']
    row = (usage.get('accounts') or {}).get(account) or {}
    fetched = allocator.parse_time(row.get('fetched_at'))
    if (row.get('status') != 'fresh' or row.get('stale') is True or row.get('available') is False
            or row.get('account', account) != account or fetched is None
            or not 0 <= (now-fetched).total_seconds() <= 900):
        raise ValueError(_tr('额度缺失或超过15分钟；请先在 Usage Float 刷新该账号，再开启'))
    spec = accounts['accounts'][account]
    observed = next((w for w in row.get('windows', [])
                     if allocator.canonical_window_id(spec, w.get('id')) == wid), None)
    used = percentage(observed)
    reset = allocator.parse_time((observed or {}).get('resets_at'))
    if used is None or reset is None or reset <= now:
        raise ValueError(_tr('所选窗口缺少有效额度或重置日期；请先刷新额度'))
    return {k: v for k, v in {'account': account, 'fetched_at': row['fetched_at'],
            'observation_id': row.get('observation_id'), 'used_pct': used, 'remaining_pct': 100-used,
            'resets_at': observed['resets_at']}.items() if v is not None}


def save(allocator, path, document):
    # Caller holds the same sidecar lock as route_judge and allocator.
    if path.exists():
        backup = path.parent/'backups'/f'tray-priority-{uuid.uuid4().hex}'
        backup.mkdir(parents=True)
        shutil.copy2(path, backup/path.name)
    allocator.override_store.write(path, document)


def enable(account, model, window, hours, now=None):
    if hours not in DURATIONS:
        raise ValueError(_tr('请选择菜单中的预期重置时间'))
    allocator, router = modules()
    now = now or datetime.now(timezone.utc)
    accounts = allocator.load_accounts()
    target = next((t for t in targets(allocator, accounts)
                   if (t['account'], t['model'], t['window']) == (account, model, window)), None)
    if target is None:
        raise ValueError(_tr('该账号、模型或窗口已不可用；请等待菜单刷新'))
    path = allocator.overrides_path()
    with allocator.override_store.locked(path):
        doc = read_document(path)
        new_until = (now+timedelta(hours=hours)).isoformat()
        overlapping = [r for r in doc['overrides'] if overlaps(r, account, model, now, allocator, accounts)]
        if overlapping:
            ui_rules = [r for r in overlapping if r.get('intent') in (INTENT, 'panel:temporary-boost')
                        and r.get('stop_on_reset') and not r.get('expires_at')]
            if not ui_rules or any(r.get('until') == new_until for r in ui_rules):
                return _tr('该账号/模型已有临时规则；未叠加权重，也未延长截止时间')
            existing = ui_rules[0]
            existing['until'] = new_until
            existing['basis'] = _tr('用户在托盘选择预计临时重置，优先 {0}/{1}；额度回升后 router 自动撤销').format(f'{account}', f'{model}')
            doc['overrides'] = [r for r in doc['overrides'] if r is existing or r not in ui_rules]
            doc.pop('last_reset', None)
            save(allocator, path, {**doc, 'v': 1})
            return _tr('已更新 {0}/{1} 预期重置时间；重置确认后撤销，最迟 {2} 失效').format(
                f'{account_label(account)}', f'{model_label(model, accounts)}', f"{stamp(new_until, allocator)}")
        # Use ONE real observation for the baseline. Never synthesize a timestamp
        # for a cached older value and never fetch directly from provider APIs.
        usage = router.usage_snapshot(now=now)
        base = baseline(allocator, accounts, usage, target, now)
        if allocator.override_depleted({'account': account, 'model': model, 'window': window, 'boost': BOOST},
                                       usage, accounts):
            raise ValueError(_tr('该窗口额度已不足 {0}%，router 不再派新任务，临时优先无意义').format(f"{accounts['scoring']['safety_margin_pct']}"))
        row = {'id': uuid.uuid4().hex, 'intent': INTENT, 'account': account, 'model': model,
               'window': window, 'boost': BOOST, 'created_at': now.isoformat(),
               'until': (now+timedelta(hours=hours)).isoformat(), 'stop_on_reset': True,
               'reset_confirmation': 'replenishment', 'baseline_windows': {window: base},
               'basis': _tr('用户在托盘选择预计临时重置，优先 {0}/{1}；额度回升后 router 自动撤销').format(f'{account}', f'{model}')}
        # The compact chooser is single selection, not cumulative boosts.
        # Replace reset-only priorities for the chooser's models, not unrelated expiry constraints.
        selected_models = {(t['account'], t['model']) for t in targets(allocator, accounts)}
        doc['overrides'] = [r for r in doc['overrides'] if not (
            r.get('stop_on_reset') and not r.get('expires_at')
            and (r.get('account'), r.get('model')) in selected_models)]
        doc.pop('last_reset', None)
        doc['overrides'].append(row)
        save(allocator, path, {**doc, 'v': 1})
    return _tr('已开启 {0}/{1} 临时优先；重置确认后撤销，最迟 {2} 失效').format(f'{account_label(account)}', f'{model_label(model, accounts)}', f"{stamp(row['until'], allocator)}")


def cancel(entry_id, expected_revision):
    allocator, _ = modules()
    path = allocator.overrides_path()
    with allocator.override_store.locked(path):
        doc = read_document(path)
        row = next((r for r in doc['overrides'] if r['id'] == entry_id), None)
        if row is None:
            return _tr('该临时规则已关闭，无需重复取消')
        if revision(row) != expected_revision:
            raise ValueError(_tr('规则已被其他操作更新；请等待菜单刷新后再取消'))
        doc['overrides'] = [r for r in doc['overrides'] if r['id'] != entry_id]
        save(allocator, path, doc)
    return _tr('已取消这条临时优先；不影响正在运行的任务')


def snapshot(now=None):
    allocator, router = modules()
    now = now or datetime.now(timezone.utc)
    path = allocator.overrides_path()
    doc = read_document(path)
    accounts = allocator.load_accounts()
    usage = router.usage_snapshot(now=now)
    landed = {r['id']: r for r in doc['overrides'] if active(r, now, allocator)
              and allocator.override_reset_landed(r, usage, accounts, now)}
    if landed:
        # Store the first confirmed event, not a refresh timestamp. This survives
        # a tray restart and the allocator deleting the original rule.
        allocator.observe_landed_overrides(landed, path, now)
        doc = read_document(path)
    depleted = {r['id']: r for r in doc['overrides'] if active(r, now, allocator)
                and allocator.override_depleted(r, usage, accounts)}
    if depleted:
        # Down to the safety margin: the allocator retires these on its next plan; do it now so the menu agrees.
        allocator.retire_overrides(depleted, path)
        doc = read_document(path)
    rows = [r for r in doc['overrides'] if active(r, now, allocator)
            and not allocator.override_reset_landed(r, None, accounts, now)]
    entries = [{'label': model_label(r['model'], accounts) if r.get('model') else account_label(r.get('account')) or _tr('其他'),
                'id': r['id'], 'revision': revision(r),
                **dynamic_priority(r, allocator, accounts, usage, now)} for r in rows]
    for entry in entries:
        if entry['dynamic_boost'] is not None:
            entry['label'] += _tr(' · {0} · 动态加成 +{1}').format(entry['countdown'], f"{entry['dynamic_boost']:.2f}")
    choices = targets(allocator, accounts)
    for t in choices:
        t['selected'] = any(overlaps(r, t['account'], t['model'], now, allocator, accounts) for r in rows)
    last = doc.get('last_reset') or {}
    reset_at = allocator.parse_time(last.get('at')) if isinstance(last, dict) else None
    recent = reset_at is not None and timedelta(0) <= now-reset_at < timedelta(hours=RESET_BADGE_HOURS)
    label = (_tr('优先中：') + '、'.join(dict.fromkeys(e['label'] for e in entries)) if entries
             else _tr('已重置') if recent else _tr('重置前优先'))
    return {'label': label, 'entries': entries, 'targets': choices}


def perform(action):
    """Expected local diagnostics are actionable notifications, not generic failures."""
    try:
        if action['kind'] == 'enable':
            return enable(action['account'], action['model'], action['window'], action.get('hours', DEFAULT_HOURS))
        if action['kind'] == 'cancel':
            return cancel(action['id'], action['revision'])
        raise ValueError(_tr('未知临时优先操作'))
    except (ValueError, OSError) as exc:
        return _tr('未完成：{0}').format(f'{exc}')
