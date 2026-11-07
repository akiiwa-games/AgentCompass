"""Executable local Paseo dispatch and automatic, metadata-only reconciliation."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from datetime import timedelta

import dispatch_registry as registry
import entry
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


HOST_TIMEOUT = 25
# Paseo restores an idle worker from persistence before a resumed turn starts; that took ~25 s on Antigravity.
RESUME_TIMEOUT = 90
# A bridge round trip takes ~3 s here. A watch with less than a second left still gets one observation this long,
# so a zero watch reads the host, and a watch with less than this left starts no further one.
OBSERVE_FLOOR = 5

CREATING_NEXT = ('Paseo is still creating this worker. Until it exists get_agent_status answers "not found"; '
                 'that is not a failure. Do not roll again: run next_argv to observe this same dispatch.')
STALLED_NEXT = ('Paseo still reports this create as running past its deadline and has no cancel, so the worker can still '
                'appear. Keep observing with next_argv; restarting the Paseo daemon settles it. Do not roll again.')
CREATE_FAILED_NEXT = ('Paseo confirms it created no worker for this dispatch and none can appear later. '
                      'It is safe to roll this brief again.')
REMOVED_NEXT = ('Paseo created this worker and it was removed before this dispatch observed it. The dispatch is over and '
                'holds nothing. The worker may have done part of the work: check that before rolling this brief again.')


def host_call(payload):
    """One bridge round trip, bounded by payload timeout_seconds. Files rather than pipes: a killed bridge cannot
    keep the caller waiting on a stream some descendant still holds open."""
    bridge = Path(os.environ.get('ROUTER_PASEO_BRIDGE') or
                  Path(__file__).resolve().parents[3] / 'setup' / 'paseo_dispatch_bridge.mjs')
    timeout = payload.get('timeout_seconds') or (RESUME_TIMEOUT if payload.get('operation') == 'resume' else HOST_TIMEOUT)
    try:
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout:
            stdin.write(json.dumps(payload).encode('utf-8'))
            stdin.seek(0)
            process = subprocess.Popen(['node', str(bridge)], stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL)
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                return {'ok': False, 'sent': payload['operation'] in ('create', 'resume'), 'error': 'host_timeout'}
            stdout.seek(0)
            output = stdout.read().decode('utf-8', 'replace')
    except OSError:
        return {'ok': False, 'sent': False, 'error': 'bridge_unavailable'}
    try:
        response = json.loads(output)
        if not isinstance(response, dict):
            raise ValueError()
        return response
    except ValueError:
        return {'ok': False, 'sent': payload['operation'] in ('create', 'resume'), 'error': 'invalid_host_response'}


def public(row, now=None):
    keys = ('dispatch_id', 'decision_id', 'task_id', 'parent_id', 'agent_id', 'state', 'expected_usd',
            'created_at', 'launched_at', 'started_at', 'observed_at', 'lease_until', 'hard_until', 'released_at', 'access')
    out = {k: row[k] for k in keys if k in row}
    if row.get('operation') == 'resume':
        out['resumed'] = True
    if row['state'] == 'create_failed':
        out['next_action'] = CREATE_FAILED_NEXT
    elif row['state'] == 'canceled' and (row.get('creation') or {}).get('removed'):
        out['next_action'] = REMOVED_NEXT
    elif creating(row):
        # Nothing here reads as a failure: the worker simply does not exist yet.
        out.update(creating=True, creation_phase=(row.get('creation') or {}).get('phase', 'unknown'),
                   create_deadline=registry.stamp(registry.create_deadline(row)))
        late = bool((row.get('creation') or {}).get('in_flight')) and (now or registry.utcnow()) >= registry.create_deadline(row)
        out['next_action'] = STALLED_NEXT if late else CREATING_NEXT
    return out


def creating(row):
    return (row['state'] == 'launching' or (row['state'] == 'expired' and bool((row.get('creation') or {}).get('in_flight')))) \
        and row.get('operation') == 'create' and not row.get('agent_id') and bool(row.get('launched_at'))



def require_actual_parent(parent_id):
    require_parent_id(parent_id)
    actual = os.environ.get('PASEO_AGENT_ID')
    if actual and actual != parent_id:
        raise ValueError('Dispatch parent differs from actual PASEO_AGENT_ID; another agent cannot claim this assignment')


def settle_create(row, creation, now, path):
    settled, transitioned = registry.resolve_creation(row['dispatch_id'], creation, now=now, path=path)
    if transitioned and settled.get('failure_class'):
        import account_state
        import launch_failures
        try:
            launch_failures.record(settled['bundle'], settled['failure_class'])
            account_state.record_launch_error(settled['bundle']['account'], None, code=settled['failure_class'],
                                              source=settled['dispatch_id'])
        except OSError:
            pass  # A cooldown is best effort: a busy store must not stop this observation or the roll that made it.
    return settled


def reconcile(dispatch_id=None, host=None, now=None, path=None, timeout=None):
    host = host or host_call
    now = now or registry.utcnow()
    targets = [registry.inspect(dispatch_id, path)] if dispatch_id else registry.pending(path)
    results = []
    for row in targets:
        if not row.get('launched_at') or (row['state'] in registry.TERMINAL and row['state'] != 'expired'):
            continue
        response = host({'operation': 'observe', 'dispatch_id': row['dispatch_id'], 'agent_id': row.get('agent_id'),
                         **({'timeout_seconds': timeout} if timeout else {})})
        if response.get('ok') and response.get('agent'):
            try:
                row = registry.observe(row['dispatch_id'], response['agent'], now=now, path=path,
                                       terminal=(response.get('completion_notification') or {}).get('terminal'))
                results.append(dict(public(row), completion_notification=response.get('completion_notification')))
            except registry.NotBound as exc:
                results.append({'dispatch_id': row['dispatch_id'], 'error': str(exc), 'not_bound': True})
            except ValueError as exc:
                results.append({'dispatch_id': row['dispatch_id'], 'error': str(exc)})
        elif response.get('ok') and response.get('missing') and row.get('agent_id'):
            results.append(public(registry.missing_agent(row['dispatch_id'], row['agent_id'], now=now, path=path)))
        elif response.get('ok') and not row.get('agent_id'):
            if 'creation' in response:
                row = settle_create(row, response['creation'], now, path)
            results.append(dict(public(row, now), completion_notification=response.get('completion_notification')))
        else:
            results.append({'dispatch_id': row['dispatch_id'], 'error': response.get('error') or 'agent_not_observed'})
    registry.reservations(now, path)  # Expire unacknowledged prepared/launch leases as well.
    return results


def workspace_holders(workspace, parent_id, host=None, path=None):
    """Writers in a workspace: open write dispatches, and router-launched agents running there whatever their last
    dispatch says (a worker can be sent a follow-up after it completed). The caller itself is not its own conflict.
    Returns (holders, error, the workspace's directory if Paseo gave one); a host that cannot be read leaves the recorded
    dispatches as the whole answer."""
    host = host or host_call
    holders = registry.workspace_writers(workspace, caller=parent_id, path=path)
    response = host({'operation': 'workspace_agents', 'workspace_id': workspace})
    if not response.get('ok'):
        return holders, response.get('error') or 'host_unavailable', None
    known = {h['dispatch_id'] for h in holders}
    for agent in response.get('agents') or ():
        dispatch_id = agent.get('dispatch_id')
        if agent.get('id') == parent_id or dispatch_id in known:
            continue
        try:
            row = registry.inspect(dispatch_id, path)
        except ValueError:
            row = None
        if row and row.get('access') == 'read_only':
            continue
        holders.append({'dispatch_id': dispatch_id, 'state': (row or {}).get('state'), 'agent_id': agent.get('id'),
                        'task_id': (row or {}).get('task_id'), 'title': ((row or {}).get('request') or {}).get('title'),
                        'agent_status': agent.get('status')})
    return holders, None, response.get('cwd')


def reservation_records(read_only=False):
    # Called automatically by plan/record and before prepare. No host query when no active launches exist.
    if not read_only:
        reconcile()
    return registry.reservations(read_only=read_only)


def launch(dispatch_id, parent_id, host=None, watch_seconds=3600, poll_seconds=5, path=None):
    require_actual_parent(parent_id)
    host = host or host_call
    row = registry.inspect(dispatch_id, path)
    notification = None
    queried_host = False
    launch_error = None
    # watch_seconds is a hard limit on waiting: a bridge call is cut at what is left of it. Sending a create is not
    # waiting: it runs to HOST_TIMEOUT whatever the watch, and the daemon carries on when it is cut.
    deadline = time.monotonic() + watch_seconds
    observed = False

    def left():
        return deadline - time.monotonic()

    def budget():
        return min(HOST_TIMEOUT, left()) if left() >= 1 else OBSERVE_FLOOR

    if row['parent_id'] != parent_id:
        raise ValueError('Dispatch belongs to another parent')
    if row['state'] == 'prepared':
        row = registry.claim(row['request'], parent_id, now=registry.utcnow(), path=path)

        def create(window_from):
            send_by = min(window_from + timedelta(seconds=registry.CREATE_SEND_WINDOW), registry.create_deadline(row))
            return host({'operation': 'create', 'dispatch_id': dispatch_id, 'parent_id': parent_id, 'request': row['request'],
                         'send_by': registry.stamp(send_by), 'create_deadline': registry.stamp(registry.create_deadline(row))})
        if row.get('operation') == 'resume' and row.get('agent_id'):
            response = host({'operation': 'resume', 'dispatch_id': dispatch_id,
                             'parent_id': parent_id, 'agent_id': row['agent_id'], 'request': row['request']})
            if response.get('sent') is False and response.get('error') not in (
                'addressable_parent_required', 'parent_agent_not_found', 'notification_start_failed'
            ):
                row = registry.fallback_to_create(dispatch_id, now=registry.utcnow(), path=path)
                # The failed resume used up part of the window, so this gets a fresh one, still inside the deadline.
                response = create(registry.utcnow())
        else:
            # From the claim: a process paused before sending cannot stretch the window past the deadline.
            response = create(registry.parse(row['launched_at']))
        queried_host = True
        notification = response.get('completion_notification')
        if response.get('ok') and response.get('agent'):
            row = registry.observe(dispatch_id, response['agent'], path=path)
        elif response.get('sent') is False:
            launch_error = response.get('error')
            if launch_error == 'host_creation_failed':
                row = settle_create(row, {'phase': 'failed', 'failure_class': response.get('failure_class') or 'unknown'}, None, path)
            else:
                cause = ':'.join(str(p) for p in (launch_error, response.get('failure_class')) if p)
                row = registry.release(dispatch_id, parent_id,
                                       'host_rejected_before_create' + (':' + cause if cause else ''), failed=True, path=path)
        # Ambiguous errors retain the single-use claim; reconcile locates any created agent by labels.
    elif row['state'] == 'expired' and row.get('launched_at'):
        observations = reconcile(dispatch_id, host=host, path=path, timeout=budget())
        queried_host = observed = True
        notification = next((item['completion_notification'] for item in observations
                             if item.get('completion_notification')), notification)
        row = registry.inspect(dispatch_id, path)
    observe_error = None
    while row['state'] not in registry.TERMINAL and (not observed or left() >= OBSERVE_FLOOR):
        observations = reconcile(dispatch_id, host=host, path=path, timeout=budget())
        queried_host = observed = True
        notification = next((item['completion_notification'] for item in observations
                             if item.get('completion_notification')), notification)
        observe_error = next((item['error'] for item in observations if item.get('error')), None)
        row = registry.inspect(dispatch_id, path)
        remaining = left()
        if row['state'] in registry.TERMINAL or remaining < OBSERVE_FLOOR:
            break
        # Wake with at least the floor left, so the next observation is cut at what remains rather than given more.
        time.sleep(min(poll_seconds, remaining - OBSERVE_FLOOR))
    result = public(row)
    if launch_error:
        result['error'] = launch_error
    elif row['state'] == 'create_failed':
        result['error'] = 'create_failed'
    if not queried_host and row.get('launched_at') and row.get('agent_id'):
        # A terminal registry row can outlive a stopped notifier. Observation
        # recovers its durable receipt without reissuing the create request.
        observed = host({'operation': 'observe', 'dispatch_id': dispatch_id, 'agent_id': row['agent_id'],
                         'timeout_seconds': budget()})
        notification = observed.get('completion_notification')
    if notification:
        result['completion_notification'] = notification
    if row['state'] not in registry.TERMINAL:
        if observe_error:
            # The host could not be read; says nothing about the worker. A create the host has not finished is `creating`.
            result['observe_error'] = observe_error
        result['continuation'] = 'Run dispatch --dispatch-action reconcile or launch with this dispatch_id; it will observe, not create again.'
        result['continuation_argv'] = entry.argv('launch', '--dispatch-id', dispatch_id, '--parent-id', parent_id, '--watch-seconds', '120')
    return result


def stop_watcher(dispatch_id):
    """The marker the bridge's watcher stops at (stopWatcher in paseo_dispatch_bridge.mjs). Best effort: the watcher
    also ends at its watch limit, and what called this is already saved."""
    file = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'paseo-notifications' / f'{dispatch_id}.json'
    try:
        if file.exists():
            Path(f'{file}.creation-failed').write_text('released', encoding='utf-8')
    except OSError:
        pass


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
        # A create that is already over can be settled now, then released; one still running stays refused.
        observations = reconcile(args.dispatch_id)
        error = next((item['error'] for item in observations if item.get('error') and not item.get('not_bound')), None)
        row = registry.inspect(args.dispatch_id)
        if error and row.get('launched_at') and registry.unresolved(row, registry.utcnow()):
            raise ValueError(f'Paseo could not be read ({error}), so this worker or create may still be alive and '
                             'release cannot confirm it is over. Try again when Paseo answers.')
        if row['state'] in registry.ACTIVE and any(item.get('not_bound') for item in observations):
            raise ValueError("Paseo answers that this worker does not carry this dispatch's labels, so its turn may still "
                             f"be on its way. Release works once the lease lapses at {row['lease_until']}.")
        released = registry.release(args.dispatch_id, args.parent_id, args.basis[:400])
        if released['state'] == 'canceled' and row['state'] != 'canceled':
            stop_watcher(args.dispatch_id)
        return public(released)
    return public(registry.inspect(args.dispatch_id))
