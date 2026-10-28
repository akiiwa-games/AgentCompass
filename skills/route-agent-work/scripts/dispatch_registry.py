"""Task-bound decisions and durable dispatch reservations (SQLite BEGIN IMMEDIATE).

Plan records are never reservations. Only prepared/launching/launched/started rows
with a live lease hold budget. No daemon or agent is started by this module.
"""
from contextlib import contextmanager
from copy import deepcopy
from worker_context import require_parent_id
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
import provider_inventory

ACTIVE = ('prepared', 'launching', 'launched', 'started')
TERMINAL = ('completed', 'failed', 'canceled', 'expired')
DECISION_TTL = 900
PREPARE_LEASE = 300
RUN_LEASE = 600
MAX_LIFETIME = 86400
LABELS = ('router.decision_id', 'router.dispatch_id', 'router.parent_id',
          'router.task_id', 'router.account', 'router.bundle_id', 'router.request_hash')
BOUND = ('provider', 'settings')


def utcnow():
    return datetime.now(timezone.utc)


def stamp(now):
    return now.isoformat()


def parse(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def db_path():
    return Path(os.environ.get('ROUTER_DISPATCH_DB') or Path.home() / '.route-manager/dispatch.sqlite3')


@contextmanager
def transaction(path=None):
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS dispatches (id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def rows(connection, table):
    return [json.loads(r[0]) for r in connection.execute('SELECT data FROM ' + table)]


def get(connection, table, key):
    row = connection.execute('SELECT data FROM ' + table + ' WHERE id=?', (key,)).fetchone()
    if row is None:
        raise ValueError('Unknown ' + table.rstrip('s') + ' id')
    return json.loads(row[0])


def save(connection, row):
    connection.execute('INSERT INTO dispatches(id,decision_id,data) VALUES (?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (row['dispatch_id'], row['decision_id'], json.dumps(row, ensure_ascii=False)))


def event(row, state, now, reason=None):
    row.update(state=state, updated_at=stamp(now))
    row.setdefault('events', []).append({'state': state, 'at': stamp(now), **({'reason': reason} if reason else {})})
    if state in TERMINAL:
        row.update(released_at=stamp(now), lease_until=stamp(now))


def expire(connection, now):
    for row in rows(connection, 'dispatches'):
        if row['state'] in ACTIVE and parse(row['lease_until']) <= now:
            event(row, 'expired', now, 'lease_elapsed_without_fresh_host_observation')
            save(connection, row)


def resolve_bundle(bundle, accounts, explicit_override=False):
    """Catalog defaults are not the complete supported explicit-effort contract."""
    import allocator
    model = allocator.canonical(bundle.get('model'), accounts)
    if bundle.get('fast') and not allocator.models_table(accounts).get(model, {}).get('fast_capable'):
        raise ValueError(f'Model {model!r} does not support fast mode')
    siblings = [b for b in allocator.bundles(accounts).values()
                if b['model'] == model and all(b.get(k) == bundle.get(k) for k in ('provider', 'account'))]
    exact = next((b for b in siblings if b.get('effort') == bundle.get('effort')), None)
    if exact:
        return dict(exact, bundle_id=exact['id'], **({'fast': True} if bundle.get('fast') else {}))
    if not explicit_override:
        return None
    contract = accounts.get('dispatch_efforts', {}).get(bundle.get('provider'), {}).get(model, [])
    if not siblings or bundle.get('effort') not in contract:
        raise ValueError('Explicit override has no known provider/account/model/effort contract; refresh discovery and the local contract')
    base = next((b for b in siblings if not b.get('disabled')), siblings[0])
    identity = {k: bundle.get(k) for k in ('provider', 'account', 'effort')}
    identity['model'] = model
    key = 'user_override_' + digest(identity)[:24]
    return dict(base, id=key, bundle_id=key, effort=bundle['effort'], **({'fast': True} if bundle.get('fast') else {}))


def check_launch_quota(bundle, accounts, usage, now):
    """Same existing account protection for prepared MCP and direct SDK launches.

    Local disablement also applies after preparation. For quota, only fresh,
    active-window observations hard-block; unknown/stale is not exhausted.
    """
    import allocator
    reason = provider_inventory.unavailable_reason(accounts.get('_discovery') or {}, bundle['account'], bundle['provider'])
    if reason:
        raise ValueError(reason)
    if bundle['account'] not in accounts['accounts']:
        raise ValueError('Account is no longer configured; reroute using the current provider inventory')
    usage = provider_inventory.merge_usage(usage, accounts, now)
    spec = accounts['accounts'][bundle['account']]
    row = (usage.get('accounts') or {}).get(bundle['account']) or {}
    for wid, window, observed in allocator.applicable_windows(spec, bundle['model'], row):
        if allocator.below_launch_floor(window, observed, row, now):
            floor = window['launch_min_remaining_pct']
            raise ValueError(f"Account {bundle['account']} {wid} is below its existing launch protection ({floor}% remaining floor); refresh quota or replan")


def issue(result, task, now=None, path=None, allocation_task=None):
    """Register a decision, without committing capacity. Legacy unbound decisions cannot dispatch."""
    now = now or utcnow()
    bundle = dict(result.get('selected_bundle') or {})
    if not bundle or result.get('choice') in (None, 'none', 'self'):
        return {}
    bundle.setdefault('bundle_id', result.get('choice'))
    import allocator
    accounts = allocator.load_accounts()
    explicit = result.get('stage') == 'record' and result.get('constraint') == 'user_override' and bool(result.get('basis'))
    resolved = resolve_bundle(bundle, accounts, explicit)
    if resolved:
        bundle = {**bundle, **resolved}
    parent = task.get('parent_agent_id')
    task_id = task.get('task_id')
    row = {'decision_id': str(uuid.uuid4()), 'task_id': task_id, 'parent_id': parent,
           'task_hash': digest(task), 'task': task, 'allocation_task': allocation_task or task, 'bundle': bundle,
           'bound': bool(isinstance(parent, str) and parent and isinstance(task_id, str) and task_id),
           'created_at': stamp(now), 'valid_until': stamp(now + timedelta(seconds=DECISION_TTL)),
           'constraint': result.get('constraint'),
           'effort_override': bool(explicit and resolved and resolved['id'].startswith('user_override_')),
           'dispatchable': not (result.get('decision') or {}).get('needs_user_decision', False)
                           and not result.get('host_policy_violation')
                           and bool(resolved)
                           and (result.get('decision') or {}).get('action') not in ('defer', 'resume')}
    with transaction(path) as connection:
        connection.execute('INSERT INTO decisions VALUES (?,?)', (row['decision_id'], json.dumps(row, ensure_ascii=False)))
    return {k: row[k] for k in ('decision_id', 'task_id', 'parent_id', 'task_hash', 'bound', 'valid_until')}


def canonical_request(request):
    allowed = {'title', 'provider', 'initialPrompt', 'workspaceId', 'cwd', 'settings', 'labels', 'notifyOnFinish'}
    if set(request) - allowed:
        raise ValueError('Unsupported create_agent fields; put dispatch metadata in labels')
    value = {k: v for k, v in request.items() if k != 'labels' and v is not None}
    value.setdefault('settings', {})
    value.setdefault('notifyOnFinish', True)
    value['labels'] = {k: v for k, v in (request.get('labels') or {}).items() if not k.startswith('router.')}
    return value


def bound_hash(request):
    """Bind only what the decision chose; title/prompt/workspace/cwd stay the parent's to edit until launch."""
    value = canonical_request(request)
    return digest({k: value[k] for k in BOUND})


def launch_request(bundle, launch):
    if not isinstance(launch, dict) or not isinstance(launch.get('initialPrompt'), str) or not launch['initialPrompt'].strip():
        raise ValueError('launch.initialPrompt must contain the reviewed assignment')
    if not isinstance(launch.get('title'), str) or not 0 < len(launch['title']) <= 60:
        raise ValueError('launch.title must contain 1..60 characters')
    if set(launch) - {'title', 'initialPrompt', 'workspaceId', 'cwd', 'labels', 'notifyOnFinish'}:
        raise ValueError('Launch configuration comes from the decision; only title/prompt/workspace/cwd/labels/notify are inputs')
    if 'cwd' in launch and not (isinstance(launch['cwd'], str) and os.path.isabs(launch['cwd'])):
        raise ValueError('launch.cwd must be an absolute directory')
    if any(k.startswith('router.') for k in launch.get('labels', {})):
        raise ValueError('router.* labels are generated, not caller supplied')
    provider = bundle['provider']
    family = provider_inventory.launcher_family(provider)
    if provider not in ('codex', 'codex-2', 'claude', 'claude-2', 'pi', 'antigravity-resilient', 'opencode') and family not in ('codex', 'claude', 'pi', 'antigravity-resilient', 'opencode', 'grok', 'acp'):
        raise ValueError('This dispatch adapter supports verified Paseo providers only; use the provider-specific runner otherwise')
    model = bundle.get('launch_model') or bundle['model']
    settings = {}
    mode = bundle.get('modeId') or ({'codex': 'full-access', 'codex-2': 'full-access',
        'claude': 'bypassPermissions', 'claude-2': 'bypassPermissions', 'antigravity-resilient': 'full-access',
        'opencode': 'build', 'pi': 'full-access'}.get(family or provider))
    if mode:
        settings['modeId'] = mode
    if bundle.get('effort') and provider != 'antigravity-resilient' and family != 'antigravity-resilient':
        settings['thinkingOptionId'] = bundle['effort']
    if bundle.get('fast') and (provider in ('codex', 'codex-2') or family == 'codex'):
        settings['features'] = {'fast_mode': True}
    return canonical_request(dict(launch, provider=provider + '/' + model, settings=settings))


def active_records(connection, now):
    return [{'stage': 'dispatch', 'dispatch_id': r['dispatch_id'], 'state': r['state'],
             'at': r['created_at'], 'lease_until': r['lease_until'], 'choice': r['bundle']['bundle_id'],
             'bundle': r['bundle'], 'expected_usd': r['expected_usd'], 'usage': r['usage']}
            for r in rows(connection, 'dispatches')
            if r['state'] in ACTIVE and parse(r['lease_until']) > now]


def reservations(now=None, path=None, read_only=False):
    if not Path(path or db_path()).exists():
        return []
    now = now or utcnow()
    if read_only:
        connection = sqlite3.connect(Path(path or db_path()).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            return active_records(connection, now)
        finally:
            connection.close()
    with transaction(path) as connection:
        expire(connection, now)
        return active_records(connection, now)


def prepare(decision_id, task, launch, usage, accounts=None, now=None, path=None):
    require_parent_id(task.get('parent_agent_id'))
    import allocator
    now = now or utcnow()
    accounts = accounts or allocator.load_accounts()
    with transaction(path) as connection:
        expire(connection, now)
        decision = get(connection, 'decisions', decision_id)
        if not decision['bound'] or decision['task_hash'] != digest(task):
            raise ValueError('Decision is unbound or belongs to another task/parent/input')
        request = launch_request(decision['bundle'], launch)
        request_hash = bound_hash(request)
        previous = connection.execute('SELECT data FROM dispatches WHERE decision_id=?', (decision_id,)).fetchone()
        if previous:
            row = json.loads(previous[0])
            if row['state'] == 'prepared':
                # Re-preparing revises the assignment text in place; the bound launch fields cannot differ.
                request['labels'].update({k: row['request']['labels'][k] for k in LABELS})
                row['request'] = request
                save(connection, row)
            return row
        if parse(decision['valid_until']) <= now or not decision['dispatchable']:
            raise ValueError('Decision expired, deferred or requires a new route; run plan/select/record again')
        for old in rows(connection, 'dispatches'):
            unresolved = old['state'] in ACTIVE or (old['state'] == 'expired' and old.get('launched_at'))
            if unresolved and (old['parent_id'], old['task_id']) == (decision['parent_id'], decision['task_id']):
                raise ValueError('This parent/task already has an active dispatch; reconcile it first')
        bundle = decision['bundle']
        check_launch_quota(bundle, accounts, usage, now)
        if decision.get('effort_override'):
            resolved = resolve_bundle(bundle, accounts, decision.get('constraint') == 'user_override')
            if resolved is None or any(resolved.get(k) != bundle.get(k)
                    for k in ('id', 'provider', 'account', 'model', 'effort', 'modeId', 'launch_model')):
                raise ValueError('Explicit override contract changed; record a new decision')
            # Add only to this calculation, never to defaults or persistent account settings.
            accounts = deepcopy(accounts)
            accounts['accounts'][bundle['account']]['bundles'][bundle['bundle_id']] = resolved
        # Capacity check and insertion share the same transaction across concurrent parents.
        alloc_task = dict(decision['allocation_task'])
        if bundle.get('fast') and decision.get('constraint') == 'user_override':
            alloc_task['force_fast'] = True
        elif not bundle.get('fast'):
            alloc_task['disable_fast'] = True
        plan = allocator.plan(alloc_task, usage, now, records=active_records(connection, now), accounts=accounts)
        candidate = next((c for c in plan['ranking'] if c['id'] == bundle['bundle_id']), None)
        if candidate is None or any(candidate['bundle'].get(k) != bundle.get(k) for k in ('provider', 'account', 'model', 'effort')):
            raise ValueError('Decision bundle no longer matches installed account configuration')
        exclusions = list(candidate['excluded'])
        if decision.get('constraint') == 'user_override':
            exclusions = [e for e in exclusions if not e.startswith(('ability ', 'explicit model constraint'))]
        if bundle.get('fast') and not candidate['bundle'].get('fast') and not exclusions:
            exclusions.append('fast mode quota is no longer sufficient')
        if exclusions or candidate.get('available_in_hours'):
            raise ValueError('Capacity/availability changed; replan: ' + '; '.join(exclusions or ['reset not yet observed']))
        dispatch_id = str(uuid.uuid4())
        labels = {'router.decision_id': decision_id, 'router.dispatch_id': dispatch_id,
                  'router.parent_id': decision['parent_id'], 'router.task_id': decision['task_id'],
                  'router.account': bundle['account'], 'router.bundle_id': bundle['bundle_id'],
                  'router.request_hash': request_hash}
        request['labels'].update(labels)
        row = {'dispatch_id': dispatch_id, 'decision_id': decision_id,
               'parent_id': decision['parent_id'], 'task_id': decision['task_id'], 'task_hash': decision['task_hash'],
               'request_hash': request_hash, 'request': request, 'bundle': bundle,
               'expected_usd': candidate['expected_usd'], 'usage': usage.get('accounts', {}),
               'created_at': stamp(now), 'lease_until': stamp(now + timedelta(seconds=PREPARE_LEASE)),
               'hard_until': stamp(now + timedelta(seconds=MAX_LIFETIME)), 'agent_id': None}
        event(row, 'prepared', now)
        save(connection, row)
        return row


def inspect(dispatch_id, path=None):
    with transaction(path) as connection:
        return get(connection, 'dispatches', dispatch_id)


def missing_agent(dispatch_id, agent_id, now=None, path=None):
    """A successful host lookup found a previously acknowledged agent removed."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        if not agent_id or row.get('agent_id') != agent_id:
            raise ValueError('Missing-agent observation does not match an acknowledged agent')
        if row['state'] in ACTIVE or row['state'] == 'expired':
            event(row, 'canceled', now, 'host_confirmed_agent_removed')
            save(connection, row)
        return row


def claim(request, parent_id=None, call_id=None, now=None, path=None):
    """One-use pre-launch claim. Retries of the same host tool-call ID are idempotent."""
    now = now or utcnow()
    labels = request.get('labels') or {}
    with transaction(path) as connection:
        row = get(connection, 'dispatches', labels.get('router.dispatch_id'))
        require_parent_id(row['parent_id'])
        if any(labels.get(k) != row['request']['labels'][k] for k in LABELS):
            raise ValueError('Dispatch labels do not match decision/account/task/parent')
        if parent_id is not None and parent_id != row['parent_id']:
            raise ValueError('Dispatch belongs to another parent')
        if bound_hash(request) != row['request_hash']:
            raise ValueError('Dispatch launch configuration changed (provider/model/mode/effort); '
                             'title/initialPrompt/workspaceId may be edited, these may not')
        if row['state'] == 'launching' and call_id and row.get('claim_id') == call_id and parse(row['lease_until']) > now:
            return row
        if row['state'] != 'prepared' or parse(row['lease_until']) <= now:
            raise ValueError('Dispatch already claimed, terminal or expired; reconcile instead of replaying create_agent')
        import allocator
        import route_judge
        check_launch_quota(row['bundle'], allocator.load_accounts(), route_judge.usage_snapshot(now=now), now)
        row.update(claim_id=call_id or str(uuid.uuid4()), launched_at=stamp(now),
                   lease_until=stamp(min(now + timedelta(seconds=RUN_LEASE), parse(row['hard_until']))))
        event(row, 'launching', now)
        save(connection, row)
        return row


def observe(dispatch_id, snapshot, now=None, path=None):
    """Accept only the bound host agent; start/terminal receipts are idempotent."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        labels = snapshot.get('labels') or {}
        if any(labels.get(k) != row['request']['labels'][k] for k in LABELS):
            raise ValueError('Host snapshot belongs to another dispatch/parent/task')
        agent_id = snapshot.get('id')
        if not isinstance(agent_id, str) or not agent_id or row.get('agent_id') not in (None, agent_id):
            raise ValueError('Host agent ID mismatch')
        provider, _, model = row['request']['provider'].partition('/')
        if snapshot.get('provider') != provider or snapshot.get('model') != model:
            raise ValueError('Host provider/model differs from dispatched bundle')
        if snapshot.get('status') == 'running':
            settings = row['request']['settings']
            if settings.get('modeId') and snapshot.get('currentModeId') != settings['modeId']:
                raise ValueError('Host permission mode differs from dispatched settings')
            effort = snapshot.get('effectiveThinkingOptionId') or snapshot.get('thinkingOptionId')
            if settings.get('thinkingOptionId') and effort != settings['thinkingOptionId']:
                raise ValueError('Host thinking option differs from dispatched settings')
        if row['state'] in TERMINAL and row['state'] != 'expired':
            return row
        if row['state'] == 'prepared':
            raise ValueError('Agent observed without a launch claim')
        updated = snapshot.get('updatedAt')
        if not isinstance(updated, str) or parse(updated) < parse(row['launched_at']):
            raise ValueError('Host snapshot predates this launch')
        row['agent_id'] = agent_id
        row['observed_at'] = stamp(now)
        status = snapshot.get('status')
        reason = snapshot.get('attentionReason')
        target = None
        if status == 'error' or reason == 'error' or snapshot.get('providerUnavailable'):
            target = 'failed'
        elif status == 'closed' or snapshot.get('archivedAt'):
            target = 'canceled'
        elif reason == 'permission' or snapshot.get('pendingPermissionCount', 0) > 0:
            target = 'started' if row.get('started_at') else 'launched'
        elif status == 'idle' and (reason == 'finished' or row.get('started_at')):
            target = 'completed'
        elif status == 'running':
            row.setdefault('started_at', stamp(now))
            target = 'started'
        elif status in ('initializing', 'idle'):
            target = 'launched'
        if target is None:
            raise ValueError('Unrecognized host status; reservation retained until lease expiry')
        # A fresh host observation can recover an expired lease, but never outlive the hard bound.
        if target in ACTIVE and now >= parse(row['hard_until']):
            target = 'expired'
        if target in ACTIVE:
            row.pop('released_at', None)
            row['lease_until'] = stamp(min(now + timedelta(seconds=RUN_LEASE), parse(row['hard_until'])))
        if row['state'] != target:
            event(row, target, now)
        save(connection, row)
        return row


def release(dispatch_id, parent_id, reason, now=None, path=None, failed=False):
    """Explicit cancellation/known pre-send failure, not an invented terminal host receipt."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        if parent_id != row['parent_id']:
            raise ValueError('Dispatch belongs to another parent')
        if row['state'] in ('launching', 'launched', 'started') and not failed:
            raise ValueError('Observe terminal status before releasing a launched dispatch; release does not cancel an agent')
        if row['state'] not in TERMINAL or row['state'] == 'expired':
            event(row, 'failed' if failed else 'canceled', now, reason)
            save(connection, row)
        return row


def pending(path=None):
    if not Path(path or db_path()).exists():
        return []
    with transaction(path) as connection:
        # Include expired started/uncertain launches so a later observation can repair them.
        return [r for r in rows(connection, 'dispatches') if r['state'] in ACTIVE
                or (r['state'] == 'expired' and r.get('launched_at') and parse(r['hard_until']) > utcnow())]
