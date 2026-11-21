"""Executable local Paseo dispatch and automatic, metadata-only reconciliation."""
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

from datetime import timedelta

import caller_input
import dispatch_fallback
import public_data
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

# Why `reconcile` did not observe a dispatch it was asked about, one entry per branch that skips it. `NOTHING_OPEN` is
# the answer of a `reconcile` that had no open Paseo dispatch to look at (`registry.pending` lists Paseo rows only);
# `OPEN_ELSEWHERE` then names the open claims of other hosts (`registry.open_host_claims`), which it does not observe.
NOT_OBSERVED = {
    'not_paseo': 'not a Paseo dispatch ({mode})',
    'terminal': 'already {state}',
    'never_launched': '{state}, never launched',
    'not_applied': 'observation not applied',
    'unanswered': 'could not be observed',
}
NOTHING_OPEN = 'No Paseo dispatch is open, so reconcile observed nothing.'
OPEN_ELSEWHERE = 'Open on other hosts, which reconcile does not observe: {claims}.'
# What `settle` found when the receipt proves no ending for this row, so the state did not change: no receipt, one that
# could not be read, a file that was read and is not a receipt, one with no ending yet, or one whose ending does not
# count for this row (dated before this launch, or a reason registry.state_from_receipt does not read).
SETTLE_NO_RECEIPT = {
    'missing': 'No receipt of the watcher was found for this dispatch, so its state did not change.',
    'unreadable': 'The receipt of the watcher for this dispatch could not be read, so its state did not change.',
    'not_a_receipt': 'The file of the watcher for this dispatch was read and is not a receipt, so its state did not change.',
    'no_ending': 'The receipt of the watcher for this dispatch records no ending, so its state did not change.',
    'ending_not_counted': ('The receipt of the watcher for this dispatch records an ending that does not count for it '
                           '(dated before this launch, or a reason AgentCompass does not read), so its state did not change.'),
}
RELEASED_NOTHING = 'Dispatch is already {state}; nothing was released.'
OBSERVED_OVER = 'Observing it settled the dispatch as {state}; nothing was left to release.'

UNCONFIRMED_NEXT = ('Paseo did not confirm this create ({what}), so it is not known whether the worker exists or will '
                    'appear. Run next_argv: it finds the worker by its labels if it was created, and settles the dispatch '
                    'if it was not. Do not roll again before that: a second roll can start a duplicate.')
RESUME_UNCONFIRMED_NEXT = ('Paseo did not confirm this resume ({what}), so it is not known whether the worker took the turn. '
                           'Run next_argv: it observes the worker and settles the dispatch. Do not roll again before that: a '
                           'second roll can send the turn twice.')
UNCONFIRMED_WHAT = {
    'host_timeout': 'the host did not answer in time',
    'invalid_host_response': 'the bridge printed an answer that could not be read',
    'create_outcome_uncertain_reconcile_by_label': 'the bridge reports that the outcome of the create is uncertain',
    'resume_outcome_uncertain_reconcile_by_agent': 'the bridge reports that the outcome of the resume is uncertain',
    'host_request_failed': 'the request failed after the bridge had connected to the daemon',
}
# What the bridge's `condition` tokens mean: what failed, and what the caller does about that cause. The bridge sends a
# token of a fixed vocabulary beside the error code it merges several states under; create and resume read this one
# table, and what follows from it (roll again, run next_argv) is said by the answer that reads it. The vocabulary per code
# is the bridge's (`CONDITIONS` in setup/paseo_dispatch_bridge.mjs); a test reads it from there, so every token the
# bridge can send for `host_connection_failed` (the daemon was never connected), `host_request_failed` (sent) and
# `host_resume_failed` (not sent) has a row here and no row is left that the bridge never sends. The tokens of
# `worker_not_idle` and `worker_not_found` are states of dispatch_registry.WORKER_STATES.
HOST_CONDITIONS = {
    'refused': ('the daemon refused the connection (nothing listens on its port)',
                'start the daemon (check it with `paseo ls`)'),
    'timeout': ('the daemon did not answer in time',
                'check that the daemon is running and answering (`paseo ls`)'),
    'unreachable': ('the daemon could not be reached at its address',
                    "check that it is running and that `daemon.listen` in config.json under PASEO_HOME (default ~/.paseo) "
                    'is its address (`paseo ls`)'),
    'authentication': ("the daemon is running but did not accept the bridge's local credential",
                       'check that PASEO_HOME is the home of the daemon that is running (its `local-credential` file '
                       'is the credential)'),
    'credential_unreadable': ("the bridge could not read Paseo's local credential file",
                              'check that `local-credential` exists and is readable under PASEO_HOME (default ~/.paseo), '
                              'where the daemon writes it when it first starts'),
    'rejected': ('the daemon accepted the connection and closed the handshake',
                 'read the daemon log (it is running, so starting it is not the way out) and check whether it refuses '
                 'this client or differs in version from it (update Paseo)'),
    'connection_lost': ('the connection to the daemon dropped during the request',
                        'check that the daemon is still running (`paseo ls`)'),
    'daemon_error': ('the daemon answered the request with an error',
                     'check that the daemon is current and read its log'),
    'invalid_response': ("the daemon's answer could not be read",
                         'update Paseo (the daemon and the bridge SDK may be of different versions)'),
    'bridge_error': ("the bridge's own code failed before it sent the request, so nothing of it reached the daemon",
                     'run `agentcompass doctor` and, if it repeats, tell the user'),
}
# A launch Paseo never received, whose cause is not a condition: what failed and the step that fixes it. Nothing exists to
# collect from, and nothing was started in its place.
NOT_SENT_NEXT = {
    'sdk_setup_failed': 'The Paseo bridge could not load its SDK, so nothing was sent. {fix} Then roll the brief again.',
    'bridge_unavailable': ('The bridge process could not be started (Node.js is missing from PATH or the bridge file is '
                           'gone), so nothing was sent. Install Node.js or run `agentcompass setup`, then roll the brief again.'),
    'create_send_window_elapsed': ('The create request was not sent within its send window (the host was too slow), so '
                                   'nothing was launched. Roll the brief again.'),
    'parent_agent_not_found': ('Paseo does not know the parent agent this roll names, so nothing was launched. Check '
                               'PASEO_AGENT_ID or --parent-id against `paseo ls`, then roll the brief again.'),
}
NOT_SENT_OTHER = 'Paseo refused the launch before it created a worker ({code}), so nothing was sent.'
_TOKEN = re.compile(r'[a-z_]{1,48}')


def failure_cause(row, error=None):
    """The cause of a dispatch row that ended in failure, in a fixed vocabulary only (the bridge's class and the provider's
    own code, never its text); None for any other row. `error` is the code the caller already holds for it."""
    if row['state'] == 'failed' and row.get('failure_code'):
        return f"{error or 'worker_failed'}:{row['failure_code']}"
    if row['state'] == 'create_failed':
        return ':'.join(str(p) for p in (error or 'host_creation_failed', row.get('failure_class'), row.get('failure_code')) if p)
    return None


def failure_label(code, condition=None):
    """The `cause` of an unconfirmed launch: the code, then the bridge's condition token, bounded."""
    return (str(code)[:120] + (f':{condition}' if condition else ''))[:160]


def host_condition(response):
    """The bridge's `condition` for the code it answered, as a short token; None for anything else (never free text)."""
    condition = response.get('condition') if isinstance(response, dict) else None
    return condition if isinstance(condition, str) and _TOKEN.fullmatch(condition) else None


def condition_facts(condition):
    """`(what failed, what to do)` for a condition token of the bridge; None for no condition. A token this table does not
    hold is printed as the token, with advice that presumes no cause."""
    if not condition:
        return None
    if condition in HOST_CONDITIONS:
        return HOST_CONDITIONS[condition]
    if condition in registry.WORKER_STATES:
        known = registry.WORKER_STATES[condition]
        return known.reason, known.fix
    return f'the bridge reported the condition {condition}', 'check the daemon with `paseo ls` and read its log'


def _sentence(text):
    return text[:1].upper() + text[1:]


def not_sent_next(code, condition=None, setup_hint=None):
    """What a caller reads when the host refused a launch before any worker existed."""
    if code in NOT_SENT_NEXT:
        hint = setup_hint.strip()[:300] if isinstance(setup_hint, str) and setup_hint.strip() else 'Run `agentcompass setup`.'
        if not hint.endswith(('.', '!', '?')):
            hint += '.'
        return NOT_SENT_NEXT[code].format(fix=hint)
    facts = condition_facts(condition)
    if facts is None:
        head = {'host_connection_failed': 'The Paseo daemon could not be reached, so nothing was sent.'
                }.get(code) or NOT_SENT_OTHER.format(code=code)
        return f'{head} Check the daemon with `paseo ls`, then roll the brief again.'
    what, fix = facts
    return f'{_sentence(what)}, so nothing was sent. {_sentence(fix)}. Then roll the brief again.'


def resume_refusal_next(code, condition=None):
    """The way out of a refusal of `--resume <id>` by the host, from the condition when it sent one."""
    if condition in registry.WORKER_STATES:
        return registry.resume_way_out(condition)
    facts = condition_facts(condition)
    if facts is not None:
        what, fix = facts
        return f'{_sentence(what)}, so the worker was not asked. {_sentence(fix)}. Then roll --resume again with the same id.'
    return RESUME_CODE_NEXT.get(code) or (f'The host refused it ({code}): read its state with get_agent_status, then roll '
                                          '--resume again with the same id, or roll without --resume.')


# What a refusal of `--resume <id>` means by its code alone, for a bridge that sent no condition (the states a code merges
# are named by dispatch_registry.WORKER_STATES, which owns what to do about each).
RESUME_CODE_NEXT = {
    'worker_not_idle': ('The worker is not idle (running, starting, in error, waiting for a permission, or its provider is '
                        'unavailable): read its state with get_agent_status, deal with that, then roll --resume again.'),
    'worker_not_found': ('Paseo does not have the worker (absent or archived): check the id with `paseo ls`, unarchive it in '
                         'Paseo if it is archived, then roll --resume again.'),
    'host_connection_failed': ('The Paseo daemon could not be reached, so the worker was not asked: check it with `paseo ls`, '
                               'then roll --resume again with the same id.'),
    **{code: ('The worker is not the one this turn describes: roll without --resume to let the router choose, or name a '
              'route that matches the worker.')
       for code in ('worker_settings_mismatch', 'worker_model_mismatch', 'worker_parent_mismatch')},
}


def unconfirmed_what(code, condition=None):
    """What a caller reads in the parentheses of an unconfirmed create or resume: the code, and the condition's facts."""
    head = UNCONFIRMED_WHAT.get(code) or f'it answered {str(code)[:160]}'
    facts = condition_facts(condition)
    return head + (f': {facts[0]}' if facts else '')


LAUNCH_OPERATIONS = ('create', 'resume')     # bridge operations that start or continue a worker (effects)


def host_call(payload):
    """One bridge round trip, bounded by payload timeout_seconds. Files rather than pipes: a killed bridge cannot
    keep the caller waiting on a stream some descendant still holds open."""
    bridge = Path(os.environ.get('ROUTER_PASEO_BRIDGE') or
                  Path(__file__).resolve().parents[3] / 'setup' / 'paseo_dispatch_bridge.mjs')
    timeout = payload.get('timeout_seconds') or (RESUME_TIMEOUT if payload.get('operation') == 'resume' else HOST_TIMEOUT)
    if payload.get('operation') in LAUNCH_OPERATIONS:
        public_data.effect(f"paseo {payload['operation']}")    # marked before the bridge starts: never repeated
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
    if row.get('operation') == 'resume' and (row.get('observed_at') or row['state'] not in registry.TERMINAL):
        # Ended without the host ever showing the worker on this turn (the resume was refused): not a resume.
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


def reconcile(dispatch_id=None, host=None, now=None, path=None, timeout=None, not_observed=None):
    """Observe `dispatch_id`, or every open Paseo dispatch, and return the observations. The returned list holds only
    what the host was asked and answered (launch reads `bool(observations)` as "the host was queried").

    `not_observed`, a list the `reconcile`/`start` actions pass, receives each dispatch this call was asked about and
    did not observe, with why (`NOT_OBSERVED`): the branch below that skipped it is the fact. An observation the
    registry did not apply, or a failed attempt the host did not answer for, is there with its error class or the
    bridge's error code, never a host's text."""
    host = host or host_call
    now = now or registry.utcnow()
    targets = [registry.inspect(dispatch_id, path)] if dispatch_id else registry.pending(path)
    results = []
    skipped = not_observed if not_observed is not None else []

    def skip(row, why, **detail):
        skipped.append({'dispatch_id': row['dispatch_id'], 'state': row['state'], 'why': why, **detail})
    for row in targets:
        mode = row.get('dispatch_mode', 'paseo')
        if mode != 'paseo':
            skip(row, NOT_OBSERVED['not_paseo'].format(mode=mode))
            continue
        if row['state'] in registry.TERMINAL and row['state'] != 'expired':
            pending_chain = registry.fallback_guard(row, now)
            candidates = (failed_attempts(row, path) if pending_chain
                          else [row] if dispatch_id and row['state'] == 'failed'
                          and row.get('launched_at') and row.get('agent_id') else [])
            if not candidates:
                skip(row, NOT_OBSERVED['terminal'].format(state=row['state']))
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
                    except (registry.NotBound, ValueError) as exc:
                        # The row keeps its terminal; the answer says this observation was not applied.
                        skip(target, NOT_OBSERVED['not_applied'], error=type(exc).__name__)
                else:
                    skip(target, NOT_OBSERVED['unanswered'], error=response.get('error') or 'agent_not_observed')
            continue
        if not row.get('launched_at'):
            skip(row, NOT_OBSERVED['never_launched'].format(state=row['state']))
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
    one the watcher uses, so a race with a late watcher is one message, and names the id the notice ended on, which the
    row records. A send, a receipt that already shows the terminal was delivered (`evidence` `delivered`), an
    intentional skip (`evidence` `skipped`), and a parent that can no longer be reached are recorded. `already` without
    that evidence is not a delivery: the notice stays due. When Paseo answered that an earlier send's outcome is
    unknown, the bridge looks for the notice in the parent's timeline: found is `catchup_found`, not found and sent
    once under the next derived id `catchup_resent`; a receipt the watcher resolved that way is `watcher_found` or
    `watcher_resent`. A next id that is not due yet (`notice_resend_wait`) and a watcher still running the attempt's
    fallback (`fallback_pending`) leave the notice due. A notice whose ids are used up without Paseo confirming a send
    or the timeline showing it is recorded `watcher_undelivered` / `catchup_undelivered`: nothing more is sent, and the
    parent's own next router call names it (`name_undelivered`). A notice whose worker the same parent continued under a
    later dispatch, and one whose brief may still be retried or relaunched, are decided before this
    (`dispatch_registry.due_end_notices`). A second call sends nothing."""
    if host is None and 'daemon' not in paseo_config():
        return []
    sender = host or host_call
    sent = []
    for notice in registry.due_end_notices(path, now=now):
        dispatch_id = notice['dispatch_id']
        try:
            response = sender({'operation': 'notify_end', 'dispatch_id': dispatch_id,
                               'parent_id': notice.get('parent_id'), 'ended_as': notice['ended_as']})
        except Exception:
            continue
        if not isinstance(response, dict) or not response.get('ok'):
            continue
        used = response.get('message_id')
        if response.get('error') == 'parent_unavailable':
            registry.mark_end_notice_sent(dispatch_id, 'parent_unavailable', path=path)
        elif response.get('error') == 'notice_undelivered':
            # the catch-up gave up itself, or read a receipt the watcher gave up
            source = 'catchup_undelivered' if response.get('outcome') == 'undelivered' else 'watcher_undelivered'
            registry.mark_end_notice_sent(dispatch_id, source, path=path, message_id=used)
        elif response.get('sent'):
            source = 'catchup_resent' if response.get('outcome') == 'resent' else 'catchup'
            registry.mark_end_notice_sent(dispatch_id, source, path=path, message_id=used)
            sent.append(dispatch_id)
        elif response.get('already') and response.get('evidence') == 'delivered':
            resolved = response.get('delivery_outcome')
            source = ('catchup_found' if response.get('outcome') == 'found'
                      else 'watcher_' + resolved if resolved in ('found', 'resent') else 'watcher')
            registry.mark_end_notice_sent(dispatch_id, source, path=path, message_id=used)
        elif response.get('already') and response.get('evidence') == 'skipped':
            source = response.get('skipped')
            if not isinstance(source, str) or not source.isidentifier() or len(source) > 64:
                source = 'skipped'
            registry.mark_end_notice_sent(dispatch_id, source, path=path)
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
    launch_condition = None     # the bridge's condition for launch_error (host_condition), when it sent one
    setup_hint = None
    create_unconfirmed = None   # the error of a create or resume the host may still have received
    unconfirmed_condition = None  # the bridge's condition for it, when it sent one
    resume_refused = False      # the host refused to resume the worker the caller named
    refusal_detail = None   # what the host found different on a named worker it would not resume
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
            refused_resume = response.get('sent') is False and response.get('error') not in (
                'addressable_parent_required', 'parent_agent_not_found', 'notification_start_failed')
            if refused_resume and registry.named_resume(row, path):
                # `--resume <id>` named this worker: its context is what the caller asked for, and a new worker has
                # none of it. Nothing is created in its place, on any account; the caller decides what comes next.
                row = registry.release(dispatch_id, parent_id, 'resume_refused:' + str(response.get('error') or 'unknown'),
                                       failed=True, path=path)
                launch_error = response.get('error') or 'resume_refused'
                launch_condition = host_condition(response)
                resume_refused = True
                if isinstance(response.get('detail'), dict):
                    refusal_detail = {k: response['detail'].get(k) for k in ('worker', 'requested')}
                response = {'sent': None}
            elif refused_resume:
                # An automatic resume: the plan chose the bundle, so a fresh worker on it carries the brief.
                switched = registry.fallback_to_create(dispatch_id, row['claim_id'], now=registry.utcnow(), path=path)
                if switched is None:
                    # The claim is no longer this roll's (another step settled the row): nothing is created, and the
                    # answer is the row as it is.
                    row = registry.inspect(dispatch_id, path)
                    response = {'sent': None}
                else:
                    row = switched
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
            launch_condition = host_condition(response)
            setup_hint = response.get('setupHint')
            if launch_error == 'host_creation_failed':
                row = settle_create(row, {'phase': 'failed', 'failure_class': response.get('failure_class') or 'unknown',
                                          **({'failure_code': response['failure_code']}
                                             if isinstance(response.get('failure_code'), str) else {})}, None, path)
            else:
                cause = ':'.join(str(p) for p in (launch_error, response.get('failure_class')) if p)
                row = registry.release(dispatch_id, parent_id,
                                       'host_rejected_before_create' + (':' + cause if cause else ''), failed=True, path=path)
        elif response.get('sent') and not response.get('ok') and isinstance(response.get('error'), str):
            create_unconfirmed = response['error']
            unconfirmed_condition = host_condition(response)
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
        if launch_condition:
            result['refusal_condition'] = launch_condition
        if refusal_detail:
            result['refusal_detail'] = refusal_detail
        if row['state'] != 'create_failed':
            result['next_action'] = (resume_refusal_next(launch_error, launch_condition) if resume_refused
                                     else not_sent_next(launch_error, launch_condition, setup_hint))
    elif row['state'] == 'create_failed':
        result['error'] = 'create_failed'
    elif row['state'] == 'failed' and row.get('agent_id'):
        result['error'] = 'worker_failed'
    if failure_cause(row, result.get('error')):
        result['cause'] = failure_cause(row, result.get('error'))
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
        unconfirmed = create_unconfirmed or (observe_error if observe_error != 'agent_not_observed' else None)
        if unconfirmed and row.get('operation') == 'resume' and not result.get('next_action'):
            # A resume the host may have received: the worker exists, and what is unknown is whether it took the turn.
            result['cause'] = failure_label(unconfirmed, unconfirmed_condition)
            result['next_action'] = RESUME_UNCONFIRMED_NEXT.format(what=unconfirmed_what(unconfirmed, unconfirmed_condition))
        elif (unconfirmed and result.get('creation_phase') == 'unknown'
                and result.get('next_action') in (CREATING_NEXT, STALLED_NEXT)):
            # The host has not said it carries out this create (a phase it reports, `accepted` for one, makes it the creating
            # state and keeps the text above): what is known is the error, so say that.
            result['cause'] = failure_label(unconfirmed, unconfirmed_condition)
            result['next_action'] = UNCONFIRMED_NEXT.format(what=unconfirmed_what(unconfirmed, unconfirmed_condition))
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
    """(the receipt, None), or (None, why there is none: `missing`, `unreadable` or `not_a_receipt`)."""
    try:
        document = json.loads(notification_file(dispatch_id).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, ValueError):
        return None, 'unreadable'
    return (document, None) if isinstance(document, dict) else (None, 'not_a_receipt')


def settle(dispatch_id, path=None, now=None):
    """Write the watcher's ending into the registry without another host round trip. Idempotent.

    The receipt's reason is the ending. Its raw host status is not applied, so a superseded receipt whose status
    is the next round's `running` still closes this row. A receipt that is not an ending changes nothing, and the
    answer's `note` says which of `SETTLE_NO_RECEIPT` it was. `creation failed` with no agent settles through
    resolve_creation. A row that already has a terminal state keeps it; side effects the first writer skipped still
    run."""
    notification_file(dispatch_id)
    now = now or registry.utcnow()
    row = registry.inspect(dispatch_id, path)
    document, absent = read_notification(dispatch_id)
    receipt = document or {}
    terminal = receipt.get('terminal') if isinstance(receipt.get('terminal'), dict) else None
    reason = terminal.get('reason') if terminal else None
    creating_failed = reason == 'creation failed' or receipt.get('status') == 'creation_failed'
    if creating_failed and not row.get('agent_id') and row['state'] in ('launching', 'expired'):
        failure = terminal.get('failure_class') if terminal and isinstance(terminal.get('failure_class'), str) else 'unknown'
        code = terminal.get('failure_code') if terminal and isinstance(terminal.get('failure_code'), str) else None
        creation = {'phase': 'failed', 'failure_class': failure, **({'failure_code': code} if code else {})}
        settled, _ended = registry.resolve_creation(dispatch_id, creation, now=now, path=path)
        return end_notices(public(settled, now), now, path)
    # registry.state_from_receipt owns whether the receipt proves an ending for this row; without one nothing changes.
    no_ending = None if creating_failed or registry.state_from_receipt(row, receipt)[0] else (
        absent or ('ending_not_counted' if reason else 'no_ending'))
    if row['state'] in registry.TERMINAL and row['state'] != 'expired' and not row.get('side_effects_due'):
        # Still let a stored receipt finish effects the state writer skipped. finalize keeps the terminal.
        if reason is None:
            return end_notices(_noted(public(row, now), no_ending), now, path)
    settled = registry.finalize_dispatch(dispatch_id, now=now, path=path, receipt_only=True)
    return end_notices(_noted(public(settled, now), no_ending), now, path)


def _noted(answer, no_ending):
    return dict(answer, note=SETTLE_NO_RECEIPT[no_ending]) if no_ending else answer


def end_notices(answer, now=None, path=None):
    """Send the end notices that are due. One that fails stays due for the next read; the answer carries the error
    class in `end_notices_error`, never its text."""
    try:
        deliver_end_notices(now=now, path=path)
    except Exception as exc:
        answer['end_notices_error'] = type(exc).__name__
    return answer


UNDELIVERED_MEANING = ('AgentCompass could not deliver these workers\' end notices to you: Paseo left the outcome of each '
                       'send unknown and your timeline did not show the notice after three tries over more than an hour. '
                       'Check each worker with get_agent_status (agent_id) for how it ended and its final reply.')
# The router calls a parent makes and reads the answer of. A watcher's `settle` and `fallback` run with its parent's
# environment and nobody reads them; the tray's `reconcile` has no parent.
NAMING_STAGES = ('roll', 'plan', 'record', 'launch')
NAMING_ACTIONS = ('reconcile', 'start', 'status', 'release', 'prepare')


def name_undelivered(answer, args, path=None):
    """Name, in the answer of a router call the parent makes itself, the end notices of its dispatches that were given up
    as undelivered (`dispatch_registry.name_undelivered`): that call is the first sign the parent takes turns again.
    The parent is the actual Paseo agent (`PASEO_AGENT_ID`), never a caller-given id, so no notice is ever named to
    another parent; each is named once."""
    parent = os.environ.get('PASEO_AGENT_ID', '').strip()
    if (not parent or not isinstance(answer, dict) or getattr(args, 'dry_run', False)
            or (args.stage not in NAMING_STAGES
                and not (args.stage == 'dispatch' and getattr(args, 'dispatch_action', None) in NAMING_ACTIONS))):
        return answer
    try:
        named = registry.name_undelivered(parent, path=path)
    except (OSError, ValueError, sqlite3.Error):
        return answer
    if named:
        answer['undelivered_end_notices'] = named
        answer['undelivered_end_notices_meaning'] = UNDELIVERED_MEANING
    return answer


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
        document = json.loads(caller_input.read_text(args, args.input))
        require_actual_parent(document['task'].get('parent_agent_id'))
        import usage_reading
        reconcile()
        row = registry.prepare(document['decision_id'], document['task'], document['launch'],
                               usage_reading.usage_snapshot(args.usage_cache))
        return {**public(row), 'create_agent': row['request']}
    if action == 'start' and not args.dispatch_id:
        raise ValueError('start acknowledgment requires --dispatch-id; it observes the host rather than trusting caller status')
    if action in ('reconcile', 'start'):
        not_observed = []
        observations = reconcile(args.dispatch_id, not_observed=not_observed)
        answer = {'observations': observations, 'not_observed': not_observed, 'active_reservations': registry.reservations()}
        if not args.dispatch_id and not observations and not not_observed:
            elsewhere = ', '.join(f"{r['dispatch_id']} ({r['dispatch_mode']})" for r in registry.open_host_claims())
            answer['note'] = NOTHING_OPEN + (' ' + OPEN_ELSEWHERE.format(claims=elsewhere) if elsewhere else '')
        return end_notices(answer)
    if not args.dispatch_id:
        raise ValueError('status/release requires --dispatch-id')
    if action == 'release':
        if not args.parent_id or not args.basis:
            raise ValueError('release requires --parent-id and --basis explaining cancellation')
        require_actual_parent(args.parent_id)
        before = registry.inspect(args.dispatch_id)['state']
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
            return dict(public(released), canceled_retry=released['canceled_retry'], released=True)
        if released['state'] != row['state']:
            return dict(public(released), released=True)
        # registry.release changes nothing on a row that is already over: the answer names its state.
        over = RELEASED_NOTHING if before == row['state'] else OBSERVED_OVER
        return dict(public(released), released=False, note=over.format(state=row['state']))
    return public(registry.inspect(args.dispatch_id))
