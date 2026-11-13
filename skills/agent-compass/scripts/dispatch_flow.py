"""Executable local Paseo dispatch and automatic, metadata-only reconciliation."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time

from datetime import timedelta

import dispatch_fallback
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
# The covered failures (dispatch_fallback): AgentCompass retries or relaunches the brief itself, so these say so.
CREATE_FAILED_NEXT = ('Paseo confirms it created no worker for this attempt and none can appear later. AgentCompass '
                      'relaunches this brief on the next candidate by itself and notifies the parent once the dispatch '
                      'reaches a final state: do not roll it again.')
REQUEST_CREATE_FAILED_NEXT = ('Paseo refused to create this worker and none can appear later. The refusal was not '
                              'attributed to the model or account, so AgentCompass did not cool down or switch '
                              'candidates: check cause and your launch arguments before rolling again.')
MODEL_LATCHED_NEXT = (' The provider reported that model {model} does not exist on {account} ({code}), so routing excludes it '
                      'until {retry} and the relaunch picks another model. Tell the user it is unavailable.')
WORKER_FAILED_NEXT = ('Paseo created this worker and it ended in error, so it no longer runs this brief. AgentCompass '
                      'relaunches the brief on the next candidate (this model is left out for the rest of the dispatch) '
                      'and notifies the parent of the final outcome: do not roll it again.')
RETRY_STUCK_NEXT = ('Its provider kept retrying one request without progress, so AgentCompass stopped the turn and holds '
                    'that model (with backoff) or its whole account (until cleared with the tray clear button or '
                    '`agentcompass hold --clear`) out of dispatch; plan/roll warnings and the tray account list show the '
                    'provider message. ')
TURN_FAILED_NEXT = ('Its provider ended the turn with an error. AgentCompass had its classifier read the provider message: '
                    'when it means the provider refuses this model or account, that model or account is held out of '
                    'dispatch (plan/roll warnings and the tray account list show it; clear with the tray clear button or '
                    '`agentcompass hold --clear`). AgentCompass retries a transient error or relaunches the brief on the '
                    'next candidate by itself and notifies the parent of the final outcome: do not roll it again.')
WORKER_TURN_FAILED_NEXT = ('Paseo created this worker and its turn ended in error. AgentCompass\'s classifier read the '
                           'error as coming from the worker\'s own work, not from its provider, so nothing is held. Read '
                           'its error (get_agent_activity on agent_id) and check whether it did part of the work before '
                           'rolling this brief again.')
TASK_INPUT_NEXT = ('Paseo created this worker and its turn ended in error. AgentCompass\'s classifier read the error as '
                   'caused by this task\'s own input (for example a prompt too long for the model), not by the provider '
                   'or the model, so nothing is held and another model may refuse it too. Read its error '
                   '(get_agent_activity on agent_id) and change the brief before rolling it again.')
RETRY_STUCK_CLEAN = ('The worker did no work before it got stuck; AgentCompass relaunches the brief on another model by '
                     'itself: do not roll it again.')
RETRY_STUCK_PARTIAL = ('The worker had already done part of the work (it is stopped, not removed); AgentCompass relaunches '
                       'the brief on another model and tells the new worker to check the workspace first: do not roll it '
                       'again.')
# The same failures with automatic relaunch turned off (AGENTCOMPASS_ROLL_FALLBACK=off): the parent rolls again itself.
MANUAL_NEXT = {
    'create_failed': ('Paseo confirms it created no worker for this dispatch and none can appear later. '
                      'It is safe to roll this brief again.'),
    'model_latched': (' The provider reported that model {model} does not exist on {account} ({code}), so routing excludes '
                      'it until {retry}: rolling again picks another model. Tell the user it is unavailable.'),
    'worker_failed': ('Paseo created this worker and it ended in error, so nothing is running this brief. Read its error '
                      '(get_agent_activity on agent_id) and tell the user before rolling again: the router does not read '
                      'error text, so it may choose the same model again.'),
    'turn_failed': ('Its provider ended the turn with an error. AgentCompass had its classifier read the provider message: '
                    'when it means the provider refuses this model or account, that model or account is held out of '
                    'dispatch (plan/roll warnings and the tray account list show it; clear with the tray clear button or '
                    '`agentcompass hold --clear`). Read its error (get_agent_activity on agent_id) and check whether it '
                    'did part of the work before rolling this brief again.'),
    'retry_stuck_clean': 'The worker did no work before it got stuck: roll this brief again and the router picks another model.',
    'retry_stuck_partial': ('The worker had already done part of the work (it is stopped, not removed): check its activity '
                            '(get_agent_activity on agent_id) and its worktree before rolling this brief again.'),
}
FALLBACK_RUNNING_NEXT = ('AgentCompass is relaunching this brief itself ({where}); do not roll it again. Run next_argv to '
                         'observe the current attempt; the parent is notified once the dispatch reaches a final state.')
FALLBACK_SCHEDULED_NEXT = ('The last attempt hit a transient provider failure; AgentCompass retries the same model by itself '
                           'at {retry_at} (its backoff) and nothing runs this brief until then. Do not roll it again. Run '
                           'next_argv after that time to observe the retry; the parent is notified once the dispatch '
                           'reaches a final state.')
FALLBACK_RELAUNCHED_NEXT = ('This attempt failed and AgentCompass relaunched the brief as dispatch {current}, which next_argv '
                            'observes. Do not roll it again.')
RETRY_STUCK_RUNNING = ('Stopping the worker failed, so it may still be running and resume once the provider recovers: stop '
                       'it with cancel_agent on agent_id and check what it did before rolling this brief again.')
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
            process = subprocess.Popen(['node', str(bridge)], stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
                                       env=bridge_env(),
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
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


def bridge_env():
    """The bridge's environment. Its watcher (a detached child) hands a failed attempt to `agentcompass dispatch
    --dispatch-action fallback` before it tells the parent anything (paseo_dispatch_bridge.mjs runFallback), and
    settles the registry with `--dispatch-action settle` before every exit."""
    env = dict(os.environ)
    env['AGENTCOMPASS_CONTINUATION_ARGV'] = json.dumps(entry.argv('continuation-verdict'))
    env['AGENTCOMPASS_SETTLE_ARGV'] = json.dumps(entry.argv('dispatch', '--dispatch-action', 'settle'))
    if dispatch_fallback.enabled():
        env['AGENTCOMPASS_FALLBACK_ARGV'] = json.dumps(entry.argv('dispatch', '--dispatch-action', 'fallback'))
    else:
        env.pop('AGENTCOMPASS_FALLBACK_ARGV', None)
    return env


def public(row, now=None):
    keys = ('dispatch_id', 'decision_id', 'task_id', 'parent_id', 'agent_id', 'state', 'expected_usd',
            'created_at', 'launched_at', 'started_at', 'observed_at', 'lease_until', 'hard_until', 'released_at', 'access')
    out = {k: row[k] for k in keys if k in row}
    auto = dispatch_fallback.enabled()
    if row.get('operation') == 'resume':
        out['resumed'] = True
    if row['state'] == 'create_failed':
        import launch_failures
        candidate_scoped = bool(launch_failures.candidate_failure_class(row.get('failure_class'), row.get('failure_code')))
        out['next_action'] = (REQUEST_CREATE_FAILED_NEXT if not candidate_scoped
                              else CREATE_FAILED_NEXT if auto else MANUAL_NEXT['create_failed'])
        import model_latch
        if model_latch.means_unavailable(row.get('failure_code')):
            out['next_action'] += (MODEL_LATCHED_NEXT if auto else MANUAL_NEXT['model_latched']).format(
                model=model_latch.launch_model(row['bundle']), account=row['bundle'].get('account'),
                code=row['failure_code'], retry=model_latch.retry_text({'observed_at': row.get('updated_at')}))
    elif row['state'] == 'failed' and row.get('agent_id'):
        out['next_action'] = (retry_stuck_next(row) if row.get('failure_code') == 'provider_retry_stuck'
                              else (TURN_FAILED_NEXT if auto else MANUAL_NEXT['turn_failed'])
                              if row.get('failure_code') == 'provider_turn_failed'
                              else WORKER_TURN_FAILED_NEXT if row.get('failure_code') == 'worker_turn_failed'
                              else TASK_INPUT_NEXT if row.get('failure_code') == 'task_input_rejected'
                              else WORKER_FAILED_NEXT if auto else MANUAL_NEXT['worker_failed'])
    elif row['state'] == 'canceled' and (row.get('creation') or {}).get('removed'):
        out['next_action'] = REMOVED_NEXT
    elif creating(row):
        # Nothing here reads as a failure: the worker simply does not exist yet.
        out.update(creating=True, creation_phase=(row.get('creation') or {}).get('phase', 'unknown'),
                   create_deadline=registry.stamp(registry.create_deadline(row)))
        late = bool((row.get('creation') or {}).get('in_flight')) and (now or registry.utcnow()) >= registry.create_deadline(row)
        out['next_action'] = STALLED_NEXT if late else CREATING_NEXT
    if row.get('attempt_of'):
        out.update(root_dispatch_id=row['attempt_of'], attempt=row.get('attempt'))
    fallback = row.get('fallback') or {}
    if fallback:
        out = with_fallback(out, row, fallback, now)
    return out


def with_fallback(out, row, fallback, now=None):
    """What the first dispatch's attempt record says about this brief, over what this one row says."""
    out = dict(out, attempts=fallback.get('lines') or [], current_attempt=fallback.get('current'))
    outcome = fallback.get('outcome') or {}
    if fallback.get('state') == 'final' and outcome.get('result') != 'declined' and outcome.get('next_action'):
        out['next_action'] = outcome['next_action']
    elif fallback.get('state') == 'retrying' and fallback.get('retry_at') \
            and registry.fallback_guard({'fallback': fallback}, now or registry.utcnow()):
        out.update(retry_at=fallback['retry_at'],
                   next_action=FALLBACK_SCHEDULED_NEXT.format(retry_at=fallback['retry_at']))
    elif registry.fallback_guard({'fallback': fallback}, now or registry.utcnow()):
        out['next_action'] = FALLBACK_RUNNING_NEXT.format(where=(fallback.get('lines') or ['attempt 1'])[-1])
    elif fallback.get('state') == 'relaunched' and fallback.get('current') not in (None, row['dispatch_id']) \
            and row['state'] in registry.TERMINAL:
        out['next_action'] = FALLBACK_RELAUNCHED_NEXT.format(current=fallback['current'])
    return out


def public_chain(row, path=None, now=None):
    """public(row) with its brief's attempt record, for an attempt that is not the first dispatch."""
    out = public(row, now)
    if row.get('attempt_of') and not row.get('fallback'):
        try:
            root = registry.inspect(row['attempt_of'], path)
        except ValueError:
            return out
        if root.get('fallback'):
            out = with_fallback(out, row, root['fallback'], now)
    return out


def retry_stuck_next(row):
    detail = row.get('failure_detail') or {}
    if detail.get('cancelled') is False:
        return RETRY_STUCK_NEXT + RETRY_STUCK_RUNNING
    auto = dispatch_fallback.enabled()
    # An old receipt without the flag may have worked: say so rather than invite a blind re-roll.
    if detail.get('partial_work') is False:
        return RETRY_STUCK_NEXT + (RETRY_STUCK_CLEAN if auto else MANUAL_NEXT['retry_stuck_clean'])
    return RETRY_STUCK_NEXT + (RETRY_STUCK_PARTIAL if auto else MANUAL_NEXT['retry_stuck_partial'])


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
    if transitioned:
        import launch_failures
        effective_class = launch_failures.candidate_failure_class(settled.get('failure_class'), settled.get('failure_code'))
        if effective_class:
            import account_state
            import model_latch
            try:
                launch_failures.record(settled['bundle'], settled.get('failure_class'),
                                       failure_code=settled.get('failure_code'))
                account_state.record_launch_error(settled['bundle']['account'], None, code=effective_class,
                                                  source=settled['dispatch_id'])
            except OSError:
                pass  # A cooldown is best effort: a busy store must not stop this observation or the roll that made it.
            try:
                model_latch.record(settled['bundle'], settled.get('failure_code'), source=settled['dispatch_id'])
            except OSError:
                pass
            if model_latch.means_unavailable(settled.get('failure_code')):
                # The provider just refused this model: drop its cached live list so the next roll reads it afresh.
                import live_models
                try:
                    live_models.invalidate(live_models.bundle_provider(settled.get('bundle')))
                except OSError:
                    pass
    return settled


def failed_attempts(row, path=None):
    """Failed attempts of `row`'s brief that have a host agent a parent follow-up could have resumed."""
    out, seen = [], set()
    for dispatch_id in [row['dispatch_id'], *(a.get('dispatch_id') for a in (
            (row.get('fallback') or {}).get('attempts') or ()) if a.get('dispatch_id'))]:
        if dispatch_id in seen:
            continue
        seen.add(dispatch_id)
        try:
            target = row if dispatch_id == row['dispatch_id'] else registry.inspect(dispatch_id, path)
        except ValueError:
            continue
        if target.get('state') == 'failed' and target.get('launched_at') and target.get('agent_id'):
            out.append(target)
    return out


def reconcile(dispatch_id=None, host=None, now=None, path=None, timeout=None):
    host = host or host_call
    now = now or registry.utcnow()
    targets = [registry.inspect(dispatch_id, path)] if dispatch_id else registry.pending(path)
    results = []
    for row in targets:
        if row.get('dispatch_mode', 'paseo') != 'paseo':
            continue
        if row['state'] in registry.TERMINAL and row['state'] != 'expired':
            pending_chain = registry.fallback_guard(row, now)
            candidates = (failed_attempts(row, path) if pending_chain
                          else [row] if dispatch_id and row['state'] == 'failed'
                          and row.get('launched_at') and row.get('agent_id') else [])
            for target in candidates:
                response = host({'operation': 'observe', 'dispatch_id': target['dispatch_id'],
                                 'agent_id': target['agent_id'],
                                 **({'timeout_seconds': timeout} if timeout else {})})
                if response.get('ok') and response.get('agent'):
                    try:
                        updated = registry.observe(
                            target['dispatch_id'], response['agent'], now=now, path=path,
                            terminal=(response.get('completion_notification') or {}).get('terminal'))
                        results.append(dict(public(updated),
                                            completion_notification=response.get('completion_notification')))
                    except (registry.NotBound, ValueError):
                        pass
            continue
        if not row.get('launched_at'):
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


def paseo_placement_probe(directory, parent_id, host=None):
    """Query Paseo's adapter for whether canonical `directory` can be placed for `parent_id`."""
    host = host or host_call
    response = host({'operation': 'workspace_agents', 'cwd': directory, 'parent_id': parent_id})
    if not response.get('ok'):
        return None
    return {k: response[k] for k in ('placeable', 'error', 'enclosing_directories') if k in response} or None


def workspace_holders(directory, parent_id, host=None, path=None):
    """Writers in canonical `directory`: open write dispatches across all hosts, plus router-launched Paseo agents
    running in `directory` whatever their last dispatch says (a worker can be sent a follow-up after it completed).
    The caller itself is not its own conflict. Returns (holders, host_error, placement_probe); a host that cannot be
    read leaves the recorded dispatches as the whole answer."""
    host = host or host_call
    holders = registry.writers_at(directory, caller=parent_id, path=path)
    response = host({'operation': 'workspace_agents', 'cwd': directory, 'parent_id': parent_id})
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
    probe = {k: response[k] for k in ('placeable', 'error', 'enclosing_directories') if k in response} or None
    return holders, None, probe


def deliver_end_notices(now=None, path=None, host=None):
    """Send each due end notice once. Without an explicit daemon, and without an injected host, send nothing.

    A failed send leaves the notice due so the next read retries it. The bridge computes the message id, the same
    one the watcher uses, so a race with a late watcher is one message. A send, a receipt that already shows the
    terminal was delivered (`evidence` `delivered`), an intentional skip (`evidence` `skipped`), and a parent that
    can no longer be reached are recorded. `already` without that evidence is not a delivery: the notice stays due.
    A second call sends nothing."""
    del now
    if host is None and 'daemon' not in paseo_config():
        return []
    sender = host or host_call
    sent = []
    for notice in registry.due_end_notices(path):
        try:
            response = sender({'operation': 'notify_end', 'dispatch_id': notice['dispatch_id'],
                               'parent_id': notice.get('parent_id')})
        except Exception:
            continue
        if not isinstance(response, dict) or not response.get('ok'):
            continue
        if response.get('error') == 'parent_unavailable':
            registry.mark_end_notice_sent(notice['dispatch_id'], 'parent_unavailable', path=path)
        elif response.get('sent'):
            registry.mark_end_notice_sent(notice['dispatch_id'], 'catchup', path=path)
            sent.append(notice['dispatch_id'])
        elif response.get('already') and response.get('evidence') == 'delivered':
            registry.mark_end_notice_sent(notice['dispatch_id'], 'watcher', path=path)
        elif response.get('already') and response.get('evidence') == 'skipped':
            source = response.get('skipped')
            if not isinstance(source, str) or not source.isidentifier() or len(source) > 64:
                source = 'skipped'
            registry.mark_end_notice_sent(notice['dispatch_id'], source, path=path)
    return sent


def reservation_records(read_only=False):
    # Called automatically by plan/record and before prepare. No host query when no active launches exist.
    if not read_only:
        reconcile()
    records = registry.reservations(read_only=read_only)
    try:
        deliver_end_notices()
    except Exception:
        pass
    return records


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
    if row['state'] in registry.TERMINAL and row['state'] != 'expired':
        now = registry.utcnow()
        root_row = row if registry.chain_root(row) == dispatch_id else registry.inspect(registry.chain_root(row), path)
        if registry.fallback_guard(root_row, now) or row['state'] == 'failed':
            observations = reconcile(
                root_row['dispatch_id'] if registry.fallback_guard(root_row, now)
                else dispatch_id, host=host, path=path, timeout=budget())
            queried_host = bool(observations)
            notification = next((item['completion_notification'] for item in observations
                                 if item.get('completion_notification')), notification)
            row = registry.inspect(dispatch_id, path)
        if path is None:
            # A transient failure's same-bundle retry that fell due starts here too, not only from its watcher.
            if dispatch_fallback.resume_due(dispatch_id, host=host) is not None:
                row = registry.inspect(dispatch_id, path)   # the retry ran, or was retired: read what it left
        current = registry.current_attempt(dispatch_id, path)
        if current != dispatch_id:
            dispatch_id, row = current, registry.inspect(current, path)
            queried_host = False
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
                row = settle_create(row, {'phase': 'failed', 'failure_class': response.get('failure_class') or 'unknown',
                                          **({'failure_code': response['failure_code']}
                                             if isinstance(response.get('failure_code'), str) else {})}, None, path)
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
    result = public_chain(row, path)
    if launch_error:
        result['error'] = launch_error
    elif row['state'] == 'create_failed':
        result['error'] = 'create_failed'
    elif row['state'] == 'failed' and row.get('agent_id'):
        result['error'] = 'worker_failed'
    if row['state'] == 'failed' and row.get('failure_code'):
        result['cause'] = f"{result.get('error') or 'worker_failed'}:{row['failure_code']}"
    if row['state'] == 'create_failed':
        # Fixed vocabulary only: the bridge's class and the provider's own code, never its text.
        result['cause'] = ':'.join(str(p) for p in (result['error'], row.get('failure_class'), row.get('failure_code')) if p)
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


_DISPATCH_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')


def notification_file(dispatch_id):
    """The watcher's receipt. The id is checked before it becomes a path, so it cannot leave the notifications directory."""
    if not isinstance(dispatch_id, str) or _DISPATCH_ID.fullmatch(dispatch_id) is None:
        raise ValueError('invalid dispatch id')
    root = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass')
    return root / 'paseo-notifications' / f'{dispatch_id}.json'


def read_notification(dispatch_id):
    try:
        document = json.loads(notification_file(dispatch_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def settle(dispatch_id, path=None, now=None):
    """Write the watcher's ending into the registry without another host round trip. Idempotent.

    The receipt's reason is the ending. Its raw host status is not applied, so a superseded receipt whose status
    is the next round's `running` still closes this row. A receipt that is not an ending changes nothing.
    `creation failed` with no agent settles through resolve_creation. A row that already has a terminal state keeps
    it; side effects the first writer skipped still run."""
    notification_file(dispatch_id)
    now = now or registry.utcnow()
    row = registry.inspect(dispatch_id, path)
    receipt = read_notification(dispatch_id) or {}
    terminal = receipt.get('terminal') if isinstance(receipt.get('terminal'), dict) else None
    reason = terminal.get('reason') if terminal else None
    creating_failed = reason == 'creation failed' or receipt.get('status') == 'creation_failed'
    if creating_failed and not row.get('agent_id') and row['state'] in ('launching', 'expired'):
        failure = terminal.get('failure_class') if terminal and isinstance(terminal.get('failure_class'), str) else 'unknown'
        code = terminal.get('failure_code') if terminal and isinstance(terminal.get('failure_code'), str) else None
        creation = {'phase': 'failed', 'failure_class': failure, **({'failure_code': code} if code else {})}
        settled, _ended = registry.resolve_creation(dispatch_id, creation, now=now, path=path)
        try:
            deliver_end_notices(now=now, path=path)
        except Exception:
            pass
        return public(settled, now)
    if row['state'] in registry.TERMINAL and row['state'] != 'expired' and not row.get('side_effects_due'):
        # Still let a stored receipt finish effects the state writer skipped. finalize keeps the terminal.
        if reason is None:
            try:
                deliver_end_notices(now=now, path=path)
            except Exception:
                pass
            return public(row, now)
    settled = registry.finalize_dispatch(dispatch_id, now=now, path=path, receipt_only=True)
    try:
        deliver_end_notices(now=now, path=path)
    except Exception:
        pass
    return public(settled, now)


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
    if action == 'fallback':
        if not args.dispatch_id:
            raise ValueError('fallback requires --dispatch-id')
        return dispatch_fallback.handle(args.dispatch_id)
    if action == 'settle':
        if not args.dispatch_id:
            raise ValueError('settle requires --dispatch-id')
        return settle(args.dispatch_id)
    if action == 'prepare':
        if not args.input:
            raise ValueError('dispatch prepare requires --input {decision_id, task, launch}')
        document = json.loads(args.input.read_text(encoding='utf-8-sig'))
        require_actual_parent(document['task'].get('parent_agent_id'))
        import usage_reading
        reconcile()
        row = registry.prepare(document['decision_id'], document['task'], document['launch'],
                               usage_reading.usage_snapshot(args.usage_cache))
        return {**public(row), 'create_agent': row['request']}
    if action == 'start' and not args.dispatch_id:
        raise ValueError('start acknowledgment requires --dispatch-id; it observes the host rather than trusting caller status')
    if action in ('reconcile', 'start'):
        observations = reconcile(args.dispatch_id)
        records = registry.reservations()
        try:
            deliver_end_notices()
        except Exception:
            pass
        return {'observations': observations, 'active_reservations': records}
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
        if released.get('canceled_retry'):
            # The scheduled retry is canceled in the registry; its watcher stops waiting for it too.
            stop_watcher(released['canceled_retry'])
            return dict(public(released), canceled_retry=released['canceled_retry'])
        return public(released)
    return public(registry.inspect(args.dispatch_id))
