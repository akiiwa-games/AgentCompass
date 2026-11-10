"""Retry or relaunch a Paseo dispatch whose chosen model failed, so the parent hears only the final outcome.

Owner requirement (2026-11-10): "roll首选模型失败时，重试和自动选第二个模型都自己做好，不要让主会话自己重试。"

Contract:
- Covered failures of one attempt (`failure_of`): a create Paseo confirms failed (a provider INVALID_MODEL included;
  model_latch then holds that model) or a bundle refused at prepare/claim (registry.CapacityChanged: an exclusion that
  appeared since the plan, such as a live model list that dropped it, a launch floor or a disabled account); a worker that
  ended in error; a provider that kept retrying (`provider_retry_stuck`, only when the bridge's stop took); a turn the
  provider failed (`provider_turn_failed`, e.g. 429 or an exhausted quota). What a provider failure blocks is the
  System-One classifier's scope that provider_retry recorded for that dispatch (model, account, transient, unknown):
  this module reads the scope and nothing else, no message text, no model or provider names.
- Not covered, reported to the parent as before: scope `task_input` (the brief's own input) and `none` (the worker's own
  error), a stop that did not take (the worker may still be running), a removed or finished worker, a launch the host
  refused before creating anything for reasons of its own (parent, notifier), and every non-Paseo surface.
- Transient: the same bundle once more after its provider_retry backoff, when that wait fits the task's `can_wait_hours`
  (none given: nothing waits and the next candidate starts now). A model the owner named (`roll --model`, a
  `user_override` decision) is only ever retried this way, never switched.
- Otherwise: allocation runs again on the original decision's allocation task (its classifier profile, needs such as
  local_only, fast) with every bundle that failed in this dispatch marked unavailable; the classifier is not asked
  again. The winner is launched with the first dispatch's title, workspace, cwd, access, parent and the original prompt,
  prefixed by a note naming the failed attempts and asking the worker to check the workspace before continuing.
- One worker per brief: a relaunch is prepared only after the failed attempt is terminal and confirmed dead (create
  failed, or the worker ended in error, or the bridge's stop was confirmed), and between attempts the first dispatch
  carries a guard (registry.fallback_guard) so the parent or another observer cannot start the brief a second time.
  A failed attempt is claimed once (`handled`), so two observers never relaunch it twice.
- Bound: every failure puts its bundle in the dispatch's excluded set (a transient one after its single same-bundle
  retry), and allocation never returns an excluded bundle, so one dispatch makes at most (startable candidates +
  transient retries) attempts, and ends `blocked` with every attempt's cause once allocation has nothing left.
- Bookkeeping: each attempt is its own registry row with its own decision, lease, labels and holds, linked by
  `attempt_of` (the first dispatch, the id the parent holds) and `previous_attempt`; the first dispatch keeps
  `fallback` = {state, current, attempts, handled, outcome}. Each step writes a `stage: fallback` ledger line and each
  re-plan its own plan line. A failed row is terminal, so its lease is released before the next attempt takes one.
"""
import argparse
import json
import os
from pathlib import Path
import time
from datetime import timedelta

import dispatch_registry as registry

PROVIDER_FAILURES = ('provider_retry_stuck', 'provider_turn_failed')
TRANSIENT = 'transient'
NOT_COVERED_SCOPES = ('none', 'task_input')


def enabled():
    return os.environ.get('AGENTCOMPASS_ROLL_FALLBACK', '').strip().lower() != 'off'


def notification_dir():
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'paseo-notifications'


def failure_of(row):
    """What ended one attempt: {covered, kind ('switch' | 'transient' | None), label, scope}."""
    state, code = row.get('state'), row.get('failure_code')
    if state == 'create_failed':
        creation = row.get('creation') or {}
        cause = row.get('failure_class') or ('outcome_unknown' if creation.get('phase') == 'failed'
                                             else creation.get('phase') or 'unknown')
        return {'covered': True, 'kind': 'switch', 'scope': None,
                'label': ':'.join(str(p) for p in ('create_failed', cause, code) if p)}
    if state != 'failed' or not row.get('agent_id'):
        reason = next((e.get('reason') for e in reversed(row.get('events') or ()) if e.get('reason')), None)
        return {'covered': False, 'kind': None, 'scope': None, 'label': ':'.join(p for p in (state, reason) if p)}
    if code in PROVIDER_FAILURES:
        if (row.get('failure_detail') or {}).get('cancelled') is False:
            return {'covered': False, 'kind': None, 'scope': None, 'label': f'{code}:stop_failed'}
        import provider_retry
        scope = provider_retry.dispatch_scope(row['dispatch_id']) or 'unknown'
        if scope in NOT_COVERED_SCOPES:
            return {'covered': False, 'kind': None, 'scope': scope, 'label': f'{code} (classifier scope {scope})'}
        return {'covered': True, 'kind': TRANSIENT if scope == TRANSIENT else 'switch', 'scope': scope,
                'label': f'{code} (classifier scope {scope})'}
    if code == 'task_input_rejected':
        return {'covered': False, 'kind': None, 'scope': 'task_input', 'label': code}
    if code == 'worker_turn_failed':
        return {'covered': False, 'kind': None, 'scope': 'none', 'label': code}
    if code is None:
        return {'covered': True, 'kind': 'switch', 'scope': None, 'label': 'worker_error'}
    return {'covered': False, 'kind': None, 'scope': None, 'label': str(code)}


def refused(exc):
    return {'covered': True, 'kind': 'switch', 'scope': None, 'label': 'refused_before_launch',
            'detail': str(exc)[:300]}


def bundle_view(bundle):
    bundle = bundle or {}
    return {k: bundle.get(k) for k in ('bundle_id', 'provider', 'account', 'model', 'effort') if bundle.get(k)}


def hold_key(bundle):
    import provider_retry
    try:
        entry = provider_retry.entry_for(bundle, provider_retry.active())
    except OSError:
        return None
    return provider_retry.entry_key(entry) if entry else None


def line(attempt, following=None):
    head = f"attempt {attempt['attempt']}: {attempt.get('model')} on {attempt.get('account')}"
    if not attempt.get('failure'):
        return head + (' (current attempt)' if attempt.get('dispatch_id') else '')
    text = f"{head} failed: {attempt['failure']}"
    if following is None:
        return text
    if following.get('bundle_id') == attempt.get('bundle_id'):
        return text + '; retried the same model after its backoff'
    return text + f"; switched to {following.get('model')} on {following.get('account')}"


def lines(attempts):
    return [line(a, attempts[i + 1] if i + 1 < len(attempts) else None) for i, a in enumerate(attempts)]


def relaunch_prompt(base, attempts):
    failed = [f"- attempt {a['attempt']}: model {a.get('model')} on account {a.get('account')} failed with "
              f"{a.get('failure')}" for a in attempts if a.get('failure')]
    return ('AgentCompass note: earlier attempts at this brief stopped before finishing:\n' + '\n'.join(failed) +
            '\nThey may have left work in this workspace. Before you start, check its state (uncommitted changes, '
            'partial commits on the branch, files the brief names) and continue from it rather than starting over or '
            'discarding it.\n\n---\n\n' + base)


def write_attempts_file(dispatch_id, root_id, attempt_lines):
    """What the new attempt's watcher puts in the parent's notification (paseo_dispatch_bridge.mjs attemptsNote)."""
    folder = notification_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{dispatch_id}.json.attempts').write_bytes(
            json.dumps({'root_dispatch_id': root_id, 'lines': attempt_lines}, ensure_ascii=False).encode('utf-8'))
    except OSError:
        pass  # the notification then names only this attempt; the registry still has the whole chain


class AlreadyHandled(Exception):
    def __init__(self, outcome):
        super().__init__('already handled')
        self.outcome = outcome


class Chain:
    """The attempts of one brief, from its first dispatch (`root`) or, before any row exists, from its decision."""

    def __init__(self, decision, launch, *, root=None, access='write', allow_concurrent=None, cwd=None,
                 judge=None, host=None, sleep=time.sleep):
        if judge is None:
            import route_judge as judge
        self.judge, self.host, self.sleep = judge, host, sleep
        self.decision = decision
        self.owner_named = decision.get('constraint') == 'user_override'
        task = dict(decision.get('allocation_task') or decision['task'])
        task.pop('chain', None)   # a relaunch is a new worker, never a resume
        self.task = task
        self.parent = decision['parent_id']
        self.launch = launch
        self.access, self.allow_concurrent, self.cwd = access, allow_concurrent, cwd
        self.root = root
        fallback = (root or {}).get('fallback') or {}
        self.attempts = [dict(a) for a in fallback.get('attempts') or ()]
        if root is not None and not self.attempts:
            self.attempts = [self.entry(root['bundle'], root)]

    @classmethod
    def of(cls, root, **kwargs):
        request = root['request']
        base = root.get('fallback_prompt') or request['initialPrompt']
        launch = {'title': request['title'], 'initialPrompt': base,
                  **({'workspaceId': request['workspaceId']} if request.get('workspaceId') else {})}
        return cls(registry.decision_record(root['decision_id']), launch, root=root, access=root.get('access', 'write'),
                   allow_concurrent=root.get('allow_concurrent'), cwd=root.get('cwd'), **kwargs)

    @property
    def root_id(self):
        return self.root['dispatch_id'] if self.root else None

    def entry(self, bundle, row=None):
        return {'attempt': len(self.attempts) + 1, **bundle_view(bundle),
                **({'dispatch_id': row['dispatch_id'], 'decision_id': row['decision_id']} if row else {})}

    def failed(self, attempt, failure, row=None):
        attempt.update(failure=failure['label'], kind=failure['kind'], covered=failure['covered'],
                       **({'scope': failure['scope']} if failure.get('scope') else {}),
                       **({'detail': failure['detail']} if failure.get('detail') else {}),
                       **({'state': row['state'], 'agent_id': row.get('agent_id')} if row else {}))
        if failure['covered']:
            held = hold_key({k: attempt.get(k) for k in ('account', 'provider', 'model')}) if failure['scope'] else None
            if held:
                attempt['hold'] = held

    def excluded(self):
        """Bundles this dispatch must not start again: each covered failure's, a transient one once retried."""
        out = set()
        for a in self.attempts:
            if not a.get('covered') or not a.get('bundle_id'):
                continue
            if a.get('kind') != TRANSIENT or self.transient_tries(a['bundle_id']) > 1 or not self.can_retry(a):
                out.add(a['bundle_id'])
        return out

    def transient_tries(self, bundle_id):
        return sum(1 for a in self.attempts if a.get('bundle_id') == bundle_id and a.get('kind') == TRANSIENT)

    def can_retry(self, attempt):
        return self.owner_named or self.backoff(attempt) <= float(self.task.get('can_wait_hours') or 0) * 3600

    def backoff(self, attempt):
        """Seconds until provider_retry lets this model be tried again; its own first backoff when nothing is held."""
        import provider_retry
        try:
            entry = provider_retry.entry_for({k: attempt.get(k) for k in ('account', 'provider', 'model')},
                                             provider_retry.active())
        except OSError:
            entry = None
        if entry and isinstance(entry.get('retry_at'), str):
            return max(0.0, (registry.parse(entry['retry_at']) - registry.utcnow()).total_seconds())
        return float(provider_retry.retry_seconds(1))

    def ledger(self, action, attempt, **extra):
        try:
            self.judge.append_ledger({'v': 1, 'at': registry.stamp(registry.utcnow()), 'stage': 'fallback',
                                      'action': action, 'parent_id': self.parent, 'task_id': self.task.get('task_id'),
                                      'root_dispatch_id': self.root_id, 'attempt': attempt.get('attempt'),
                                      'failed': {k: attempt.get(k) for k in (
                                          'dispatch_id', 'bundle_id', 'provider', 'account', 'model', 'effort',
                                          'failure', 'scope', 'hold', 'detail') if attempt.get(k)}, **extra})
        except Exception:   # noqa: BLE001 - auditing never stops a relaunch
            pass

    def save(self, state, **fields):
        if not self.root:
            return None
        attempts = [dict(a) for a in self.attempts]

        def change(fallback):
            fallback.update(state=state, attempts=attempts, lines=lines(attempts), **fields)
            if state not in ('retrying', 'relaunching'):
                fallback.pop('until', None)
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        return self.root

    def claim(self, failed_id):
        """Only one observer handles a failed attempt; a second gets the first one's outcome."""
        claimed = {}

        def change(fallback):
            handled = dict(fallback.get('handled') or {})
            claimed['before'] = handled.get(failed_id)
            if not claimed['before']:
                handled[failed_id] = {'action': 'pending', 'at': registry.stamp(registry.utcnow())}
                fallback['handled'] = handled
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        before = claimed['before']
        if before and before.get('action') != 'pending':
            raise AlreadyHandled(before)
        if before:
            pending_at = before.get('at')
            if pending_at and registry.utcnow() - registry.parse(pending_at) < timedelta(seconds=registry.RUN_LEASE):
                raise AlreadyHandled({'action': 'pending', 'root_dispatch_id': self.root_id,
                                      'detail': 'another observer is relaunching this attempt'})

    def outcome(self, failed_id, outcome):
        if not self.root or not failed_id:
            return outcome

        def change(fallback):
            fallback['handled'] = {**(fallback.get('handled') or {}), failed_id: outcome}
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        return outcome

    # The loop.

    def run(self, failure, row=None, bundle=None):
        """From one failed attempt to the next live worker or a final state. `row` is the failed attempt's row (None
        when its bundle was refused before a row existed); `bundle` the refused bundle then."""
        failed_id = row['dispatch_id'] if row else None
        while True:
            attempt = next((a for a in self.attempts if row and a.get('dispatch_id') == row['dispatch_id']), None)
            if attempt is None:
                attempt = self.entry(bundle or row['bundle'], row)
                self.attempts.append(attempt)
            self.failed(attempt, failure, row)
            if not failure['covered']:
                self.ledger('declined', attempt)
                return self.finish('declined', failure, failed_id)
            choice = self.next_choice(attempt)
            if choice is None:
                kind = 'owner_named' if self.owner_named else 'blocked'
                self.ledger(kind, attempt, excluded=sorted(self.excluded()))
                return self.finish(kind, failure, failed_id)
            decision, how, wait = choice
            new_bundle = decision['selected_bundle']
            self.ledger(how, attempt, wait_seconds=round(wait) if wait else None,
                        next={'decision_id': decision['decision_id'], **bundle_view(new_bundle)})
            try:
                new = registry.prepare(decision['decision_id'], self.task, dict(
                    self.launch, initialPrompt=relaunch_prompt(self.launch['initialPrompt'], self.attempts)),
                    self.judge.usage_snapshot(None), access=self.access, allow_concurrent=self.allow_concurrent,
                    cwd=self.cwd, attempt={'attempt_of': self.root_id, 'attempt': len(self.attempts) + 1,
                                           'previous_attempt': (row or {}).get('dispatch_id')} if self.root else None)
            except registry.CapacityChanged as exc:
                row, bundle, failure = None, new_bundle, refused(exc)
                continue
            except ValueError as exc:   # a writer appeared in the workspace, or the decision cannot dispatch
                attempt_next = self.entry(new_bundle)
                self.attempts.append(attempt_next)
                self.failed(attempt_next, {'covered': False, 'kind': None, 'scope': None,
                                           'label': 'not_prepared', 'detail': str(exc)[:300]})
                self.ledger('declined', attempt_next)
                return self.finish('declined', {'label': 'not_prepared'}, failed_id)
            if self.root is None:
                # Refused before the first row existed: this attempt becomes the dispatch the parent holds.
                self.root = new
                self.attempts.append(self.entry(new_bundle, new))
                self.save('relaunching', current=new['dispatch_id'])
            else:
                self.attempts.append(self.entry(new_bundle, new))
                self.save('relaunching', current=new['dispatch_id'],
                          until=registry.stamp(registry.utcnow() + timedelta(seconds=registry.RUN_LEASE)))
            write_attempts_file(new['dispatch_id'], self.root_id, lines(self.attempts))
            import dispatch_flow
            try:
                receipt = dispatch_flow.launch(new['dispatch_id'], self.parent, host=self.host, watch_seconds=0)
            except registry.CapacityChanged as exc:   # refused at claim: nothing was sent
                registry.release(new['dispatch_id'], self.parent, 'refused_at_claim', failed=True)
                row, bundle, failure = registry.inspect(new['dispatch_id']), new_bundle, refused(exc)
                continue
            current = registry.inspect(new['dispatch_id'])
            if current['state'] in registry.TERMINAL and current['state'] != 'expired':
                row, bundle, failure = current, new_bundle, failure_of(current)
                continue
            self.save('relaunched', current=current['dispatch_id'])
            outcome = {'action': 'relaunched', 'dispatch_id': current['dispatch_id'], 'root_dispatch_id': self.root_id,
                       'agent_id': current.get('agent_id'), 'attempts': self.attempts, 'lines': lines(self.attempts),
                       'receipt': receipt}
            return self.outcome(failed_id, outcome)

    def next_choice(self, attempt):
        """(decision, 'retry_same' | 'switch', seconds waited) for the next attempt, or None when nothing may start."""
        if attempt.get('kind') == TRANSIENT and self.transient_tries(attempt['bundle_id']) == 1 and self.can_retry(attempt):
            wait = self.backoff(attempt)
            if wait > 0:
                self.save('retrying', current=attempt.get('dispatch_id'),
                          until=registry.stamp(registry.utcnow() + timedelta(seconds=wait + registry.RUN_LEASE)))
                self.sleep(wait)
            return self.same_bundle(attempt), 'retry_same', wait
        if self.owner_named:
            return None
        decision = self.replan()
        return (decision, 'switch', 0) if decision else None

    def same_bundle(self, attempt):
        # A transient failure is a provider's, so it always has the row whose bundle it was.
        bundle = dict(registry.inspect(attempt['dispatch_id'])['bundle'])
        result = {'stage': 'record' if self.owner_named else 'plan', 'choice': bundle.get('bundle_id'),
                  'selected_bundle': bundle, 'decision': {'action': 'start'},
                  **({'constraint': 'user_override',
                      'basis': f'AgentCompass transient retry of dispatch {self.root_id} on the owner-named model'}
                     if self.owner_named else {})}
        issued = registry.issue(result, self.task, allocation_task=self.task)
        return {**result, **issued}

    def replan(self):
        """Allocation again on the original inputs with this dispatch's failed bundles unavailable."""
        import roll
        excluded = self.excluded()
        profile = self.task.get('profile') if isinstance(self.task.get('profile'), dict) else {}
        args = argparse.Namespace(stage='plan', dry_run=False, no_score=True, score=False, demo=False, fast=False,
                                  score_timeout=getattr(self.judge, 'SCORER_TIMEOUT_SECONDS', 30), usage_cache=None,
                                  hours=None,
                                  keep_whole=(f'AgentCompass relaunch of dispatch {self.root_id}: the original roll '
                                              'kept this brief whole') if profile.get('multi_task') else None)
        for _ in range(len(excluded) + 64):
            decision, _ = self.judge.plan_stage({'task': self.task, 'unavailable': sorted(excluded)}, args)
            bundle = decision.get('selected_bundle') or {}
            if (decision.get('error') or decision.get('choice') in (None, 'none', 'self', 'split') or not bundle
                    or decision.get('host_policy_violation') or (decision.get('decision') or {}).get('needs_user_decision')
                    or not decision.get('decision_id')):
                return None
            bundle_id = bundle.get('bundle_id') or decision.get('choice')
            if bundle_id in excluded:
                return None
            if not roll.can_launch_via_paseo(bundle.get('provider')):
                # This relaunch runs on Paseo; a candidate only another surface starts is passed over, not launched.
                excluded.add(bundle_id)
                self.attempts.append({**self.entry(bundle), 'failure': 'not_launchable_on_paseo', 'kind': 'switch',
                                      'covered': True})
                continue
            return decision
        return None

    def finish(self, kind, failure, failed_id):
        text = final_text(kind, self.attempts, self.task)
        self.save('final', outcome={'result': kind, 'next_action': text}, current=(
            self.attempts[-1].get('dispatch_id') if self.attempts else None) or self.root_id)
        outcome = {'action': 'final', 'result': kind, 'covered': bool(failure.get('covered')),
                   'root_dispatch_id': self.root_id, 'attempts': self.attempts, 'lines': lines(self.attempts),
                   'text': text}
        return self.outcome(failed_id, outcome)


def final_text(kind, attempts, task):
    listed = '\n'.join(lines(attempts))
    if kind == 'blocked':
        return ('AgentCompass retried or relaunched this brief on every startable candidate and none is left:\n'
                f'{listed}\nNothing is running it. The holds and cooldowns behind these failures lift on their own '
                '(plan/roll warnings and the tray account list say when). Tell the user.')
    if kind == 'owner_named':
        return ('The model was named by the owner, so AgentCompass retried only a transient failure and does not '
                f'switch to another model:\n{listed}\nNothing is running this brief. Tell the user; to let the router '
                'choose, roll it without --model.')
    return listed


def handle(dispatch_id, *, judge=None, host=None, sleep=time.sleep):
    """The bridge's watcher calls this (`agentcompass dispatch --dispatch-action fallback --dispatch-id ID`) when an
    attempt ends in a failure, and roll when a create fails at once. Returns {'action': 'relaunched' | 'final' |
    'unsettled' | 'disabled' | 'pending', ...}; the watcher stays silent on `relaunched` and sends one notification
    otherwise."""
    if not enabled():
        return {'action': 'disabled'}
    import dispatch_flow
    row = registry.inspect(dispatch_id)
    if row['state'] not in registry.TERMINAL or row['state'] == 'expired':
        dispatch_flow.reconcile(dispatch_id, host=host)
        row = registry.inspect(dispatch_id)
    if row['state'] not in registry.TERMINAL or row['state'] == 'expired':
        return {'action': 'unsettled', 'state': row['state']}
    root_id = registry.chain_root(row)
    root = row if root_id == dispatch_id else registry.inspect(root_id)
    chain = Chain.of(root, judge=judge, host=host, sleep=sleep)
    try:
        chain.claim(dispatch_id)
    except AlreadyHandled as done:
        return done.outcome
    return chain.run(failure_of(row), row)


def after_refusal(decision_id, launch, reason, *, access='write', allow_concurrent=None, cwd=None, judge=None,
                  host=None, sleep=time.sleep):
    """roll's first prepare was refused (registry.CapacityChanged) before any row existed: go on to the next candidate."""
    decision = registry.decision_record(decision_id)
    chain = Chain(decision, launch, access=access, allow_concurrent=allow_concurrent, cwd=cwd, judge=judge,
                  host=host, sleep=sleep)
    return chain.run(refused(reason), bundle=decision['bundle'])
