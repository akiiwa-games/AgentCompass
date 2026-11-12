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
from host_placement import canonical_dir
import provider_inventory


def bundle_windows(bundle):
    """Quota windows a dispatched bundle draws; None (whole account) when they cannot be resolved."""
    try:
        import account_fleet
        import quota_pools
        return quota_pools.bundle_pools(bundle, account_fleet.load_accounts()) or None
    except Exception:
        return None

ACTIVE = ('prepared', 'launching', 'launched', 'started')
TERMINAL = ('completed', 'failed', 'create_failed', 'canceled', 'expired')
DECISION_TTL = 900
PREPARE_LEASE = 300
RUN_LEASE = 600
# A bridge sends a create only within this window after the claim, so a request the daemon has not admitted by the
# (later) create deadline can never arrive. The deadline leaves room for the daemon's own provider timeouts (120 s
# refresh, 60 s run start, then the prompt); Antigravity's cold start needs longer. Both stay inside RUN_LEASE.
CREATE_SEND_WINDOW = 60
CREATE_DEADLINE = 240
CREATE_DEADLINES = {'antigravity-resilient': 480}
MAX_LIFETIME = 86400
# A worker idle longer than this is not resumed: its runtime and prompt cache are gone.
REUSE_MAX_IDLE_HOURS = 24
LABELS = ('router.decision_id', 'router.dispatch_id', 'router.parent_id',
          'router.task_id', 'router.account', 'router.bundle_id', 'router.request_hash')
BOUND = ('provider', 'settings')


class NotBound(ValueError):
    """Paseo answered, and this worker does not carry this dispatch's labels."""


class CapacityChanged(ValueError):
    """The decision's bundle can no longer start (an exclusion, a launch floor, a disabled account or model, a live
    model list that dropped it): the attempt failed before anything was sent, so the brief may go to the next candidate."""


class WorkspaceBusy(ValueError):
    """The workspace already has a writer; `holders` names each one."""

    def __init__(self, workspace, holders):
        super().__init__(f'Workspace {workspace} already has a writer')
        self.workspace, self.holders = workspace, holders


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
    return Path(os.environ.get('ROUTER_DISPATCH_DB') or Path.home() / '.agent-compass/dispatch.sqlite3')


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
        raise ValueError('Unknown ' + {'dispatches': 'dispatch'}.get(table, table.rstrip('s')) + ' id')
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
    import account_fleet
    import fast_modes
    import auto_bundles
    model = account_fleet.canonical(bundle.get('model'), accounts)
    if bundle.get('fast'):
        fast_modes.validate(account_fleet.bundle_harness(bundle, bundle.get('account')), model,
                                      account_fleet.model_spec(accounts, model))
    own_model = account_fleet.row_key(bundle.get('model') or '', accounts)
    siblings = [b for b in account_fleet.bundles(accounts).values()
                if account_fleet.row_key(b.get('model') or '', accounts) == own_model and all(b.get(k) == bundle.get(k) for k in ('provider', 'account'))]
    bid = bundle.get('bundle_id') or bundle.get('id')
    by_id = next((b for b in siblings if bid and b.get('id') == bid), None)
    carrier = next((b for b in siblings if bundle.get('pin_carrier') and b.get('id') == bundle['pin_carrier']), None)
    if carrier and bundle.get('effort') in (account_fleet.dispatch_allowed_efforts(carrier, accounts, model) or ()):
        # The plan already matched the effort the user named to the launch contract; the row exists only for this task.
        return dict(carrier, id=bid, bundle_id=bid, effort=bundle['effort'], pin_carrier=bundle['pin_carrier'],
                    **({'fast': True} if bundle.get('fast') else {}))
    if by_id and by_id.get('effort') == bundle.get('effort'):
        return dict(by_id, bundle_id=by_id['id'], **({'fast': True} if bundle.get('fast') else {}))
    if not by_id:
        exact = (next((b for b in siblings if b.get('effort') == bundle.get('effort') and not b.get('disabled')), None)
                 or next((b for b in siblings if b.get('effort') == bundle.get('effort')), None))
        if exact:
            return dict(exact, bundle_id=exact['id'], **({'fast': True} if bundle.get('fast') else {}))
    # A named level with no row of its own arrives without the row's list, so every row of the model speaks for what it offers.
    contract = {eff for row in (bundle, *siblings) for eff in account_fleet.dispatch_allowed_efforts(row, accounts, model) or ()}
    if not explicit_override:
        return None
    exact = (next((b for b in siblings if b.get('effort') == bundle.get('effort') and not b.get('disabled')), None)
             or next((b for b in siblings if b.get('effort') == bundle.get('effort')), None))
    if exact:
        return dict(exact, bundle_id=exact['id'], **({'fast': True} if bundle.get('fast') else {}))
    if not siblings or bundle.get('effort') not in contract:
        raise ValueError('Explicit override has no known provider/account/model/effort contract; refresh discovery and the local contract')
    base = next((b for b in siblings if not b.get('disabled')), siblings[0])
    identity = {k: bundle.get(k) for k in ('provider', 'account', 'effort')}
    identity['model'] = own_model
    key = 'user_override_' + digest(identity)[:24]
    mode = base.get('modeId') or auto_bundles.HARNESS_MODE.get(base.get('harness'))
    return dict(base, id=key, bundle_id=key, effort=bundle['effort'], **({'modeId': mode} if mode else {}),
                **({'launch_model': bundle['launch_model']} if bundle.get('launch_model') else {}),
                **({'fast': True} if bundle.get('fast') else {}))


def check_launch_quota(bundle, accounts, usage, now):
    """Same existing account protection for prepared MCP and direct SDK launches.

    Local disablement also applies after preparation. For quota, only fresh,
    active-window observations hard-block; unknown/stale is not exhausted.
    """
    import account_fleet
    import fast_modes
    import quota_pools
    reason = provider_inventory.unavailable_reason(accounts.get('_discovery') or {}, bundle['account'], bundle['provider'])
    if reason:
        raise ValueError(reason)
    if bundle['account'] not in accounts['accounts']:
        raise ValueError('Account is no longer configured; reroute using the current provider inventory')
    if bundle.get('fast'):
        model = account_fleet.canonical(bundle['model'], accounts)
        fast_modes.validate(account_fleet.bundle_harness(bundle, bundle['account']), model,
                                      account_fleet.model_spec(accounts, model))
    usage = provider_inventory.merge_usage(usage, accounts, now)
    spec = accounts['accounts'][bundle['account']]
    row = (usage.get('accounts') or {}).get(bundle['account']) or {}
    for wid, window, observed in quota_pools.applicable_windows(spec, bundle.get('model'), row, bundle, accounts):
        if quota_pools.below_launch_floor(window, observed, row, now):
            floor = window.get('launch_min_remaining_pct', 0)
            raise ValueError(f"Account {bundle['account']} {wid} is below its existing launch protection ({floor}% remaining floor); refresh quota or replan")


def issue(result, task, now=None, path=None, allocation_task=None):
    """Register a decision, without committing capacity. Legacy unbound decisions cannot dispatch."""
    now = now or utcnow()
    bundle = dict(result.get('selected_bundle') or {})
    if not bundle or result.get('choice') in (None, 'none', 'self'):
        return {}
    bundle.setdefault('bundle_id', result.get('choice'))
    import account_fleet
    accounts = account_fleet.load_accounts()
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
                           and (result.get('decision') or {}).get('action') != 'defer'}
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


# The one table of launcher families this Paseo adapter launches, with each family's permission mode.
# Accounts are admitted by their discovered family, never by name.
PASEO_LAUNCHER_MODES = {'codex': 'full-access', 'claude': 'bypassPermissions', 'pi': None, 'omp': 'full',
                        'antigravity-resilient': 'full-access', 'opencode': 'build', 'grok': None, 'acp': None}


def launch_request(bundle, launch):
    if not isinstance(launch, dict) or not isinstance(launch.get('initialPrompt'), str) or not launch['initialPrompt'].strip():
        raise ValueError('launch.initialPrompt must contain the reviewed assignment')
    if not isinstance(launch.get('title'), str) or not 0 < len(launch['title']) <= 60:
        raise ValueError('launch.title must contain 1..60 characters')
    if set(launch) - {'title', 'initialPrompt', 'cwd', 'labels', 'notifyOnFinish'}:
        raise ValueError('Launch configuration comes from the decision; only title/prompt/cwd/labels/notify are inputs')
    if 'cwd' in launch and not (isinstance(launch['cwd'], str) and os.path.isabs(launch['cwd'])):
        raise ValueError('launch.cwd must be an absolute directory')
    if any(k.startswith('router.') for k in launch.get('labels', {})):
        raise ValueError('router.* labels are generated, not caller supplied')
    provider = bundle['provider']
    family = provider_inventory.launcher_family(provider)
    if family not in PASEO_LAUNCHER_MODES:
        raise ValueError('This dispatch adapter supports verified Paseo providers only; use the provider-specific runner otherwise')
    model = bundle.get('launch_model') or bundle['model']
    settings = {}
    # Pi has fixed tool permissions and advertises no selectable modes. Ignore
    # the obsolete full-access value in saved/discovered Pi bundles as well.
    mode = None if family == 'pi' else bundle.get('modeId') or PASEO_LAUNCHER_MODES[family]
    if mode:
        settings['modeId'] = mode
    if bundle.get('effort') and family != 'antigravity-resilient':
        settings['thinkingOptionId'] = bundle['effort']
    if bundle.get('fast'):
        from fast_modes import validate
        validate(family, bundle['model'])
    if family == 'codex':
        settings['features'] = {'service_tier': 'priority' if bundle.get('fast') else 'default'}
    elif family == 'claude':
        settings['features'] = {'fast_mode': bool(bundle.get('fast'))}
    return canonical_request(dict(launch, provider=provider + '/' + model, settings=settings))


def active_records(connection, now):
    return [{'stage': 'dispatch', 'dispatch_id': r['dispatch_id'], 'state': r['state'],
             'at': r['created_at'], 'lease_until': r['lease_until'], 'choice': r['bundle']['bundle_id'],
             'bundle': r['bundle'], 'expected_usd': r['expected_usd'], 'usage': r['usage'],
             **({'agent_id': r['agent_id']} if r.get('agent_id') else {})}
            for r in rows(connection, 'dispatches')
            if r.get('dispatch_mode', 'paseo') == 'paseo' and r['state'] in ACTIVE and parse(r['lease_until']) > now]


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


def paseo_agent_record(agent_id, paseo_home=None):
    """Read local Paseo agent JSON for agent_id if present on disk; None when absent."""
    if not isinstance(agent_id, str) or not agent_id.strip():
        return None
    agents_dir = os.environ.get('ROUTER_PASEO_AGENTS_DIR')
    base = Path(agents_dir) if agents_dir else Path(paseo_home or os.environ.get('PASEO_HOME') or Path.home() / '.paseo') / 'agents'
    if not base.is_dir():
        return None
    for candidate in base.glob(f'*/{agent_id}.json'):
        try:
            doc = json.loads(candidate.read_text(encoding='utf-8', errors='replace'))
            if isinstance(doc, dict):
                return doc
        except (OSError, ValueError):
            continue
    return None


def reusable_workers(parent_id, now=None, path=None, paseo_home=None):
    """Completed, idle Paseo workers owned by parent_id, newest first, with compaction/context state."""
    if not isinstance(parent_id, str) or not parent_id.strip() or not Path(path or db_path()).exists():
        return []
    import account_fleet
    import worker_sessions
    now = now or utcnow()
    with transaction(path) as connection:
        expire(connection, now)
        all_rows = rows(connection, 'dispatches')
    by_agent = {}
    for row in all_rows:
        if row.get('parent_id') != parent_id:
            continue
        agent_id = row.get('agent_id')
        if isinstance(agent_id, str) and agent_id.strip():
            by_agent.setdefault(agent_id.strip(), []).append(row)
    if not by_agent:
        return []
    p_home = Path(paseo_home or os.environ.get('PASEO_HOME') or Path.home() / '.paseo')
    home_dir = p_home.parent if p_home.name == '.paseo' else Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    max_tokens = worker_sessions.rotation_threshold(account_fleet.load_accounts())
    candidates = []
    for agent_id, history in by_agent.items():
        history.sort(key=lambda r: (r.get('created_at') or '', r.get('updated_at') or ''))
        if any(r['state'] in ACTIVE or (r['state'] == 'expired' and r.get('launched_at') and parse(r['hard_until']) > now)
               for r in history):
            continue
        latest = history[-1]
        if latest.get('state') != 'completed':
            continue
        last_seen = latest.get('updated_at') or latest.get('created_at')
        if not last_seen or now - parse(last_seen) > timedelta(hours=REUSE_MAX_IDLE_HOURS):
            continue
        doc = paseo_agent_record(agent_id, paseo_home=paseo_home)
        if isinstance(doc, dict):
            labels = doc.get('labels') if isinstance(doc.get('labels'), dict) else {}
            owner = labels.get('router.parent_id') or labels.get('paseo.parent-agent-id')
            if owner and owner != parent_id:
                continue
            status = doc.get('status') or doc.get('lastStatus')
            # `closed`: Paseo collected the idle runtime. A resume must cold-restore it from persistence first,
            # which outlasts the launch call (~25 s on Antigravity) and can land the turn on a new provider session.
            if (doc.get('archivedAt') or status in ('running', 'initializing', 'error', 'closed')
                    or doc.get('attentionReason') in ('error', 'permission')
                    or doc.get('providerUnavailable') or doc.get('pendingPermissions')):
                continue
        state = worker_sessions.session_state(agent_id, home=home_dir)
        carried = worker_sessions.worker_historical_tokens(state)
        worn = bool(isinstance(carried, (int, float)) and carried >= max_tokens)
        bundle = dict(latest.get('bundle') or {})
        bundle_id = bundle.get('bundle_id') or bundle.get('id')
        chain_id = next((r.get('chain_id') for r in reversed(history) if r.get('chain_id')), None) or (
            history[0].get('task_id') or f'worker-{agent_id[:8]}')
        task_ids = list(dict.fromkeys(r.get('task_id') for r in reversed(history) if r.get('task_id')))
        brief_paths = list(dict.fromkeys(r.get('brief_path') for r in reversed(history) if r.get('brief_path')))
        titles = list(dict.fromkeys((r.get('request') or {}).get('title') or '' for r in reversed(history)
                                    if (r.get('request') or {}).get('title')))
        profiles = [r['profile'] for r in reversed(history) if isinstance(r.get('profile'), dict)]
        read_only_review = bool(history and all(bool(r.get('read_only_review')) for r in history))
        candidates.append({
            'agent_id': agent_id,
            'bundle': bundle,
            'bundle_id': bundle_id,
            'chain_id': chain_id,
            'task_id': latest.get('task_id'),
            'task_ids': task_ids,
            'brief_path': latest.get('brief_path'),
            'brief_paths': brief_paths,
            'title': titles[0] if titles else '',
            'titles': titles,
            'profiles': profiles,
            'read_only_review': read_only_review,
            'updated_at': latest.get('updated_at') or latest.get('created_at') or '',
            'session_state': state,
            'worn': worn,
        })
    candidates.sort(key=lambda c: c['updated_at'], reverse=True)
    return candidates


def chain_root(row):
    """The dispatch the parent was given for this brief: an automatic relaunch is an attempt of its first dispatch."""
    return row.get('attempt_of') or row['dispatch_id']


RETRY_CANCELED_NEXT = ('The parent released this brief while AgentCompass waited to retry it, so the scheduled retry is '
                       'canceled and nothing runs this brief.')
RESUMED_NEXT = ('The parent sent a follow-up turn to the failed worker and it is running again, so AgentCompass '
                'retired the pending fallback and continues on that worker.')


def retire_scheduled(fallback, now, result, next_action, root_id=None, *, current=None, extra_ids=()):
    """End a pending fallback in the caller's transaction: a scheduled same-bundle retry (`current=None`, for release or
    expiry) or any pending retry/switch when the parent resumed `current`. The first dispatch's attempt record becomes
    final and pending claims become terminal, so a due-retry or watcher call that runs later finds it handled and starts
    nothing. Returns the retired attempt's id, or None when nothing was retired."""
    retry_of = fallback.get('retry_of')
    handled = dict(fallback.get('handled') or {})
    if current is None:
        if fallback.get('state') != 'retrying' or not retry_of:
            return None
        targets = [retry_of]
    else:
        pending = [k for k, v in handled.items() if isinstance(v, dict) and v.get('action') in ('scheduled', 'pending')]
        if retry_of and retry_of not in pending:
            pending.insert(0, retry_of)
        resumed = fallback.get('state') == 'final' and (fallback.get('outcome') or {}).get('result') == result
        if fallback and not resumed and fallback.get('state') not in ('retrying', 'relaunching') and not pending:
            return None
        targets = list(dict.fromkeys([*pending, *(k for k in extra_ids if k), current]))
    outcome = {'action': 'final', 'result': result, 'root_dispatch_id': root_id,
               'lines': fallback.get('lines') or [], 'text': next_action, 'at': stamp(now)}
    fallback['handled'] = {**handled, **{target: outcome for target in targets}}
    fallback.update(state='final', outcome={'result': result, 'next_action': next_action}, updated_at=stamp(now),
                    **({'current': current} if current is not None else {}))
    for key in ('until', 'retry_at', 'retry_of'):
        fallback.pop(key, None)
    return targets[0]


def fallback_guard(row, now):
    """AgentCompass is between two attempts of this brief (waiting out a transient backoff or relaunching): no worker
    runs, but one is about to, so the brief counts as dispatched until the guard lapses."""
    guard = row.get('fallback') or {}
    if guard.get('state') in ('retrying', 'relaunching') and isinstance(guard.get('until'), str):
        try:
            if parse(guard['until']) > now:
                return True
        except ValueError:
            pass
    for item in (guard.get('handled') or {}).values():
        if isinstance(item, dict) and item.get('action') == 'pending' and isinstance(item.get('until'), str):
            try:
                if parse(item['until']) > now:
                    return True
            except ValueError:
                pass
    return False


def unresolved(row, now, exempt_chain=None):
    """A launch whose worker may be alive or may still appear; an expired Paseo lease stays open until a later
    observation. For non-Paseo host claims (`orca`, `t3`, `native`, `herdr`, `cli`) where roll has no daemon
    reconcile channel, the claim holds while its lease is live. A brief AgentCompass is relaunching is open too,
    except to that relaunch itself (`exempt_chain`, its first dispatch)."""
    if row.get('dispatch_mode', 'paseo') != 'paseo':
        return row['state'] in ACTIVE and parse(row['lease_until']) > now
    return row['state'] in ACTIVE or (row['state'] == 'expired' and bool(row.get('launched_at')) and parse(row['hard_until']) > now) \
        or (fallback_guard(row, now) and chain_root(row) != exempt_chain)


def holder(row):
    return {'dispatch_id': row['dispatch_id'], 'state': row['state'], 'agent_id': row.get('agent_id'),
            'task_id': row.get('task_id'), 'title': (row.get('request') or {}).get('title')}


def is_writer(row, now, caller, exempt_chain=None):
    # A row from before dispatches recorded their access counts as a writer. The caller is not its own conflict.
    return row.get('access') != 'read_only' and not (caller and row.get('agent_id') == caller) \
        and unresolved(row, now, exempt_chain)


def row_directories(row):
    request = row.get('request') if isinstance(row.get('request'), dict) else {}
    return {d for d in (canonical_dir(row.get('workspace')),
                        canonical_dir(row.get('cwd')),
                        canonical_dir(request.get('cwd'))) if d}


def writers_in(connection, directory, now, caller=None, exempt_chain=None):
    place = canonical_dir(directory)
    if not place:
        return []
    return [holder(r) for r in rows(connection, 'dispatches')
            if place in row_directories(r) and is_writer(r, now, caller, exempt_chain)]


def writers_at(directory, caller=None, now=None, path=None):
    """Unsettled writers recorded in canonical `directory` across all hosts."""
    now = now or utcnow()
    if not directory or not Path(path or db_path()).exists():
        return []
    with transaction(path) as connection:
        expire(connection, now)
        return writers_in(connection, directory, now, caller)


def prepare(decision_id, task, launch, usage, accounts=None, now=None, path=None, resume_agent_id=None, fallback_prompt=None,
            access='write', new_worker=False, workspace=None, cwd=None, attempt=None):
    """`attempt` ({attempt_of, attempt, previous_attempt}) prepares an automatic relaunch of a brief whose earlier
    attempt failed (dispatch_fallback): that chain's own rows do not block it, its own live rows still do."""
    require_parent_id(task.get('parent_agent_id'))
    if isinstance(launch, dict) and 'workspaceId' in launch:
        raise ValueError('launch cannot carry a host workspaceId; pass canonical directory workspace instead')
    exempt = (attempt or {}).get('attempt_of')
    canon_ws = canonical_dir(workspace)
    cwd = canon_ws or cwd
    if canon_ws and isinstance(launch, dict):
        launch = dict(launch, cwd=canon_ws)
    import allocator
    import account_fleet
    import candidate_ranking
    import dispatch_ground_truth
    import ledger_snapshot
    now = now or utcnow()
    accounts = accounts or account_fleet.load_accounts()
    launch_git = dispatch_ground_truth.capture_launch_git(cwd or (launch.get('cwd') if isinstance(launch, dict) else None))
    with transaction(path) as connection:
        expire(connection, now)
        decision = get(connection, 'decisions', decision_id)
        if not decision['bound'] or decision['task_hash'] != digest(task):
            raise ValueError('Decision is unbound or belongs to another task/parent/input')
        request = launch_request(decision['bundle'], launch)
        if cwd is None and request.get('cwd'):
            cwd = request['cwd']
        request_hash = bound_hash(request)
        previous = connection.execute('SELECT data FROM dispatches WHERE decision_id=?', (decision_id,)).fetchone()
        if previous:
            row = json.loads(previous[0])
            if row['state'] == 'prepared':
                # Re-preparing revises the assignment text in place; the bound launch fields cannot differ.
                request['labels'].update({k: row['request']['labels'][k] for k in LABELS})
                row['request'] = request
                if canon_ws:
                    row['workspace'] = canon_ws
                if cwd and row.get('launch_commit') is None:
                    row.update(cwd=cwd, **launch_git)
                save(connection, row)
            return row
        if parse(decision['valid_until']) <= now or not decision['dispatchable']:
            raise ValueError('Decision expired, deferred or requires a new route; run plan/select/record again')
        if canon_ws and access != 'read_only':
            # Same transaction as the insert: of two concurrent rolls only the first becomes the writer.
            holders = writers_in(connection, canon_ws, now, task['parent_agent_id'], exempt)
            if holders:
                raise WorkspaceBusy(canon_ws, holders)
        busy_agents = set()
        for old in rows(connection, 'dispatches'):
            if not unresolved(old, now, exempt):
                continue
            if not new_worker and (old['parent_id'], old['task_id']) == (decision['parent_id'], decision['task_id']):
                raise ValueError('This parent/task already has an active dispatch; reconcile it first')
            if old.get('agent_id'):
                busy_agents.add(old['agent_id'])
        bundle = decision['bundle']
        try:
            check_launch_quota(bundle, accounts, usage, now)
        except ValueError as exc:
            raise CapacityChanged(str(exc)) from exc
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
        if isinstance(alloc_task.get('profile'), dict):     # a decision recorded before the four categories
            alloc_task['profile'] = ledger_snapshot.ledger_profile(alloc_task['profile'])
        profile = (
            alloc_task.get('profile')
            if isinstance(alloc_task.get('profile'), dict)
            else (task.get('profile') if isinstance(task.get('profile'), dict) else None)
        )
        read_only_review = (
            bool(profile.get('read_only_review'))
            if isinstance(profile, dict) and 'read_only_review' in profile
            else bool(isinstance(profile, dict) and profile.get('work_kind') == 'debug_review' and profile.get('code_complexity') == 0)
        )
        if read_only_review and isinstance(alloc_task.get('chain'), dict):
            cw = alloc_task['chain'].get('worker')
            if not resume_agent_id or (isinstance(cw, dict) and cw.get('read_only_review') is False):
                alloc_task.pop('chain', None)
        if bundle.get('fast') and decision.get('constraint') == 'user_override':
            alloc_task['force_fast'] = True
        elif not bundle.get('fast'):
            alloc_task['disable_fast'] = True
        if decision.get('constraint') == 'user_override':
            alloc_task['pinned_bundle'] = bundle['bundle_id']
        plan = allocator.plan(alloc_task, usage, now, records=active_records(connection, now), accounts=accounts)
        candidate = candidate_ranking.candidate_for_bundle(plan['ranking'], bundle['bundle_id'], bundle.get('fast'))
        if candidate is None or any(candidate['bundle'].get(k) != bundle.get(k) for k in ('provider', 'account', 'model', 'effort')):
            raise CapacityChanged('Decision bundle no longer matches installed account configuration')
        exclusions = list(candidate['excluded'])
        if decision.get('constraint') == 'user_override':
            import provider_retry
            # A named model on a held account or model is the probe that can lift the hold (provider_retry).
            exclusions = [e for e in exclusions if not e.startswith(('ability ', 'explicit model constraint', 'explicit effort constraint',
                                                                     provider_retry.EXCLUSION_PREFIX))]
        if bundle.get('fast') and not candidate['bundle'].get('fast') and not exclusions:
            exclusions.append('fast mode quota is no longer sufficient')
        if exclusions or candidate.get('available_in_hours'):
            raise CapacityChanged('Capacity/availability changed; replan: ' + '; '.join(exclusions or ['reset not yet observed']))
        if resume_agent_id is None and candidate.get('action') == 'resume':
            chain_worker = ((alloc_task.get('chain') or {}).get('worker')
                            if isinstance(alloc_task.get('chain'), dict) else None) or {}
            if isinstance(chain_worker.get('agent_id'), str) and chain_worker['agent_id'].strip():
                resume_agent_id = chain_worker['agent_id'].strip()
        if resume_agent_id in busy_agents:
            resume_agent_id = None
        dispatch_id = str(uuid.uuid4())
        labels = {'router.decision_id': decision_id, 'router.dispatch_id': dispatch_id,
                  'router.parent_id': decision['parent_id'], 'router.task_id': decision['task_id'],
                  'router.account': bundle['account'], 'router.bundle_id': bundle['bundle_id'],
                  'router.request_hash': request_hash}
        request['labels'].update(labels)
        chain_source = alloc_task if isinstance(alloc_task.get('chain'), dict) else (
            task if not read_only_review and isinstance(task.get('chain'), dict) else {}
        )
        chain_id = (chain_source.get('chain') or {}).get('id') if (resume_agent_id or not read_only_review) else None
        row = {'dispatch_id': dispatch_id, 'decision_id': decision_id,
               'parent_id': decision['parent_id'], 'task_id': decision['task_id'], 'task_hash': decision['task_hash'],
               'request_hash': request_hash, 'request': request, 'bundle': bundle,
               'expected_usd': candidate['expected_usd'], 'usage': usage.get('accounts', {}),
               'created_at': stamp(now), 'lease_until': stamp(now + timedelta(seconds=PREPARE_LEASE)),
               'hard_until': stamp(now + timedelta(seconds=MAX_LIFETIME)),
               'operation': 'resume' if resume_agent_id else 'create',
               'agent_id': resume_agent_id, 'access': access,
               **({'workspace': canon_ws} if canon_ws else {}),
               **({'cwd': cwd} if cwd else {}),
               **launch_git,
               'fallback_prompt': fallback_prompt if resume_agent_id else None,
               'read_only_review': read_only_review,
               **({'profile': profile} if isinstance(profile, dict) else {}),
               **({'brief_path': task['brief_path']} if task.get('brief_path') else {}),
               **({'chain_id': chain_id} if chain_id else {}),
               **({k: attempt[k] for k in ('attempt_of', 'attempt', 'previous_attempt') if attempt.get(k) is not None}
                  if attempt else {})}
        event(row, 'prepared', now)
        save(connection, row)
        return row


def fallback_to_create(dispatch_id, now=None, path=None):
    """Switch a claimed resume dispatch to a fresh create when the previous worker cannot be resumed."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        if isinstance(row.get('fallback_prompt'), str) and row['fallback_prompt'].strip():
            row['request']['initialPrompt'] = row['fallback_prompt']
        row['operation'] = 'create'
        row['agent_id'] = None
        row['updated_at'] = stamp(now)
        save(connection, row)
        return row


def inspect(dispatch_id, path=None):
    with transaction(path) as connection:
        return get(connection, 'dispatches', dispatch_id)


def decision_record(decision_id, path=None):
    with transaction(path) as connection:
        return get(connection, 'decisions', decision_id)


def current_attempt(dispatch_id, path=None):
    """The attempt now carrying this brief: the dispatch the parent was given, or the relaunch that replaced it."""
    row = inspect(dispatch_id, path)
    root = row if chain_root(row) == dispatch_id else inspect(chain_root(row), path)
    return (root.get('fallback') or {}).get('current') or root['dispatch_id']


def update_fallback(root_id, change, now=None, path=None):
    """Apply change(fallback dict) to the first dispatch's attempt record in one transaction; returns the row."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', root_id)
        fallback = dict(row.get('fallback') or {})
        change(fallback)
        fallback['updated_at'] = stamp(now)
        row['fallback'] = fallback
        save(connection, row)
        return row


def create_deadline(row):
    """When a create the daemon never admitted is settled as failed; a claim is what starts the clock."""
    seconds = CREATE_DEADLINES.get(provider_inventory.launcher_family(row['bundle']['provider']), CREATE_DEADLINE)
    return parse(row['launched_at']) + timedelta(seconds=seconds)


def resolve_creation(dispatch_id, creation, now=None, path=None):
    """Settle a create no agent has appeared for, from the daemon's durable record of it (None: never admitted).

    `failed` means the daemon's run is over and it never reruns a key by itself, so nothing can appear later. Neither
    can a request never admitted once the bridge's send window has passed. An admitted create still in flight has no
    cancel on the host and can still produce a worker, so it stays open however late it is.

    Returns (row, whether this call ended it), so that of several processes settling one row only one acts on it. An
    unknown outcome says nothing against the provider, so it carries no failure_class and cools nothing down."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        if row['state'] not in ('launching', 'expired') or row.get('agent_id') or row.get('operation') != 'create':
            return row, False
        phase = creation.get('phase') if creation else None
        transitioned = True
        if phase == 'completed' or (phase == 'failed' and creation.get('agent_created')):
            # A worker existed, so it may have run: not the same as a create that never got that far.
            event(row, 'canceled', now, 'host_confirmed_agent_removed')
            row['creation'] = {'phase': phase, 'removed': True}
        elif phase == 'failed':
            cause = 'outcome_unknown' if creation.get('outcome_unknown') else creation.get('failure_class') or 'unknown'
            event(row, 'create_failed', now, 'host_creation_failed:' + cause)
            if not creation.get('outcome_unknown'):
                row['failure_class'] = creation.get('failure_class') or 'unknown'
                if isinstance(creation.get('failure_code'), str):
                    row['failure_code'] = creation['failure_code']
            row['creation'] = {'phase': phase}
        elif creation is None and now >= create_deadline(row):
            event(row, 'create_failed', now, 'host_never_admitted_create')
            row['creation'] = {'phase': 'not_admitted'}
        else:
            row['creation'] = {'phase': phase or 'not_admitted', 'in_flight': bool(phase)}
            transitioned = False
        save(connection, row)
        return row, transitioned


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
        import account_fleet
        import route_judge
        try:
            check_launch_quota(row['bundle'], account_fleet.load_accounts(), route_judge.usage_snapshot(now=now), now)
        except ValueError as exc:
            raise CapacityChanged(str(exc)) from exc
        row.update(claim_id=call_id or str(uuid.uuid4()), launched_at=stamp(now),
                   lease_until=stamp(min(now + timedelta(seconds=RUN_LEASE), parse(row['hard_until']))))
        event(row, 'launching', now)
        save(connection, row)
        return row


def finished_before(terminal, launched_at):
    """The bridge's notifier saw this launch finish; later host lifecycle (idle collection, plugin reload,
    archiving) must not rewrite that outcome."""
    if not isinstance(terminal, dict) or terminal.get('reason') != 'finished':
        return False
    at = terminal.get('at')
    try:
        return isinstance(at, str) and parse(at) >= parse(launched_at)
    except ValueError:
        return False


def provider_failed(terminal, launched_at):
    """The bridge's notifier saw this launch fail at its provider: stopped while the provider kept retrying without
    progress, or its turn ended with the provider's error."""
    if not isinstance(terminal, dict) or terminal.get('failure_code') not in ('provider_retry_stuck', 'provider_turn_failed'):
        return False
    at = terminal.get('at')
    try:
        return isinstance(at, str) and parse(at) >= parse(launched_at)
    except ValueError:
        return False


def provider_replied(terminal, launched_at):
    """The bridge's notifier saw this launch's turn end on the model's own reply. Paseo's idle/finished state reads the
    same after a cancel, so only this lifts a provider hold; a terminal without the flag says nothing."""
    if not isinstance(terminal, dict) or terminal.get('reason') != 'finished' or terminal.get('replied') is not True:
        return False
    at = terminal.get('at')
    try:
        return isinstance(at, str) and isinstance(launched_at, str) and parse(at) >= parse(launched_at)
    except ValueError:
        return False


def classify_provider_failure(dispatch_id, terminal, path=None):
    """Record a provider failure the notifier saw (provider_retry asks the classifier, so outside the registry's
    transaction) and return the scope it was recorded with, or None."""
    import provider_retry
    if not isinstance(terminal, dict) or terminal.get('failure_code') not in provider_retry.FAILURE_CODES:
        return None
    try:
        row = inspect(dispatch_id, path)
    except (ValueError, OSError):
        return None
    if row.get('state') in TERMINAL or not isinstance(row.get('launched_at'), str) \
            or not provider_failed(terminal, row['launched_at']):
        return None
    try:
        provider_retry.record(row['bundle'], terminal, dispatch_id=dispatch_id)
    except OSError:
        pass  # Best effort: provider_retry.ingest records it from the watcher's receipt on the next roll.
    try:
        return provider_retry.dispatch_scope(dispatch_id)
    except OSError:
        return None


def later(first, second):
    """True when ISO stamp `first` is after `second` (a stamp or a datetime); False when either is missing or malformed."""
    try:
        if isinstance(second, str):
            second = parse(second)
        return isinstance(first, str) and isinstance(second, datetime) and parse(first) > second
    except ValueError:
        return False


def agent_resumed(snapshot, row, terminal=None):
    """True when `row` is a failed attempt whose bound host agent is running a new turn (or finished one sent after it
    failed). A stuck provider retry whose cancel failed never stopped, so its running status is still the failed turn."""
    if row.get('state') != 'failed' or not row.get('agent_id') or not isinstance(snapshot, dict):
        return False
    if snapshot.get('id') != row['agent_id']:
        return False
    if (row.get('failure_detail') or {}).get('cancelled') is False or (terminal or {}).get('cancelled') is False:
        return False
    if snapshot.get('archivedAt') or snapshot.get('providerUnavailable'):
        return False
    status = snapshot.get('status')
    if status in ('running', 'initializing'):     # Paseo keeps the failed turn's attention flag until it is viewed
        return True
    if snapshot.get('attentionReason') == 'error' and not later(snapshot.get('lastUserMessageAt'),
                                                                  snapshot.get('attentionTimestamp')):
        return False                                # the follow-up turn, if any, also errored
    if status == 'idle' and (snapshot.get('attentionReason') == 'permission'
                             or (snapshot.get('pendingPermissionCount') or 0) > 0):
        return True
    last_message = snapshot.get('lastUserMessageAt')
    released_at = row.get('released_at')
    if status == 'idle' and isinstance(last_message, str) and isinstance(released_at, str):
        try:
            ended = parse(released_at)
            if isinstance((terminal or {}).get('at'), str):
                ended = max(ended, parse(terminal['at']))
            return parse(last_message) > ended
        except ValueError:
            return False
    return False


def observe(dispatch_id, snapshot, now=None, path=None, terminal=None):
    """Accept only the bound host agent; start/terminal receipts are idempotent.

    `terminal` is the notifier's first terminal sighting of this dispatch, when it has one."""
    now = now or utcnow()
    import provider_retry
    scope = classify_provider_failure(dispatch_id, terminal, path)
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        agent_id = snapshot.get('id')
        if row['state'] in TERMINAL and row['state'] != 'expired' and agent_resumed(snapshot, row, terminal):
            root = row if chain_root(row) == dispatch_id else get(connection, 'dispatches', chain_root(row))
            fallback = dict(root.get('fallback') or {})
            was_resumed = (fallback.get('state') == 'final'
                           and (fallback.get('outcome') or {}).get('result') == 'resumed'
                           and fallback.get('current') == row['dispatch_id']
                           and not any(isinstance(v, dict) and v.get('action') in ('scheduled', 'pending')
                                       for v in (fallback.get('handled') or {}).values()))
            if not was_resumed and retire_scheduled(fallback, now, 'resumed', RESUMED_NEXT,
                                                    root['dispatch_id'], current=row['dispatch_id']):
                root['fallback'] = fallback
                root.setdefault('events', []).append({
                    'state': root['state'], 'at': stamp(now),
                    'reason': f'pending fallback retired: parent resumed {row["dispatch_id"]}'})
                save(connection, root)
                if root['dispatch_id'] == dispatch_id:
                    row = root
            return row
        labels = snapshot.get('labels') or {}
        if any(labels.get(k) != row['request']['labels'][k] for k in LABELS):
            raise NotBound('Host snapshot belongs to another dispatch/parent/task')
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
        # A completed worker can be resumed; its later quota failure still matters.
        if snapshot.get('status') == 'error':
            updated_error = snapshot.get('updatedAt')
            if isinstance(updated_error, str) and parse(updated_error) >= parse(row['launched_at']):
                import account_state
                import quota_exhaustion
                try:
                    quota_exhaustion.record(row['bundle']['account'], snapshot.get('lastError'),
                                            windows=bundle_windows(row['bundle']), at=updated_error, source=agent_id)
                except OSError:
                    pass  # Best effort: a busy store must not stop this observation or the reconcile that made it.
                try:
                    account_state.record_launch_error(row['bundle']['account'], snapshot.get('lastError'),
                                                      code=snapshot.get('lastErrorClass'), at=updated_error, source=agent_id)
                except OSError:
                    pass
        if row['state'] in TERMINAL and row['state'] != 'expired':
            return row
        if row['state'] == 'prepared':
            raise ValueError('Agent observed without a launch claim')
        updated = snapshot.get('updatedAt')
        if not isinstance(updated, str) or parse(updated) < parse(row['launched_at']):
            raise ValueError('Host snapshot predates this launch')
        row['agent_id'] = agent_id
        row['observed_at'] = stamp(now)
        if isinstance(snapshot.get('cwd'), str):
            row['cwd'] = snapshot['cwd']
        status = snapshot.get('status')
        reason = snapshot.get('attentionReason')
        # A resumed worker carries this dispatch's labels before its turn starts, so its idle/finished state may
        # still be the previous turn's. Only a seen run or a user message after this launch shows the new turn.
        last_message = snapshot.get('lastUserMessageAt')
        try:
            delivered = isinstance(last_message, str) and parse(last_message) >= parse(row['launched_at'])
        except ValueError:
            delivered = False
        undelivered = row.get('operation') == 'resume' and not row.get('started_at') and not delivered and status != 'running'
        if undelivered:
            reason = None
        target = None
        stuck = provider_failed(terminal, row['launched_at'])
        if stuck:
            # The provider failed this turn (or the bridge stopped it retrying): whatever the host says after, it failed.
            target = 'failed'
            # A failed turn the classifier read as the worker's own error (or this task's input) is not the provider's.
            row['failure_code'] = provider_retry.outcome_code(terminal['failure_code'], scope)
            row['failure_detail'] = {k: terminal[k] for k in ('partial_work', 'cancelled') if k in terminal}
        elif finished_before(terminal, row['launched_at']) and status != 'running':
            target = 'completed'
        elif status == 'error' or reason == 'error' or snapshot.get('providerUnavailable'):
            target = 'failed'
        elif status == 'closed' or snapshot.get('archivedAt'):
            # Paseo closes a finished worker once it collects the idle runtime; only an unfinished one was canceled.
            target = 'completed' if reason == 'finished' else 'canceled'
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
        # The same state read again is no sign of life, so only a turn that landed renews or recovers a resume's lease.
        # Without this every observation restored it, and release could never end one whose turn never came.
        if target in ACTIVE and undelivered and (row['state'] == 'expired' or now >= parse(row['lease_until'])):
            target = 'expired'
        if target in ACTIVE:
            row.pop('released_at', None)
            if not undelivered:
                row['lease_until'] = stamp(min(now + timedelta(seconds=RUN_LEASE), parse(row['hard_until'])))
        prev_state = row['state']
        later_dispatch_ids = set()
        if target == 'completed' and agent_id:
            ref_stamp = row.get('launched_at') or row.get('created_at')
            for other in rows(connection, 'dispatches'):
                if other['dispatch_id'] != dispatch_id and other.get('agent_id') == agent_id:
                    other_stamp = other.get('launched_at') or other.get('created_at')
                    if ref_stamp and later(other_stamp, ref_stamp):
                        later_dispatch_ids.add(other['dispatch_id'])
        if row['state'] != target:
            event(row, target, now)
        save(connection, row)
    if target == 'launched' and not row.get('started_at') and row.get('launch_commit') is None \
            and isinstance(row.get('cwd'), str) and row['cwd'].strip():
        import dispatch_ground_truth
        launch_git = dispatch_ground_truth.capture_launch_git(row['cwd'])
        with transaction(path) as connection:
            latest = get(connection, 'dispatches', dispatch_id)
            if latest.get('launch_commit') is None and not latest.get('started_at'):
                latest.update(launch_git)
                save(connection, latest)
                row = latest
    if target == 'completed' and (prev_state != 'completed' or 'ground_truth_recorded_at' not in row):
        import dispatch_ground_truth
        gt = dispatch_ground_truth.collect_ground_truth(
            row,
            idle_threshold=RUN_LEASE,
            recorded_at=stamp(now),
            agent_doc=paseo_agent_record(agent_id),
            other_dispatch_ids=later_dispatch_ids,
        )
        with transaction(path) as connection:
            latest = get(connection, 'dispatches', dispatch_id)
            latest.update(gt)
            save(connection, latest)
            row = latest
    # Only a turn positively seen ending on the model's reply lifts a hold: a cancelled probe reads as finished too.
    if target == 'completed' and provider_replied(terminal, row.get('launched_at')):
        try:
            provider_retry.clear(row['bundle'].get('account'), provider_retry.launch_model(row['bundle']),
                                 include_account=True, launched_at=row.get('launched_at') or row.get('created_at'),
                                 bundle=row['bundle'])
        except OSError:
            pass
    if target in ('started', 'completed'):
        import account_state
        import launch_failures
        try:
            launch_failures.clear(row['bundle'].get('account'))
        except OSError:
            pass  # Best effort, and this observation is already saved.
        try:
            account_state.clear_latch(row['bundle'].get('account'), launched_at=row.get('launched_at') or row.get('created_at'))
        except OSError:
            pass
        import model_latch
        try:
            model_latch.clear(row['bundle'].get('account'), model_latch.launch_model(row['bundle']),
                              launched_at=row.get('launched_at') or row.get('created_at'))
        except OSError:
            pass
    return row


def release(dispatch_id, parent_id, reason, now=None, path=None, failed=False):
    """Explicit cancellation/known pre-send failure, not an invented terminal host receipt. The parent's cancellation
    (not `failed`) of a brief AgentCompass is waiting to retry also cancels that retry, in the same transaction
    (retire_scheduled); the returned row then carries `canceled_retry`, the failed attempt whose watcher to stop."""
    now = now or utcnow()
    with transaction(path) as connection:
        row = get(connection, 'dispatches', dispatch_id)
        if parent_id != row['parent_id']:
            raise ValueError('Dispatch belongs to another parent')
        if row['state'] in ('launching', 'launched', 'started') and not failed:
            raise ValueError('Observe terminal status before releasing a launched dispatch; release does not cancel an agent')
        if row['state'] == 'expired' and (row.get('creation') or {}).get('in_flight') and not failed:
            raise ValueError('Paseo is still creating this worker and cannot cancel a create; release does not stop it')
        canceled = None
        if not failed:
            root = row if chain_root(row) == dispatch_id else get(connection, 'dispatches', chain_root(row))
            fallback = dict(root.get('fallback') or {})
            canceled = retire_scheduled(fallback, now, 'canceled', RETRY_CANCELED_NEXT, root['dispatch_id'])
            if canceled:
                # The failed row stays as it ended; only its attempt record and history change.
                root['fallback'] = fallback
                root.setdefault('events', []).append({'state': root['state'], 'at': stamp(now),
                                                      'reason': 'scheduled retry canceled: ' + reason[:200]})
                save(connection, root)
                if root['dispatch_id'] == dispatch_id:
                    row = root
        if row['state'] not in TERMINAL or row['state'] == 'expired':
            event(row, 'failed' if failed else 'canceled', now, reason)
            save(connection, row)
        return dict(row, canceled_retry=canceled) if canceled else row


def pending(path=None):
    if not Path(path or db_path()).exists():
        return []
    with transaction(path) as connection:
        # Include expired started/uncertain Paseo launches so a later observation can repair them.
        return [r for r in rows(connection, 'dispatches')
                if r.get('dispatch_mode', 'paseo') == 'paseo' and unresolved(r, utcnow())]


def record_host_claim(surface, decision, task, bundle, *, label=None, usage=None, access='write',
                      workspace=None, cwd=None, launched=False, now=None, path=None):
    """Record a non-Paseo host dispatch (`orca`, `t3`, `native`, `herdr`, `cli`) in `dispatches` under the same
    canonical-directory writer rule and SQLite BEGIN IMMEDIATE transaction as Paseo."""
    parent_id = task.get('parent_agent_id')
    require_parent_id(parent_id)
    now = now or utcnow()
    canon_ws = canonical_dir(workspace)
    landing = canon_ws or cwd
    decision_id = decision.get('decision_id') or f'{surface}-{uuid.uuid4()}'
    bundle_copy = dict(bundle or {})
    bundle_copy.setdefault('bundle_id', bundle_copy.get('id') or decision.get('choice') or f'{surface}:unknown')
    raw_usd = (decision.get('expected_usd') if isinstance(decision.get('expected_usd'), (int, float))
               else bundle_copy.get('expected_usd'))
    expected_usd = float(raw_usd) if isinstance(raw_usd, (int, float)) and not isinstance(raw_usd, bool) else None
    lease_secs = RUN_LEASE if launched else PREPARE_LEASE
    state = 'launched' if launched else 'prepared'
    with transaction(path) as connection:
        expire(connection, now)
        by_dec = connection.execute('SELECT data FROM dispatches WHERE decision_id=?', (decision_id,)).fetchone()
        if by_dec:
            existing_row = json.loads(by_dec[0])
            if existing_row['state'] in ACTIVE:
                return existing_row
            decision_id = f'{surface}:{decision_id}:{uuid.uuid4().hex[:8]}'
        if canon_ws and access != 'read_only':
            holders = writers_in(connection, canon_ws, now, parent_id)
            if holders:
                raise WorkspaceBusy(canon_ws, holders)
        dispatch_id = str(uuid.uuid4())
        row = {
            'dispatch_id': dispatch_id,
            'decision_id': decision_id,
            'dispatch_mode': surface,
            'parent_id': parent_id,
            'task_id': task.get('task_id'),
            'request': {**({'title': label} if label else {}), **({'cwd': landing} if landing else {})},
            'bundle': bundle_copy,
            'expected_usd': expected_usd,
            'usage': (usage or {}).get('accounts', {}),
            'created_at': stamp(now),
            **({'launched_at': stamp(now)} if launched else {}),
            'lease_until': stamp(now + timedelta(seconds=lease_secs)),
            'hard_until': stamp(now + timedelta(seconds=MAX_LIFETIME)),
            'operation': 'create',
            'agent_id': None,
            'access': access,
            **({'workspace': canon_ws} if canon_ws else {}),
            **({'cwd': landing} if landing else {}),
            **({'profile': task['profile']} if isinstance(task.get('profile'), dict) else {}),
            **({'brief_path': task['brief_path']} if task.get('brief_path') else {}),
        }
        event(row, state, now)
        save(connection, row)
        return row


def settle_host_claim(dispatch_id, target_state='canceled', note=None, *, now=None, path=None):
    """Settle a non-Paseo host claim (`completed`, `failed`, or `canceled`) when its adapter observes completion or failure."""
    if not dispatch_id or target_state not in TERMINAL or not Path(path or db_path()).exists():
        return None
    now = now or utcnow()
    with transaction(path) as connection:
        found = connection.execute('SELECT data FROM dispatches WHERE id=?', (dispatch_id,)).fetchone()
        if not found:
            return None
        row = json.loads(found[0])
        if row['state'] not in TERMINAL:
            event(row, target_state, now, note)
            save(connection, row)
        return row


def record_external_launch(dispatch_mode, external_id, decision_id, bundle, *, parent_id=None, task_id=None,
                           profile=None, brief_path=None, cwd=None, completed=False, now=None, path=None):
    """Persist an external non-Paseo runner dispatch (`grok-bot`, `dot`) in `dispatches` without holding Paseo budget."""
    if not external_id or not decision_id:
        return None
    import dispatch_ground_truth
    now = now or utcnow()
    dispatch_id = f'{dispatch_mode}:{external_id}'
    launch_git = dispatch_ground_truth.capture_launch_git(cwd, external_runner=True)
    with transaction(path) as connection:
        existing = connection.execute('SELECT data FROM dispatches WHERE id=?', (dispatch_id,)).fetchone()
        if existing:
            row = json.loads(existing[0])
        else:
            by_dec = connection.execute('SELECT data FROM dispatches WHERE decision_id=?', (decision_id,)).fetchone()
            if by_dec:
                return json.loads(by_dec[0])
            row = {
                'dispatch_id': dispatch_id,
                'decision_id': decision_id,
                'dispatch_mode': dispatch_mode,
                'external_task_id': external_id,
                'parent_id': parent_id,
                'task_id': task_id,
                'bundle': dict(bundle or {}),
                'created_at': stamp(now),
                'launched_at': stamp(now),
                'lease_until': stamp(now),
                'hard_until': stamp(now + timedelta(seconds=MAX_LIFETIME)),
                'operation': 'create',
                'agent_id': None,
                'access': 'write' if cwd else 'read_only',
                **({'cwd': cwd} if cwd else {}),
                **launch_git,
                **({'profile': profile} if isinstance(profile, dict) else {}),
                **({'brief_path': brief_path} if brief_path else {}),
            }
            event(row, 'external_launched', now)
            save(connection, row)
    if completed:
        return complete_external(dispatch_mode, external_id, now=now, path=path)
    return row


def complete_external(dispatch_mode, external_id, *, now=None, path=None):
    """Mark a previously recorded external runner dispatch `completed` and persist its ground-truth fields."""
    target_db = Path(path or db_path())
    if not external_id or not target_db.exists():
        return None
    dispatch_id = f'{dispatch_mode}:{external_id}'
    with transaction(target_db) as connection:
        found = connection.execute('SELECT data FROM dispatches WHERE id=?', (dispatch_id,)).fetchone()
        if not found:
            return None
        row = json.loads(found[0])
    if row.get('state') == 'completed' and 'ground_truth_recorded_at' in row:
        return row
    import dispatch_ground_truth
    now = now or utcnow()
    gt = dispatch_ground_truth.collect_ground_truth(
        row,
        idle_threshold=RUN_LEASE,
        recorded_at=stamp(now),
        agent_doc=None,
    )
    with transaction(target_db) as connection:
        latest = get(connection, 'dispatches', dispatch_id)
        if latest.get('state') != 'completed':
            event(latest, 'completed', now)
        latest.update(gt)
        save(connection, latest)
        return latest
