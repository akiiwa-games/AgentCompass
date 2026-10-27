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

DURATIONS = (3, 6, 24)
BOOST = 0.35
INTENT = 'tray:anticipated-reset'
TOP_MODELS = (('codex', 'gpt-6-astra', 'Astra'),
              ('antigravity', 'gemini-4-argon-high', 'Argon'),
              ('claude', 'claude-opus-5-5', 'Opus 5.5'))
DEFAULT_HOURS = 24
RESET_BADGE_HOURS = 12
LABELS = {'codex': 'Codex', 'claude': 'Claude', 'claude-2': 'Claude 2', 'antigravity': 'Antigravity'}


def modules():
    scripts = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'skills/route-agent-work/scripts'
    if not (scripts/'route_judge.py').is_file():
        raise ValueError(_tr('未找到 route-agent-work，请先安装 router 技能'))
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


def model_label(model):
    return next((label for _, mid, label in TOP_MODELS if mid == model), model)


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
    until = allocator.parse_time(row.get('until'))
    return until is not None and until > now


def overlaps(row, account, model, now, allocator, accounts):
    # An account-wide or same-model priority already applies. Do not double its
    # boost by opening another window or by adopting an existing CLI-created rule.
    return (active(row, now, allocator) and not allocator.override_reset_landed(row, None, accounts, now)
            and row.get('account') == account
            and (not row.get('model') or allocator.canonical(row['model'], accounts) == model))


def targets(allocator, accounts):
    policy = allocator.host_policy.load_policy()
    result = []
    bundles = allocator.bundles(accounts).values()
    for account, model, label in TOP_MODELS:
        if not allocator.host_policy.account_allowed(policy, account) or not any(
                b['account'] == account and b['model'] == model and not b.get('disabled') for b in bundles):
            continue
        windows = [(wid, spec) for wid, spec, _ in allocator.applicable_windows(
            accounts['accounts'][account], model, {}) if spec.get('hours', 0) >= 24]
        if not windows:
            continue
        wid, _ = max(windows, key=lambda w: w[1]['hours'])
        result.append({'account': account, 'model': model, 'window': wid, 'label': label})
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
        raise ValueError(_tr('请选择菜单中的最长等待时间'))
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
        if any(overlaps(r, account, model, now, allocator, accounts) for r in doc['overrides']):
            return _tr('该账号/模型已有临时规则；未叠加权重，也未延长截止时间')
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
        # The compact chooser is single selection, not three cumulative boosts.
        # Replace reset-only priorities for these three models, not unrelated expiry constraints.
        selected_models = {(a, m) for a, m, _ in TOP_MODELS}
        doc['overrides'] = [r for r in doc['overrides'] if not (
            r.get('stop_on_reset') and not r.get('expires_at')
            and (r.get('account'), r.get('model')) in selected_models)]
        doc.pop('last_reset', None)
        doc['overrides'].append(row)
        save(allocator, path, {**doc, 'v': 1})
    return _tr('已开启 {0}/{1} 临时优先；重置确认后撤销，最迟 {2} 失效').format(f'{LABELS.get(account, account)}', f'{model_label(model)}', f"{stamp(row['until'], allocator)}")


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
    entries = [{'label': model_label(r.get('model') or LABELS.get(r.get('account'), _tr('其他'))),
                'id': r['id'], 'revision': revision(r)} for r in rows]
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
            return enable(action['account'], action['model'], action['window'], DEFAULT_HOURS)
        if action['kind'] == 'cancel':
            return cancel(action['id'], action['revision'])
        raise ValueError(_tr('未知临时优先操作'))
    except (ValueError, OSError) as exc:
        return _tr('未完成：{0}').format(f'{exc}')
