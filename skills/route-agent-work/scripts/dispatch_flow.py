"""Executable local Paseo dispatch and automatic, metadata-only reconciliation."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import dispatch_registry as registry
from worker_context import require_parent_id

def paseo_config():
    """The local Paseo config ($PASEO_HOME/config.json); empty when absent or unreadable."""
    try:
        config = json.loads((Path(os.environ.get('PASEO_HOME') or Path.home() / '.paseo') / 'config.json')
                            .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def paseo_reachable(timeout=1.0):
    """The local daemon accepts connections at config daemon.listen, as the bridge dials it."""
    listen = (paseo_config().get('daemon') or {}).get('listen')
    listen = listen.strip() if isinstance(listen, str) and listen.strip() else '127.0.0.1:6767'
    address, _, port = listen.rpartition(':')
    try:
        with socket.create_connection((address or '127.0.0.1', int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def host_call(payload):
    bridge = Path(os.environ.get('ROUTER_PASEO_BRIDGE') or
                  Path(__file__).resolve().parents[3] / 'setup' / 'paseo_dispatch_bridge.mjs')
    try:
        result = subprocess.run(['node', str(bridge)], input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', timeout=25)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'sent': payload['operation'] == 'create', 'error': 'host_timeout'}
    except OSError:
        return {'ok': False, 'sent': False, 'error': 'bridge_unavailable'}
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError()
        return response
    except ValueError:
        return {'ok': False, 'sent': payload['operation'] == 'create', 'error': 'invalid_host_response'}


def public(row):
    keys = ('dispatch_id', 'decision_id', 'task_id', 'parent_id', 'agent_id', 'state', 'expected_usd',
            'created_at', 'launched_at', 'started_at', 'observed_at', 'lease_until', 'hard_until', 'released_at')
    return {k: row[k] for k in keys if k in row}


def require_actual_parent(parent_id):
    require_parent_id(parent_id)
    actual = os.environ.get('PASEO_AGENT_ID')
    if actual and actual != parent_id:
        raise ValueError('Dispatch parent differs from actual PASEO_AGENT_ID; another agent cannot claim this assignment')


def reconcile(dispatch_id=None, host=None, now=None, path=None):
    host = host or host_call
    now = now or registry.utcnow()
    targets = [registry.inspect(dispatch_id, path)] if dispatch_id else registry.pending(path)
    results = []
    for row in targets:
        if not row.get('launched_at') or row['state'] in ('completed', 'failed', 'canceled'):
            continue
        response = host({'operation': 'observe', 'dispatch_id': row['dispatch_id'], 'agent_id': row.get('agent_id')})
        if response.get('ok') and response.get('agent'):
            try:
                row = registry.observe(row['dispatch_id'], response['agent'], now=now, path=path)
                results.append(public(row))
            except ValueError as exc:
                results.append({'dispatch_id': row['dispatch_id'], 'error': str(exc)})
        elif response.get('ok') and response.get('missing') and row.get('agent_id'):
            results.append(public(registry.missing_agent(row['dispatch_id'], row['agent_id'], now=now, path=path)))
        else:
            results.append({'dispatch_id': row['dispatch_id'], 'error': response.get('error') or 'agent_not_observed'})
    registry.reservations(now, path)  # Expire unacknowledged prepared/launch leases as well.
    return results


def reservation_records(read_only=False):
    # Called automatically by plan/record and before prepare. No host query when no active launches exist.
    if not read_only:
        reconcile()
    return registry.reservations(read_only=read_only)


def launch(dispatch_id, parent_id, host=None, watch_seconds=3600, poll_seconds=5, path=None):
    require_actual_parent(parent_id)
    host = host or host_call
    row = registry.inspect(dispatch_id, path)
    if row['parent_id'] != parent_id:
        raise ValueError('Dispatch belongs to another parent')
    if row['state'] == 'prepared':
        row = registry.claim(row['request'], parent_id, now=registry.utcnow(), path=path)
        response = host({'operation': 'create', 'dispatch_id': dispatch_id,
                         'parent_id': parent_id, 'request': row['request']})
        if response.get('ok') and response.get('agent'):
            row = registry.observe(dispatch_id, response['agent'], path=path)
        elif response.get('sent') is False:
            row = registry.release(dispatch_id, parent_id, 'host_rejected_before_create', failed=True, path=path)
        # Ambiguous errors retain the single-use claim; reconcile locates any created agent by labels.
    elif row['state'] == 'expired' and row.get('launched_at'):
        reconcile(dispatch_id, host=host, path=path)
        row = registry.inspect(dispatch_id, path)
    deadline = time.monotonic() + watch_seconds
    while row['state'] not in registry.TERMINAL:
        reconcile(dispatch_id, host=host, path=path)
        row = registry.inspect(dispatch_id, path)
        if row['state'] in registry.TERMINAL or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
    result = public(row)
    if row['state'] not in registry.TERMINAL:
        result['continuation'] = 'Run dispatch --dispatch-action reconcile or launch with this dispatch_id; it will observe, not create again.'
        result['continuation_argv'] = [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('route_judge.py')),
            'launch', '--dispatch-id', dispatch_id, '--parent-id', parent_id, '--watch-seconds', '120']
    return result


def cli(args):
    if args.stage == 'launch':
        if not args.dispatch_id or not args.parent_id:
            raise ValueError('launch requires --dispatch-id and --parent-id')
        if not 0 <= args.watch_seconds <= 86400 or not 1 <= args.poll_seconds <= 60:
            raise ValueError('watch-seconds must be 0..86400 and poll-seconds 1..60')
        return launch(args.dispatch_id, args.parent_id, watch_seconds=args.watch_seconds, poll_seconds=args.poll_seconds)
    action = args.dispatch_action
    if action == 'prepare':
        if not args.input:
            raise ValueError('dispatch prepare requires --input {decision_id, task, launch}')
        document = json.loads(args.input.read_text(encoding='utf-8-sig'))
        require_actual_parent(document['task'].get('parent_agent_id'))
        import route_judge
        reconcile()
        row = registry.prepare(document['decision_id'], document['task'], document['launch'],
                               route_judge.usage_snapshot(args.usage_cache))
        return {**public(row), 'create_agent': row['request']}
    if action == 'start' and not args.dispatch_id:
        raise ValueError('start acknowledgment requires --dispatch-id; it observes the host rather than trusting caller status')
    if action in ('reconcile', 'start'):
        return {'observations': reconcile(args.dispatch_id), 'active_reservations': registry.reservations()}
    if not args.dispatch_id:
        raise ValueError('status/release requires --dispatch-id')
    if action == 'release':
        if not args.parent_id or not args.basis:
            raise ValueError('release requires --parent-id and --basis explaining cancellation')
        require_actual_parent(args.parent_id)
        return public(registry.release(args.dispatch_id, args.parent_id, args.basis[:400]))
    return public(registry.inspect(args.dispatch_id))
