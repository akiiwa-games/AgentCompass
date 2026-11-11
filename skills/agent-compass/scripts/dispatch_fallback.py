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
  (none given: nothing waits and the next candidate starts now). Nothing sleeps through the wait: the step records the
  retry time (`scheduled`) and returns; the failed attempt's watcher, or any later launch/observe of the brief
  (`resume_due`), runs `handle` again once it is due. The failed attempt's claim and the first dispatch's guard both
  last until then, so no other observer relaunches it meanwhile. The guard lasts as long as the task can wait
  (can_wait_hours); a retry nobody started by then is retired (`retry_expired`), never run beside a new dispatch of
  the brief. The parent's release of the dispatch cancels a scheduled retry (registry.retire_scheduled), and a parent
  follow-up that resumes a failed attempt's worker retires any pending retry or switch (`resumed`) and restores that
  attempt as `current`. A model the owner named (`roll --model`, a
  `user_override` decision) follows the same can_wait_hours rule and is never switched: when its backoff does not fit,
  the parent is told (`owner_named`).
- Otherwise: allocation runs again on the original decision's allocation task (its classifier profile, needs such as
  local_only, fast) with every bundle that failed in this dispatch marked unavailable; the classifier is not asked
  again. The winner is launched with the first dispatch's title, workspace, cwd, access, parent and the original prompt,
  prefixed by a note naming the failed attempts and asking the worker to check the workspace before continuing.
- One worker per brief: a relaunch is prepared only after the failed attempt is terminal and confirmed dead (create
  failed, or the worker ended in error, or the bridge's stop was confirmed), and `Chain.run` rechecks the failed
  attempt's host status via `dispatch_flow.reconcile` before launching so a worker the parent resumed never gets a
  sibling. Between attempts the first dispatch carries a guard (registry.fallback_guard) so the parent or another
  observer cannot start the brief a second time. A failed attempt is claimed once (`handled`), so two observers never
  relaunch it twice.
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


def later_than(stamp, now):
    """True when the stamp is after now; a missing or unreadable stamp has passed."""
    try:
        return isinstance(stamp, str) and registry.parse(stamp) > now
    except ValueError:
        return False


def lease_end(stamp):
    try:
        return registry.stamp(registry.parse(stamp) + timedelta(seconds=registry.RUN_LEASE)) if stamp else None
    except ValueError:
        return None


class AlreadyHandled(Exception):
    def __init__(self, outcome):
        super().__init__('already handled')
        self.outcome = outcome


class Chain:
    """The attempts of one brief, from its first dispatch (`root`) or, before any row exists, from its decision."""

    def __init__(self, decision, launch, *, root=None, access='write', cwd=None,
                 judge=None, host=None):
        if judge is None:
            import route_judge as judge
        self.judge, self.host = judge, host
        self.decision = decision
        self.owner_named = decision.get('constraint') == 'user_override'
        task = dict(decision.get('allocation_task') or decision['task'])
        task.pop('chain', None)   # a relaunch is a new worker, never a resume
        self.task = task
        self.parent = decision['parent_id']
        self.launch = launch
        self.access, self.cwd = access, cwd
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
                   cwd=root.get('cwd'), **kwargs)

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
        """The same rule for every model, the owner's included: the backoff must fit the task's can_wait_hours."""
        return self.backoff(attempt) <= float(self.task.get('can_wait_hours') or 0) * 3600

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
            if state != 'retrying':
                fallback.pop('retry_at', None)
                fallback.pop('retry_of', None)
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        return self.root

    def claim(self, failed_id):
        """Only one observer handles a failed attempt; a second gets the first one's outcome. Returns True when the
        claim is the scheduled same-bundle retry falling due (run it with resume=True), False for a fresh failure.

        A claim stays valid until its own `until`: a step in progress holds it for RUN_LEASE, a scheduled retry until
        its retry time (no observer may take a failure over while its backoff is still being waited out). Only a claim
        whose holder died (a pending one past its `until`) or a scheduled retry that is due may be taken.

        A scheduled retry only runs while the first dispatch's guard holds (fallback `until`, see `schedule`): the
        guard is what keeps the brief from being dispatched again, so a retry found due after it lapsed is retired
        (`retry_expired`) in this same transaction instead, and a fresh dispatch of the brief and this retry never both
        run. Taking a due retry renews the guard, so it cannot lapse between the claim and the relaunch's own guard."""
        claimed = {}

        def change(fallback):
            now = registry.utcnow()
            handled = dict(fallback.get('handled') or {})
            before = claimed['before'] = handled.get(failed_id)
            action = (before or {}).get('action')
            due = action == 'scheduled' and not later_than(before.get('retry_at'), now)
            stale = action == 'pending' and not later_than(before.get('until') or lease_end(before.get('at')), now)
            resume = due or (stale and bool(before.get('resume')))
            if resume and fallback.get('retry_of') == failed_id and not later_than(fallback.get('until'), now):
                registry.retire_scheduled(fallback, now, 'retry_expired', RETRY_EXPIRED_NEXT, self.root_id)
                claimed['before'] = fallback['handled'][failed_id]
                claimed['take'] = False
                return
            claimed['take'] = before is None or due or stale
            if claimed['take']:
                handled[failed_id] = {'action': 'pending', 'at': registry.stamp(now), 'until': registry.stamp(
                    now + timedelta(seconds=registry.RUN_LEASE)), **({'resume': True} if resume else {})}
                claimed['resume'] = resume
                fallback['handled'] = handled
                if resume:
                    lease = registry.stamp(now + timedelta(seconds=registry.RUN_LEASE))
                    fallback['until'] = max(fallback['until'], lease, key=registry.parse)
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        before = claimed['before']
        if claimed['take']:
            return claimed['resume']
        if before.get('action') == 'pending':
            raise AlreadyHandled({'action': 'pending', 'root_dispatch_id': self.root_id,
                                  'detail': 'another observer is relaunching this attempt'})
        raise AlreadyHandled(before)

    def outcome(self, failed_id, outcome):
        if not self.root or not failed_id:
            return outcome

        def change(fallback):
            fallback['handled'] = {**(fallback.get('handled') or {}), failed_id: outcome}
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        return outcome

    # The loop.

    def run(self, failure, row=None, bundle=None, resume=False):
        """From one failed attempt to the next live worker, a scheduled retry or a final state. `row` is the failed
        attempt's row (None when its bundle was refused before a row existed); `bundle` the refused bundle then.
        `resume`: the scheduled same-bundle retry of `row` is due (claim said so); it starts without deciding again."""
        failed_id = row['dispatch_id'] if row else None
        while True:
            attempt = next((a for a in self.attempts if row and a.get('dispatch_id') == row['dispatch_id']), None)
            if attempt is None:
                attempt = self.entry(bundle or row['bundle'], row)
                self.attempts.append(attempt)
            kept = len(self.attempts)
            if resume:
                resume = False
                resumed_id = self.resumed_attempt(row)
                if resumed_id:
                    return self.settle_resumed(resumed_id, attempt, failed_id)
                choice = (self.same_bundle(attempt), 'retry_same', 0)
            else:
                self.failed(attempt, failure, row)
                if not failure['covered']:
                    self.ledger('declined', attempt)
                    return self.finish('declined', failure, failed_id)
                resumed_id = self.resumed_attempt(row)
                if resumed_id:
                    return self.settle_resumed(resumed_id, attempt, failed_id)
                choice = self.next_choice(attempt)
            if choice is None:
                kind = 'owner_named' if self.owner_named else 'blocked'
                self.ledger(kind, attempt, excluded=sorted(self.excluded()))
                return self.finish(kind, failure, failed_id)
            decision, how, wait = choice
            if how == 'scheduled':
                return self.schedule(attempt, wait, failed_id)
            if how == 'switch':
                resumed_id = self.resumed_attempt(row)
                if resumed_id:
                    self.attempts[kept:] = []
                    return self.settle_resumed(resumed_id, attempt, failed_id)
            new_bundle = decision['selected_bundle']
            self.ledger(how, attempt, next={'decision_id': decision['decision_id'], **bundle_view(new_bundle)})
            try:
                new = registry.prepare(decision['decision_id'], self.task, dict(
                    self.launch, initialPrompt=relaunch_prompt(self.launch['initialPrompt'], self.attempts)),
                    self.judge.usage_snapshot(None), access=self.access,
                    cwd=self.cwd, attempt={'attempt_of': self.root_id, 'attempt': len(self.attempts) + 1,
                                           'previous_attempt': (row or {}).get('dispatch_id')} if self.root else None)
            except registry.CapacityChanged as exc:
                row, bundle, failure = None, new_bundle, refused(exc)
                continue
            except ValueError as exc:   # a writer appeared in the workspace, or the decision cannot dispatch
                live = self.live_attempt(failed_id)
                if live:
                    # Another attempt of this brief already runs (its own watcher reports): this failure is settled by
                    # it, and the chain's record must keep pointing at it.
                    return self.outcome(failed_id, {'action': 'relaunched', 'dispatch_id': live,
                                                    'root_dispatch_id': self.root_id, 'attempts': self.attempts,
                                                    'lines': lines(self.attempts),
                                                    'detail': 'another attempt of this brief is live'})
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
        """(decision, 'retry_same' | 'switch', 0) for the next attempt now, (None, 'scheduled', seconds) for a same-bundle
        retry after its backoff, or None when nothing may start."""
        if attempt.get('kind') == TRANSIENT and self.transient_tries(attempt['bundle_id']) == 1 and self.can_retry(attempt):
            wait = self.backoff(attempt)
            if wait > 0:
                return None, 'scheduled', wait
            return self.same_bundle(attempt), 'retry_same', 0
        if self.owner_named:
            return None
        decision = self.replan()
        return (decision, 'switch', 0) if decision else None

    def schedule(self, attempt, wait, failed_id):
        """Nothing blocks for the backoff: the retry time is recorded and the step returns. The failed attempt's watcher
        (paseo_dispatch_bridge.mjs, outcome `scheduled`) or any later observation (`resume_due`, from dispatch launch or
        observe) runs `handle` again once it is due; the claim and the guard hold until then, so no one starts the brief
        in between and the observer that resumes is the only one.

        The guard (fallback `until`) lasts as long as the task said it can wait (can_wait_hours from the failure), and at
        least RUN_LEASE past the retry time: a retry nobody started by then is retired by the next claim (`claim`), so
        the guard never lapses while the retry may still run."""
        now = registry.utcnow()
        retry_at = registry.stamp(now + timedelta(seconds=wait))
        until = registry.stamp(now + timedelta(seconds=max(wait + registry.RUN_LEASE,
                                                            float(self.task.get('can_wait_hours') or 0) * 3600)))
        self.ledger('scheduled', attempt, wait_seconds=round(wait), retry_at=retry_at)
        self.save('retrying', current=attempt.get('dispatch_id'), retry_of=failed_id, retry_at=retry_at, until=until)
        return self.outcome(failed_id, {'action': 'scheduled', 'root_dispatch_id': self.root_id, 'retry_at': retry_at,
                                        'attempts': self.attempts, 'lines': lines(self.attempts)})

    def same_bundle(self, attempt):
        """The decision for a transient failure's retry on its own bundle. It is issued and written to the ledger like
        every routed start (a plan line, or a record line for the owner's model), so the audit sees a route for it."""
        # A transient failure is a provider's, so it always has the row whose bundle it was.
        bundle = dict(registry.inspect(attempt['dispatch_id'])['bundle'])
        result = {'stage': 'record' if self.owner_named else 'plan', 'source': 'fallback_retry',
                  'choice': bundle.get('bundle_id'), 'selected_bundle': bundle, 'decision': {'action': 'start'},
                  'parent_id': self.parent,
                  **({'constraint': 'user_override',
                      'basis': f'AgentCompass transient retry of dispatch {self.root_id} on the owner-named model'}
                     if self.owner_named else
                     {'basis': f'AgentCompass transient retry of dispatch {self.root_id} on its routed bundle'})}
        issued = registry.issue(result, self.task, allocation_task=self.task)
        decision = {**result, **issued}
        try:
            self.judge.append_ledger({
                'v': 1, 'at': registry.stamp(registry.utcnow()), 'stage': decision['stage'], 'source': 'fallback_retry',
                'choice': decision['choice'],
                **{k: decision[k] for k in ('decision_id', 'parent_id', 'task_hash', 'bound') if decision.get(k)},
                **({'task_id': self.task['task_id']} if self.task.get('task_id') else {}),
                'constraint': decision.get('constraint'), 'basis': decision['basis'][:400],
                'bundle': {k: bundle.get(k) for k in ('provider', 'account', 'model', 'effort', 'bundle_id', 'fast')
                           if bundle.get(k) is not None},
                'action': 'start', 'root_dispatch_id': self.root_id, 'attempt': len(self.attempts) + 1})
        except Exception:   # noqa: BLE001 - auditing never stops a relaunch
            pass
        return decision

    def live_attempt(self, failed_id):
        """Another attempt of this brief that is running or may still appear, if the chain's record names one."""
        current = ((self.root or {}).get('fallback') or {}).get('current')
        if not current or current in (failed_id, None):
            return None
        try:
            row = registry.inspect(current)
        except (KeyError, ValueError):
            return None
        return current if registry.unresolved(row, registry.utcnow()) else None

    def resumed_attempt(self, row=None):
        """A failed attempt of this brief whose worker the parent resumed, observed on the host via reconcile."""
        if not self.root:
            return None
        import dispatch_flow
        seen = []
        for candidate in ([row['dispatch_id']] if row else []) + [
                a['dispatch_id'] for a in reversed(self.attempts) if a.get('dispatch_id')]:
            if candidate not in seen:
                seen.append(candidate)
        for dispatch_id in seen:
            target = row if row and row['dispatch_id'] == dispatch_id else registry.inspect(dispatch_id)
            if target.get('state') == 'failed' and target.get('launched_at') and target.get('agent_id'):
                dispatch_flow.reconcile(dispatch_id, host=self.host)
                self.root = registry.inspect(self.root['dispatch_id'])
                fb = self.root.get('fallback') or {}
                if fb.get('state') == 'final' and (fb.get('outcome') or {}).get('result') == 'resumed':
                    return fb.get('current') or self.root['dispatch_id']
        self.root = registry.inspect(self.root['dispatch_id'])
        fb = self.root.get('fallback') or {}
        if fb.get('state') == 'final' and (fb.get('outcome') or {}).get('result') == 'resumed':
            return fb.get('current') or self.root['dispatch_id']
        return None

    def settle_resumed(self, resumed_id, attempt, failed_id):
        self.ledger('resumed', attempt, resumed_dispatch_id=resumed_id)
        attempts = [dict(a) for a in self.attempts]

        def change(fallback):
            fallback.update(attempts=attempts, lines=lines(attempts))
            registry.retire_scheduled(fallback, registry.utcnow(), 'resumed', registry.RESUMED_NEXT,
                                      self.root_id, current=resumed_id, extra_ids=(failed_id,))
        self.root = registry.update_fallback(self.root['dispatch_id'], change)
        return self.root['fallback']['handled'][failed_id or resumed_id]

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


RETRY_EXPIRED_NEXT = ('AgentCompass scheduled a retry of this brief on the same model after a transient provider failure, '
                      'but nothing started it before the wait the task allows (can_wait_hours) ran out, so the retry is '
                      'retired and nothing runs this brief. Tell the user; roll it again if it is still wanted.')


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


def handle(dispatch_id, *, judge=None, host=None):
    """The bridge's watcher calls this (`agentcompass dispatch --dispatch-action fallback --dispatch-id ID`) when an
    attempt ends in a failure, and roll when a create fails at once. Returns {'action': 'relaunched' | 'scheduled' |
    'final' | 'unsettled' | 'disabled' | 'pending', ...}; the watcher stays silent on `relaunched`, calls again at
    `retry_at` on `scheduled`, looks again on `pending`, and sends one notification otherwise. Called again for a
    failure whose scheduled retry is due, it starts that retry."""
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
    if registry.fallback_guard(root, registry.utcnow()):
        dispatch_flow.reconcile(root_id, host=host)
        root = registry.inspect(root_id)
    chain = Chain.of(root, judge=judge, host=host)
    try:
        resume = chain.claim(dispatch_id)
    except AlreadyHandled as done:
        return done.outcome
    return chain.run(failure_of(row), row, resume=resume)


def resume_due(dispatch_id, *, host=None, now=None):
    """Any observation of a brief (dispatch launch/observe) starts its scheduled same-bundle retry once it is due, so
    the retry does not depend on the failed attempt's watcher alone. None when nothing is due."""
    if not enabled():
        return None
    try:
        row = registry.inspect(dispatch_id)
        root = row if registry.chain_root(row) == dispatch_id else registry.inspect(registry.chain_root(row))
    except (KeyError, ValueError):
        return None
    fallback = root.get('fallback') or {}
    if fallback.get('state') != 'retrying' or not fallback.get('retry_of') \
            or later_than(fallback.get('retry_at'), now or registry.utcnow()):
        return None
    return handle(fallback['retry_of'], host=host)


def after_refusal(decision_id, launch, reason, *, access='write', cwd=None, judge=None, host=None):
    """roll's first prepare was refused (registry.CapacityChanged) before any row existed: go on to the next candidate."""
    decision = registry.decision_record(decision_id)
    chain = Chain(decision, launch, access=access, cwd=cwd, judge=judge, host=host)
    return chain.run(refused(reason), bundle=decision['bundle'])
