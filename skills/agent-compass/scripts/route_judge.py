"""Read local subscription usage and obtain bounded low-effort routing judgments."""

import argparse
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request

# Also support the path-loaded allocator and python -I entrypoints.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_state
import antigravity_plan
import decision_ledger
import dispatch_flow
import dispatch_registry
import entry
import fast_modes
import host_policy
import provider_inventory
import public_data
import quota_overrides
import roll
import scorer_client
import stage_help
import subscription_judge
import usage_reading


CONSTRAINTS = ('user_override', 'hard_constraint', 'dot_closed_loop')
# Why an override departs from the router, so overrides can later be learned from as preferences: the task was
# classified wrongly, the router's pick is not good enough, it is stronger than needed, the chain should stay with
# its owner, the pick cannot run, or something else (say what in --basis).
REASON_CATEGORIES = ('misclassified', 'quality_insufficient', 'overqualified', 'continuity', 'unavailable', 'other')


def dot_unreachable(timeout=1.5):
    """Why the local dot2api cannot be called now, or None when /readyz answers. Reads no credential."""
    base = os.environ.get('DOT2API_BASE_URL', '').strip()
    if not base:
        try:
            config = json.loads((Path.home() / '.dot2api' / 'client.json').read_text(encoding='utf-8'))
            base = str(config.get('base_url', '')).strip()
        except (OSError, ValueError, AttributeError):
            return 'no ~/.dot2api/client.json'
    if not base:
        return 'client.json has no base_url'
    try:
        with urllib.request.urlopen(base.rstrip('/') + '/readyz', timeout=timeout) as response:
            return None if response.status == 200 else f'/readyz answered {response.status}'
    except (OSError, ValueError) as exc:
        return f'{type(exc).__name__} on /readyz'


def recent_route_lock(task, records, now, bundle_id, window_seconds):
    """Find a matching recent route only for an explicitly configured reroute guard."""
    if not isinstance(task, dict) or not records:
        return None
    chain_id = (task.get('chain') or {}).get('id') if isinstance(task.get('chain'), dict) else None
    task_id = (task.get('task_id') or '').strip() if isinstance(task.get('task_id'), str) else None
    req_hash = decision_ledger.task_request_hash(task)
    if not chain_id and not task_id and not req_hash:
        return None
    for row in reversed(records):
        if (not isinstance(row, dict) or row.get('stage') != 'plan'
                or row.get('choice') not in (bundle_id, fast_modes.variant_id(bundle_id))):
            continue
        try:
            at = datetime.fromisoformat(str(row.get('at')).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            continue
        if (now - at).total_seconds() > window_seconds:
            break
        row_hash = row.get('request_hash') or row.get('task_hash')
        if ((chain_id and row.get('chain') == chain_id)
                or (task_id and row.get('task_id') == task_id)
                or (req_hash and row_hash == req_hash)):
            return row
    return None


def match_bundle(accounts, args, include_disabled=False):
    """Bundle id for an account/model/effort the caller names, or None when it is not a launchable bundle.

    A name that only a family guess could place matches its own rows, never the flagship's. With no effort named, the
    model's default effort (else High) leads, not whichever row the document lists first."""
    import account_fleet
    matches = []
    for bundle in account_fleet.bundles(accounts).values():
        if not include_disabled and bundle.get('disabled'):
            continue
        same = account_fleet.row_key(bundle['model'], accounts) == account_fleet.row_key(args.model, accounts)
        if (bundle['account'] == args.account and bundle['provider'] == args.provider and same
                and (args.effort is None or bundle.get('effort') == args.effort)):
            matches.append(bundle)
    if not matches:
        return None
    default = account_fleet.model_spec(accounts, args.model).get('default_effort') or 'high'
    return next((b for b in matches if b.get('effort') == default), matches[0])['id']


NAMED_ROW_FIELDS = ('modeId', 'draws', 'launch_model', 'harness', 'billing')


def record_constraint(args, task=None, classified=None):
    """Log a route the caller is forcing. No judge is called; the allocator is run on the same task so the
    ledger shows whether the router would have chosen it and, if not, what the override cost.

    `classified` is classify_pinned's answer for a pinned roll: the allocator then plans the classified task, the
    pinned bundle carries that plan's price, and the launch is reserved from the same profile."""
    basis = (args.basis or '').strip()
    if not basis or not args.account or not args.provider or not args.model:
        raise ValueError('record requires --basis, --provider, --account and --model.')
    started = time.monotonic()
    if task is None:
        task = {}
    if not task and args.input is not None:
        loaded = json.loads(args.input.read_text(encoding='utf-8-sig'))
        task = loaded.get('task') if isinstance(loaded, dict) and isinstance(loaded.get('task'), dict) else {}
    if not task.get('parent_agent_id'):
        # The gate matches same-session rows first; a record without the session ID loses to any
        # roll this session made inside the window. Only real host IDs, never the ppid fallback.
        from worker_context import detect_host_session
        parent = (getattr(args, 'parent_id', None) or '').strip() or detect_host_session()
        parent = parent.removeprefix('claude:')
        if parent and not parent.startswith(('claude-session-', 'codex-session-')):
            task = {**task, 'parent_agent_id': parent}
    import account_fleet
    import allocator
    import auto_bundles
    import axis_scores
    import candidate_eval
    import candidate_ranking
    import launch_failures
    import model_latch
    import model_resolution
    import provider_retry
    import quota_pools
    accounts = account_fleet.load_accounts()
    now = datetime.now(timezone.utc)
    usage = usage_reading.usage_snapshot(args.usage_cache, args.accounts, now)
    usage = provider_inventory.merge_usage(usage, accounts, now, args.accounts)
    unavailable = provider_inventory.unavailable_reason(accounts.get('_discovery') or {}, args.account, args.provider)
    if unavailable:
        raise ValueError(unavailable)
    # `model:effort` and `model-effort` name an effort; --effort, when given too, wins.
    pin = copy.copy(args)
    pin.model, named_effort = model_resolution.split_model_effort(args.model)
    resolved = account_fleet.resolve_model(pin.model, accounts)
    pin.effort = args.effort or named_effort or resolved['effort']
    direct = resolved['source'] in model_resolution.DIRECT_SOURCES
    own_model = account_fleet.row_key(pin.model, accounts)
    own_rows = [b for b in account_fleet.bundles(accounts).values() if b['account'] == args.account and b['provider'] == args.provider
                and account_fleet.row_key(b['model'], accounts) == own_model]
    if not direct and (not own_rows or (pin.effort and pin.effort not in {b.get('effort') for b in own_rows}
                                        and not any(pin.effort in (account_fleet.dispatch_allowed_efforts(b, accounts) or ())
                                                    for b in own_rows))):
        # What it resembles is another model. The same answer the plan gives: blocked, never a substitute.
        raise ValueError(f"named_model_unavailable: {pin.model}{':' + pin.effort if pin.effort else ''} has no configured route on "
                         f"{args.account}/{args.provider}. Tell the user; do not substitute another model.")
    chosen = match_bundle(accounts, pin)
    disabled_match = match_bundle(accounts, pin, include_disabled=True) if chosen is None else None
    if disabled_match is not None:
        if account_fleet.bundles(accounts)[disabled_match].get('disabled_by_default'):
            chosen = disabled_match     # off by default, not by the user: naming the model reaches it
        else:
            raise ValueError(
                f"Bundle {disabled_match!r} ({args.account}/{pin.model}) is disabled. "
                f"Do not dispatch {pin.model} on {args.account}; run `{router_command()} plan` to select an enabled bundle."
            )
    if own_rows and all(b.get('disabled') and not b.get('disabled_by_default') for b in own_rows):
        # Every level unchecked is the model unchecked, also at a level with no row of its own.
        raise ValueError(
            f"Bundle {own_rows[0]['id']!r} ({args.account}/{pin.model}) is disabled. "
            f"Do not dispatch {pin.model} on {args.account}; run `{router_command()} plan` to select an enabled bundle."
        )
    discovered = account_fleet.bundles(accounts).get(chosen) or {}
    if discovered.get('discovery_unavailable'):
        raise ValueError(discovered['discovery_unavailable'])
    # The account's other row of this model supplies the launch fields of an effort it has no row for.
    sibling_id = (chosen or match_bundle(accounts, _with_effort(pin, None))
                  or match_bundle(accounts, _with_effort(pin, None), include_disabled=True))
    sibling = account_fleet.bundles(accounts).get(sibling_id) or {}
    inherited = {k: sibling[k] for k in NAMED_ROW_FIELDS if k in sibling}
    acc_spec = (accounts.get('accounts') or {}).get(args.account) or {}
    if not account_fleet.account_cwd_allowed(acc_spec, (task or {}).get('cwd')):
        raise ValueError(
            f"Account {args.account!r} is restricted to cwd_regex {acc_spec.get('cwd_regex')!r} "
            f"and cannot be dispatched from the current working directory."
        )
    # External runners dispatch directly after record; apply the same registered
    # window protection used by prepared dispatches.
    quota_bundle = {**discovered, **inherited, 'account': args.account, 'provider': args.provider, 'model': pin.model,
                    'effort': pin.effort or discovered.get('effort')}
    if chosen:
        quota_bundle['id'] = chosen
        quota_bundle.setdefault('bundle_id', chosen)
    dispatch_registry.check_launch_quota(quota_bundle, accounts, usage, now)
    acc_usage = (usage.get('accounts') or {}).get(args.account) or {}
    if acc_usage.get('status') == 'fresh' or acc_usage.get('rate_limited_until') or quota_pools.latched_windows(accounts, args.account):
        blockers = quota_pools.launch_blockers(
            quota_bundle, accounts, usage, now, records=dispatch_flow.reservation_records(read_only=True))
        if blockers:
            wid, reason = blockers[0]
            raise ValueError(
                f"Account {args.account!r} window {wid!r} is exhausted or below safety floor ({reason}). "
                f"Do not dispatch {args.account}; run `{router_command()} plan` to pick an account with remaining capacity."
            )
    cooling = (candidate_eval.cooling_reason(quota_bundle, launch_failures.active(), accounts)
               if launch_failures.enabled() else None)
    if cooling:
        raise ValueError(f"{args.account}/{pin.model} is cooling down ({cooling}). Tell the user; do not substitute another model.")
    latched_model = model_latch.reason(quota_bundle, model_latch.active(now=now))
    if latched_model:
        raise ValueError(f"{args.account}/{pin.model}: {latched_model}. Tell the user; do not substitute another model.")
    # A model the user names on a held account or model is the probe that can lift the hold: it goes, with a warning.
    retrying = provider_retry.reason(quota_bundle, provider_retry.active(now=now))
    c_model = resolved['key'] if direct else account_fleet.model_leaf(pin.model)
    fast_requested = bool(getattr(args, 'fast', False))
    if fast_requested:
        fast_modes.validate(account_fleet.bundle_harness(discovered or {'provider': args.provider}, args.account),
                                      c_model, account_fleet.model_spec(accounts, pin.model))
    warnings = []
    effort = pin.effort or discovered.get('effort')
    reviewed_task = task
    if classified:
        # What is planned and reserved; the decision stays bound to the caller's task.
        task = {**classified['task'], **({'parent_agent_id': task['parent_agent_id']} if task.get('parent_agent_id') else {})}
    records = None
    if effort is None and not sibling:
        effort = auto_bundles.HARNESS_DEFAULT_EFFORT.get(account_fleet.bundle_harness({'provider': args.provider, 'model': c_model}, args.account))
    base_b = account_fleet.bundles(accounts).get(chosen) or {}
    bundle = {**inherited, **base_b, 'provider': args.provider, 'account': args.account, 'model': c_model,
              'effort': effort, **({'bundle_id': chosen} if chosen else {}), **({'fast': True} if fast_requested else {})}
    if not direct:
        bundle.setdefault('launch_model', pin.model)
    bundle['harness'] = account_fleet.bundle_harness(bundle, args.account, chosen)
    if 'modeId' not in bundle and bundle['harness'] in auto_bundles.HARNESS_MODE:
        bundle['modeId'] = auto_bundles.HARNESS_MODE[bundle['harness']]
    if fast_requested and bundle['harness'] == 'claude':
        bundle['fast_billing'] = fast_modes.claude_billing(accounts['accounts'][args.account])
    result = {'stage': 'record', 'choice': 'constraint', 'source': 'constraint_record',
              'constraint': args.constraint, 'basis': basis[:400], 'specialty': args.specialty,
              **({'trigger': args.trigger} if getattr(args, 'trigger', None) else {}),
              'reason_category': getattr(args, 'reason_category', None),
              'selected_bundle': bundle, 'judge': None, 'observed_at': now.isoformat(), 'usage': usage}
    overlaid_accounts = axis_scores.five_axis_overlay(accounts)
    overlaid_spec = (overlaid_accounts.get('accounts') or {}).get(args.account) or {}
    if overlaid_spec.get('capacity_unknown') and overlaid_spec.get('five_axis_free_pool'):
        warnings = [f"{args.account}/{pin.model}: capacity_unknown (quota unknown, spend unpriced); dispatched because the user named it", *warnings]
    if retrying:
        warnings = [f"{args.account}/{pin.model}: {retrying}; dispatched anyway because the user named it: a turn "
                    "that ends on the model's reply lifts the hold (a cancel or stop does not)", *warnings]
    if warnings:
        result['warnings'] = warnings
    if classified:
        result['_allocation_task'] = task
        if classified.get('scorer') is not None:
            result.update(scorer=classified['scorer'], task_profile=task['profile'])
        if classified.get('hours_estimate'):
            result['hours_estimate'] = classified['hours_estimate']
        if classified.get('alerts'):
            result['alerts'] = list(classified['alerts'])
    if fast_requested:
        result['_allocation_task'] = {**(task or {}), 'force_fast': True}
    # Account loading may enroll a newly discovered account; check the resulting policy.
    availability = host_policy.load_policy()
    if not host_policy.account_allowed(availability, args.account):
        result['host_policy_violation'] = {'account': args.account, 'dispatch_allowed': False,
                                           'policy_path': str(host_policy.policy_path()[0]),
                                           'reason': 'excluded by host policy; constraint recorded, not dispatch authorization'}
    if task and not isinstance(task.get('profile'), dict):
        # record / roll --provider never calls the classifier, and the router plans only a classified task: with no
        # caller profile there is nothing to compare the user's choice with.
        result.update({'router_choice': None, 'agrees': None, 'classifier_skipped': True})
    elif task:
        try:
            plan_task = {**task, 'force_fast': True} if fast_requested else task
            has_cadence = any(
                isinstance((spec or {}).get('cadence'), dict)
                for spec in (account_fleet.load_accounts().get('accounts') or {}).values()
            )
            plan_records = (records if records is not None else
                            dispatch_flow.reservation_records(read_only=not decision_ledger.LEDGER_WRITES)
                            if not ((task.get('chain') or {}).get('id') or has_cadence) else
                            decision_ledger.read_ledger() + dispatch_flow.reservation_records(read_only=not decision_ledger.LEDGER_WRITES))
            plan = allocator.plan(plan_task, usage, now, records=plan_records)
        except (ValueError, KeyError, TypeError) as exc:
            result['router_error'] = f'{type(exc).__name__}: {exc}'
        else:
            row = candidate_ranking.candidate_for_bundle(plan['ranking'], chosen, fast_requested)
            result.update({'router_choice': plan['choice'],
                           'agrees': chosen is not None and chosen == (plan.get('selected_bundle') or {}).get('bundle_id', plan['choice'])
                           and fast_requested == bool((plan.get('selected_bundle') or {}).get('fast')),
                           'classifier_skipped': False,
                           'router_reason': plan['decision'].get('reason'),
                           'override_cost': None if row is None else {'excluded': row['excluded'], 'score': row['score'],
                                                                      'notes': row['notes']}})
            if classified and row is not None and isinstance(row.get('expected_usd'), (int, float)):
                result['expected_usd'] = row['expected_usd']    # the pinned bundle's own price, as the launch reserves it
    return decision_ledger.finalize(result, None, 'auto', started, reviewed_task if classified else task)


def _with_effort(pin, effort):
    other = copy.copy(pin)
    other.effort = effort
    return other




def router_command():
    """The public entry as this install spells it (entry.py)."""
    return entry.command()


ONBOARD_HINT = (f'Run `{router_command()} setup` to detect the CLIs you are signed in to and start usage collection.')


def readiness_gap(accounts, usage):
    """Describe why the router cannot honestly recommend anyone yet, or return None.

    `no_ready_account`: discovery ran and not one account is launchable. `no_usage_data`: no collector snapshot exists,
    so every pick is made without quota readings. A ready account with data on file is unaffected.
    """
    inventory = ((accounts.get('_discovery') or {}).get('accounts') or {})
    specs = accounts.get('accounts') if isinstance(accounts.get('accounts'), dict) else {}

    def launchable(name, row):
        if not account_state.dispatchable(row):
            return False
        # A missing spec keeps the old contract: discovery status ready is enough.
        # verified false is an explicit "do not pick this confidently".
        spec = specs.get(name)
        if isinstance(spec, dict) and spec.get('verified') is False:
            return False
        return True

    if inventory and not any(launchable(name, row) for name, row in inventory.items()):
        return {'code': 'no_ready_account', 'fatal': True,
                'message': 'No provider account is ready on this machine (no signed-in CLI was found). ' + ONBOARD_HINT}
    if not usage.get('read_ok'):
        return {'code': 'no_usage_data', 'fatal': False,
                'message': 'No usage data is available (the collector has not produced a snapshot), so quota-aware '
                           'ranking is running blind and the pick is a default, not a measured choice. ' + ONBOARD_HINT}
    return None


def apply_readiness_gap(planned, accounts, usage):
    gap = readiness_gap(accounts, usage)
    if gap is None:
        return
    planned.setdefault('alerts', []).append({'code': gap['code'], 'severity': 'warn', 'account': None, 'message': gap['message']})
    if gap['fatal']:
        planned['choice'] = 'none'
        planned.pop('selected_bundle', None)
        planned.setdefault('decision', {})['reason'] = gap['message']
        planned['error'] = gap['code']
        planned['next'] = ONBOARD_HINT


SPLIT_NEXT = ('The classifier reads this brief as {n} separate tasks (task_count); nothing was allocated or launched. You '
              'are the coordinator: split it yourself, do the tasks coupled to this conversation yourself, write one '
              'brief per independent task and roll them together (--brief a.md --brief b.md), and start a task that '
              'needs another\'s result after that result returns. If it truly is one assignment, roll it again with '
              '--keep-whole "<why>".')


def split_result(task, count, scorer, started):
    """No allocation for a brief that bundles separate tasks: the parent splits it. Logged under stage `split`, which
    authorizes no launch."""
    result = {'stage': 'split', 'choice': 'split', 'task_count': count, 'task_profile': task.get('profile'),
              'scorer': scorer, 'reason': (task.get('profile') or {}).get('reason') or None,
              'next': SPLIT_NEXT.format(n=count)}
    objective = task.get('objective')
    decision_ledger.append_ledger({'v': 1, 'at': datetime.now(timezone.utc).isoformat(), 'stage': 'split', 'choice': 'split',
                   **({'task_id': task['task_id']} if task.get('task_id') else {}),
                   **({'parent_id': task['parent_agent_id']} if task.get('parent_agent_id') else {}),
                   'kind': task.get('work_kind'), 'task_count': count,
                   **({'scorer': scorer.get('source')} if isinstance(scorer, dict) else {}),
                   'objective': objective[:200] if isinstance(objective, str) else None,
                   'duration_ms': int((time.monotonic() - started) * 1000)})
    return result


SCORER_UNAVAILABLE_NEXT = (
    'All System-One classifiers timed out or failed; mechanical keyword fallback has been removed. '
    'As the parent session, choose autonomously: either execute the task directly in this session (self), '
    'or dispatch with an explicit parent choice (`--provider <p> --account <a> --model <m> --effort <e> '
    '--basis "scorer_unavailable: parent autonomous choice"`).'
)


def scorer_unavailable_result(task, scorer, started):
    """When all System-One classifiers fail or time out, do not guess mechanically; return control to the parent session."""
    why = (scorer or {}).get('fallback_detail') or (scorer or {}).get('fallback_reason') or 'scorer_unavailable'
    reason = f'System-One classifier unavailable ({why}); mechanical fallback is disabled so the parent session chooses autonomously.'
    objective = task.get('objective')
    result = {
        'stage': 'plan', 'status': 'blocked', 'choice': 'none', 'error': 'scorer_unavailable',
        'caller_action': 'parent_autonomous_choice',
        'scorer': scorer, 'task_profile': None, 'reason': reason,
        'decision': {'action': 'parent_choice', 'reason': reason, 'needs_user_decision': False},
        'next': SCORER_UNAVAILABLE_NEXT,
    }
    decision_ledger.append_ledger({
        'v': 1, 'at': datetime.now(timezone.utc).isoformat(), 'stage': 'plan', 'choice': 'none',
        'error': 'scorer_unavailable', 'reason': reason,
        **({'task_id': task['task_id']} if task.get('task_id') else {}),
        **({'parent_id': task['parent_agent_id']} if task.get('parent_agent_id') else {}),
        'scorer': (scorer or {}).get('source'),
        'objective': objective[:200] if isinstance(objective, str) else None,
        'duration_ms': int((time.monotonic() - started) * 1000),
    })
    return result


def classify_task(task, needs, capabilities, policy, timeout, use_model=True):
    """System-One classification of one task, the same for a planned roll and a pinned one.

    Returns (profile, task, needs, scorer, hours_estimate, alerts). profile is None when no classifier answered;
    task is then unchanged. hours_estimate is the classifier's own expected_hours when the caller gave no --hours,
    else None: nothing here invents hours."""
    import task_intake
    alerts, hours_estimate = [], None
    profile, scorer = scorer_client.score_task(task, capabilities, timeout, use_model=use_model)
    if profile is None:
        return None, task, needs, scorer, None, alerts
    classified_kind = task_intake.profile_kind(profile, policy)
    derived = task_intake.profile_difficulty(profile, policy)
    order = list(task_intake.DIFFICULTY_ORDER)
    if task.get('difficulty') in order and order.index(task['difficulty']) > order.index(derived):
        alerts.append({
            'code': 'difficulty_above_profile', 'severity': 'info', 'account': None,
            'message': f"The caller's difficulty {task['difficulty']!r} is above the classifier profile's {derived!r} "
                       f"(reasoning_depth {profile['reasoning_depth']}); the classifier's {derived!r} was used. "
                       'Do not hand-fill difficulty or work_kind in task.json.'})
    if scorer.get('source') == 'system_one':
        if task.get('work_kind') or task.get('difficulty'):
            alerts.append({
                'code': 'caller_classification_ignored', 'severity': 'info', 'account': None,
                'message': 'Caller-supplied work_kind/difficulty was ignored; the System-One classifier '
                           f'assigned work_kind={classified_kind!r}, difficulty={derived!r}.'})
        task = {k: v for k, v in task.items() if k not in ('work_kind', 'difficulty')}
        task['work_kind'] = classified_kind
        # Leave difficulty off the task. normalize_task then labels the source `profile`
        # instead of treating the classifier's copy as a caller value. The strip above
        # still lets the classifier win over a caller-supplied difficulty.
    if profile['local_only'] and needs.get('local_only') is None:
        # Trust the classifier; explicit caller needs still take precedence.
        needs = {**needs, 'local_only': True,
                 'local_only_reason': profile.get('reason') or 'Task profile requires execution on this machine.'}
        task = {**task, 'needs': needs}
        alerts.append({
            'code': 'profile_local_only_applied', 'severity': 'info', 'account': None,
            'reason': needs['local_only_reason'],
            'message': 'The task profile requires local execution: ' + needs['local_only_reason']})
    task = {**task, 'profile': profile}
    if task.get('hours_source') == 'classifier_estimate' and task.get('expected_hours') is None:
        if scorer.get('source') == 'system_one' and profile.get('expected_hours'):
            task['expected_hours'] = profile['expected_hours']
            hours_estimate = {'hours': profile['expected_hours'], 'source': 'classifier_estimate',
                              'confidence': profile.get('hours_confidence', 'unknown')}
    return profile, task, needs, scorer, hours_estimate, alerts


def pinned_unclassified_result(task, scorer, hours, started):
    """A pinned roll with no task profile. The allocator prices and reserves a launch only from a profile, so
    nothing launches; the message says what is missing and never names an hours figure it does not have."""
    why = (scorer or {}).get('fallback_detail') or (scorer or {}).get('fallback_reason') or 'scorer_unavailable'
    given = (f'--hours {hours:g} was given, but' if hours is not None
             else '--hours was not given, so there is no hours estimate, and AgentCompass does not invent one;')
    reason = (f'Pinned roll: the System-One classifier is unavailable ({why}). {given} without the classifier\'s task '
              'profile the pinned bundle cannot be priced or reserved, so nothing was launched.')
    result = scorer_unavailable_result(task, scorer, started)
    result.update(reason=reason, decision=dict(result['decision'], reason=reason), pinned=True,
                  next='Run the same roll again when the classifier answers (`agentcompass scorer --action test` checks '
                       'it), or do the task in this session.')
    return result


def classify_pinned(task, args):
    """`roll --provider/--account/--model`: only the model choice is pinned. The task gets the same System-One profile
    and hours estimate as an automatic roll, so the pinned bundle is priced and reserved like any other launch.

    Returns {'task', 'scorer', 'hours_estimate', 'alerts'}; or {'result': ...} when the roll stops here (split, or no
    profile to price with)."""
    started = time.monotonic()
    hours = task.get('expected_hours')
    if isinstance(task.get('profile'), dict):
        estimate = None
        if hours is None and task.get('hours_source') == 'classifier_estimate':
            given = task['profile'].get('expected_hours')
            estimate = {'hours': given, 'source': 'caller_profile',
                        'confidence': task['profile'].get('hours_confidence', 'unknown')} if given else None
        return {'task': task, 'scorer': None, 'hours_estimate': estimate, 'alerts': []}
    if getattr(args, 'dry_run', False) and not getattr(args, 'score', False):
        # A preview launches and reserves nothing, so it needs no price: the pin is checked, the classifier is not called.
        return {'task': task, 'scorer': None, 'hours_estimate': None, 'alerts': []}
    if getattr(args, 'no_score', False):
        scorer = {'source': 'scorer_unavailable', 'model': None, 'fallback_reason': 'no_score_without_profile',
                  'fallback_detail': '--no-score was given and the task carries no profile'}
        return {'result': pinned_unclassified_result(task, scorer, hours, started)}
    import account_fleet
    import task_intake
    capabilities = account_fleet.load_capabilities()
    policy = account_fleet.load_accounts()['scoring']
    needs = dict(task.get('needs')) if isinstance(task.get('needs'), dict) else {}
    profile, classified, _, scorer, estimate, alerts = classify_task(
        task, needs, capabilities, policy, getattr(args, 'score_timeout', scorer_client.SCORER_TIMEOUT_SECONDS),
        use_model=not getattr(args, 'dry_run', False))
    if profile is None:
        return {'result': pinned_unclassified_result(task, scorer, hours, started)}
    checked = task_intake.validate_caller_profile(classified['profile'])
    classified = {**classified, 'profile': checked}
    if checked.get('multi_task') is True:
        keep_whole = (getattr(args, 'keep_whole', None) or '').strip()
        if not keep_whole:
            return {'result': split_result(classified, checked['task_count'], scorer, started)}
        alerts.append({'code': 'kept_whole', 'severity': 'info', 'account': None, 'reason': keep_whole[:300],
                       'task_count': checked['task_count'],
                       'message': f"The classifier found {checked['task_count']} separate tasks; the caller kept them "
                                  'as one assignment: ' + keep_whole[:300]})
    return {'task': classified, 'scorer': scorer, 'hours_estimate': estimate, 'alerts': alerts}


def plan_stage(document, args):
    """Classify, allocate and record one task; shared by the plan and roll stages."""
    task = document.get('task')
    if not isinstance(task, dict) or not task:
        raise ValueError('Input requires task.')
    if getattr(args, 'hours', None) is not None and task.get('expected_hours') is None:
        task = {**task, 'expected_hours': args.hours}
    if getattr(args, 'can_wait_hours', None) is not None and task.get('can_wait_hours') is None:
        cwh = args.can_wait_hours
        if not (isinstance(cwh, (int, float)) and not isinstance(cwh, bool) and math.isfinite(cwh) and cwh >= 0):
            raise ValueError('--can-wait-hours must be a non-negative number of hours.')
        task = {**task, 'can_wait_hours': float(cwh)}
    hours = task.get('expected_hours')
    if hours is None and task.get('hours_source') == 'classifier_estimate':
        pass  # The allocator budgets the classification's own expected_hours; roll reports it.
    elif not (isinstance(hours, (int, float)) and not isinstance(hours, bool) and math.isfinite(hours) and hours > 0):
        raise ValueError('Task requires expected_hours (positive number of active worker-hours, or pass --hours <n>).')
    if getattr(args, 'demo', False):
        if args.stage != 'plan' or not args.dry_run:
            raise ValueError('--demo requires plan --dry-run; remove --demo to use live providers.')
        from demo_plan import plan as reference_plan
        result = reference_plan({**document, 'task': task})
        return result, 0 if result.get('choice') != 'none' else 2
    reviewed_task = task
    started = time.monotonic()
    if not getattr(args, 'dry_run', False):
        import provider_retry
        try:
            # A worker the bridge stopped for a stuck provider retry is held before this plan, not after the next reconcile.
            provider_retry.ingest()
        except Exception:  # noqa: BLE001 - a hold that cannot be recorded now is recorded by the next observation
            pass
    now = datetime.now(timezone.utc)
    usage = usage_reading.usage_snapshot(args.usage_cache, None, now)
    unavailable = list(document['unavailable']) if isinstance(document.get('unavailable'), list) else []
    needs = dict(task.get('needs')) if isinstance(task.get('needs'), dict) else {}
    if getattr(args, 'fast', False):
        needs['fast'] = True
        task = {**task, 'needs': needs}
    import account_fleet
    import allocator
    import task_intake
    capabilities = account_fleet.load_capabilities()
    loaded_accounts = account_fleet.load_accounts()
    policy = loaded_accounts['scoring']
    guard_cfg = capabilities.get('remote_reroute_guard') or {}
    has_cadence = any(
        isinstance((spec or {}).get('cadence'), dict)
        for spec in (loaded_accounts.get('accounts') or {}).values()
    )
    need_ledger = (has_cadence
                   or bool((task.get('chain') or {}).get('id') if isinstance(task.get('chain'), dict) else False)
                   or bool(guard_cfg.get('enabled') is True and isinstance(guard_cfg.get('bundle_id'), str)
                           and guard_cfg.get('bundle_id') in unavailable))
    records = ((decision_ledger.read_ledger() if need_ledger else [])
               + dispatch_flow.reservation_records(read_only=not decision_ledger.LEDGER_WRITES))
    extra_alerts, scorer = [], None
    hours_estimate = None
    fatal_gap = bool((readiness_gap(loaded_accounts, usage) or {}).get('fatal'))
    should_score = not args.no_score and not fatal_gap and (
        args.score or task.get('auto_score')
        or (not args.dry_run and bool((task.get('objective') or '').strip()) and not isinstance(task.get('profile'), dict))
    )
    if not fatal_gap and not should_score and not isinstance(task.get('profile'), dict):
        return scorer_unavailable_result(
            task,
            {'source': 'scorer_unavailable', 'model': None, 'fallback_reason': 'no_score_without_profile',
             'fallback_detail': 'This run does not call the classifier (--no-score, or a dry run without --score), so it '
                                'requires a complete caller-supplied task.profile.'},
            started,
        ), 2
    if fatal_gap and not isinstance(task.get('profile'), dict):
        # No account can launch anything, so there is nothing to classify for: report that, with the way to set up.
        planned = {'stage': 'plan', 'choice': 'none', 'ranking': [], 'usage': usage, '_unavailable': unavailable,
                   'decision': {'action': 'none', 'needs_user_decision': True}}
        apply_readiness_gap(planned, loaded_accounts, usage)
        return decision_ledger.finalize(planned, None, 'auto', started, reviewed_task), 2
    if should_score:
        profile, task, needs, scorer, hours_estimate, alerts = classify_task(
            task, needs, capabilities, policy, args.score_timeout, use_model=bool(args.score) or not args.dry_run)
        if profile is None:
            return scorer_unavailable_result(task, scorer, started), 2
        extra_alerts.extend(alerts)
    task_profile = task_intake.validate_caller_profile(task['profile']) if task.get('profile') else {}
    if task_profile:
        task = {**task, 'profile': task_profile}
    is_ro_review = bool(task_profile.get('read_only_review'))
    if is_ro_review and isinstance(task.get('chain'), dict):
        chain_worker = task['chain'].get('worker')
        if isinstance(chain_worker, dict) and chain_worker.get('read_only_review') is False:
            task = {k: v for k, v in task.items() if k != 'chain'}
    if task_profile.get('multi_task') is True:
        count = task_profile['task_count']
        keep_whole = (getattr(args, 'keep_whole', None) or '').strip()
        if not keep_whole:
            return split_result(task, count, scorer, started), 2
        extra_alerts.append({'code': 'kept_whole', 'severity': 'info', 'account': None, 'reason': keep_whole[:300],
                             'task_count': count,
                             'message': f'The classifier found {count} separate tasks; the caller kept them as one '
                                        'assignment: ' + keep_whole[:300]})
    kind = task_intake.task_kind(task, capabilities, policy)
    guard = capabilities.get('remote_reroute_guard') or {}
    guarded_bundle = guard.get('bundle_id')
    locked_route = (recent_route_lock(task, records, now, guarded_bundle, guard.get('window_seconds', 600))
                    if guard.get('enabled') is True and isinstance(guarded_bundle, str) else None)
    if locked_route is not None and guarded_bundle in unavailable and not needs.get('local_only'):
        guarded = account_fleet.bundles(loaded_accounts).get(guarded_bundle) or {}
        probe_err = dot_unreachable() if guarded.get('provider') == 'dot' else 'no readiness probe'
        if probe_err is None:
            unavailable = [u for u in unavailable if u != guarded_bundle]
            extra_alerts.append({
                'code': 'dot_reroll_blocked', 'severity': 'warn', 'account': guarded.get('account'),
                'message': f'The effective local reroute policy retains the recent {guarded_bundle} route '
                           'while its adapter is healthy. Provide concrete local-runtime evidence or '
                           'continue the recorded route.'})
    wants_dot = task_intake.dot_may_take(kind, needs, capabilities, task.get('profile') if isinstance(task.get('profile'), dict) else None)
    dot_down = dot_unreachable() if wants_dot and 'dot_text' not in unavailable else None
    if dot_down:
        unavailable.append('dot_text')
    planned = allocator.plan(task, usage, now, unavailable, records)
    planned['_unavailable'] = unavailable
    apply_readiness_gap(planned, loaded_accounts, usage)
    if extra_alerts:
        planned.setdefault('alerts', []).extend(extra_alerts)
    if dot_down:
        planned.setdefault('alerts', []).append({
            'code': 'dot_unreachable', 'severity': 'info', 'account': 'dot',
            'message': f'Dot2API is not answering ({dot_down}); dot_text was left out of this plan. '
                       'Start it from the AgentCompass tray and plan again if Dot should do this work.'})
    if planned.get('decision', {}).get('action') == 'rotate':
        planned.setdefault('alerts', []).append({
            'code': 'worker_compaction_rotate', 'severity': 'warn', 'account': None,
            'message': f"The previous worker reached the compaction/context rotation limit; do not resume it. "
                       f"Start a fresh {planned.get('choice')} worker with a handoff summary."})
    if planned.get('choice') == 'dot_text':
        planned.setdefault('alerts', []).append({
            'code': 'dot_handoff', 'severity': 'info', 'account': 'dot',
            'message': 'Dot runs on its remote computer. One-command file workflow: '
                       '`python -X utf8 ~/.codex/skills/dot-subagent/scripts/run_dot_task.py run --prompt-file <task.md> --file <src1> --file <src2> --apply-to .` '
                       '(or `submit ...` + `status --task-id <id> --wait 120 --apply-to .` for non-blocking execution).'})
    if task.get('work_kind') and task['work_kind'] not in capabilities['routes']:
        planned.setdefault('alerts', []).append({
            'code': 'unknown_work_kind', 'severity': 'warn', 'account': None,
            'message': f"work_kind {task['work_kind']!r} is not a route, so the plan treated it as other_or_mixed. "
                       'Use one of: ' + ', '.join(capabilities['routes']) + '.'})
    if task.get('hours_source') == 'classifier_estimate':
        planned['hours_estimate'] = hours_estimate or {
            'hours': (planned.get('decision', {}).get('task') or {}).get('hours'), 'source': 'caller_profile',
            'confidence': task['profile'].get('hours_confidence', 'unknown')}
    if scorer is not None:
        planned['scorer'], planned['task_profile'] = scorer, task['profile']
    planned['_allocation_task'] = task     # carries the profile, so the launch-time re-plan scores alike
    result = decision_ledger.finalize(planned, None, 'auto', started, reviewed_task)
    return result, 0 if result.get('choice') != 'none' else 2


def scorer_cli(args):
    """`agentcompass scorer --action show|set|test`. Keys are never printed, only masked."""
    path = scorer_client.scorer_config_path()
    if args.action == 'set':
        if not args.scorer_type:
            raise ValueError('scorer set requires --type.')
        api_key = sys.stdin.readline().strip() if args.api_key == '-' else args.api_key
        scorer_client.save_scorer_config(args.scorer_type, api_key, args.base_url, args.model,
                           None if args.fallback_to_agy is None else args.fallback_to_agy == 'on', path)
        return {'stage': 'scorer', 'action': 'set', 'saved': True, **scorer_client.scorer_status(scorer_client.load_scorer_config(path), path)}, 0
    config = scorer_client.load_scorer_config(path)
    if args.action == 'show':
        return {'stage': 'scorer', 'action': 'show', **scorer_client.scorer_status(config, path)}, 0
    if args.scorer_type:
        config = {**config, 'scorer_type': args.scorer_type}
    result = scorer_client.scorer_test(config, args.objective, args.score_timeout)
    return result, 0 if result['ok'] else 1


def main():
    import account_fleet
    if sys.argv[1:2] == ['score'] and len(sys.argv) >= 3 and sys.argv[2] in ('set', 'list', 'clear'):
        import user_scores
        return user_scores.cli_main(sys.argv[2:])
    parser = argparse.ArgumentParser(prog='agentcompass', description=__doc__, add_help=False)
    parser.add_argument('stage', nargs='?', choices=tuple(stage_help.STAGES))
    parser.add_argument('-h', '--help', action='store_true', help='Show the options of this stage.')
    parser.add_argument('--help-all', action='store_true', help='Show every option the script accepts.')
    parser.add_argument('--brief', type=Path, action='append', help='roll: a Markdown task file (use instead of --task). It is classified and sent to the worker unchanged. Repeat it to roll several one-line briefs: they are classified at the same time, then routed and launched one after another.')
    parser.add_argument('--task', help='roll: the task as inline text, or - to read it from stdin (any length, kept whole); use instead of --brief. --hours is then optional.')
    parser.add_argument('--file', type=Path, action='append', default=[], help='roll: a file Dot needs (repeatable); ignored by local workers.')
    parser.add_argument('--apply-to', type=Path, help='roll: where returned Dot files land (default: cwd, only with --file).')
    parser.add_argument('--local-only', metavar='REASON', help='roll: keep the work on this machine when no --workspace is given, with the concrete local dependency.')
    parser.add_argument('--keep-whole', metavar='REASON', help='roll/plan: send a brief the classifier split into several lines of work to one worker anyway, with the concrete reason.')
    parser.add_argument('--hours', type=float, help='roll/plan: expected active worker-hours (positive number). Optional: when omitted, the classifier estimates it and says so under assumptions.')
    parser.add_argument('--can-wait-hours', type=float, metavar='HOURS', help='roll/plan: Optional. Hours this non-urgent task may wait for a stronger model\'s quota window to reset and, on roll, for a transient provider failure to clear before AgentCompass relaunches the brief on the next candidate (default 0: launch the best model available now).')
    parser.add_argument('--fast', action='store_true', help='roll/plan/record: prefer fast mode when quota is plentiful or during a pre-reset sprint.')
    parser.add_argument('--verbose', action='store_true', help='roll: print the full result instead of the short one (status, next_action, pointers). The full result is saved to disk either way.')
    parser.add_argument('--title', help='roll: worker title (default: first line of the brief).')
    parser.add_argument('--workspace', help='roll: existing directory this worker writes in and claims exclusively (default: parent cwd, warning only).')
    parser.add_argument('--task-id', help='roll: stable id for this task (default: derived from the brief).')
    parser.add_argument('--resume', metavar='AGENT_ID', help='roll: resume this specific idle worker owned by the parent session.')
    parser.add_argument('--new-worker', action='store_true', help='roll: always create a new dispatch and worker: no resume, and no handing back an open dispatch of the same brief.')
    parser.add_argument('--access', choices=('read-only', 'write'), help='roll: explicit override for whether this brief only reads or writes its workspace (default: classified by System-One, else write). Only a writer is held to one per --workspace.')
    parser.add_argument('--score', action='store_true',
                        help='plan: profile the task first with the System-One scorer (default on plan when objective is present).')
    parser.add_argument('--no-score', action='store_true',
                        help='plan: skip the System-One scorer model call and use only caller-supplied task fields/profile.')
    parser.add_argument('--score-timeout', type=int, default=scorer_client.SCORER_TIMEOUT_SECONDS,
                        help='score/plan/scorer: scorer time limit in seconds (1-120); a CLI process tree is stopped on timeout.')
    parser.add_argument('--action', choices=('show', 'set', 'test'), default='show',
                        help='scorer: show the System-One scorer config (keys masked), set it, or test it live.')
    parser.add_argument('--type', dest='scorer_type', choices=tuple(scorer_client.SCORER_TYPES),
                        help='scorer set: which small model scores tasks; scorer test: test this type instead of the saved one.')
    parser.add_argument('--api-key', help='scorer set: API key for jev/openai_compatible ("-" reads it from stdin, "" clears it).')
    parser.add_argument('--base-url', help='scorer set: API base URL for jev/openai_compatible ("" clears it).')
    parser.add_argument('--fallback-to-agy', choices=('on', 'off'), help='scorer set: fall back to agy when the chosen scorer fails.')
    parser.add_argument('--objective', help='scorer test: the sample task to classify.')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--dispatch-action', choices=('prepare', 'start', 'status', 'reconcile', 'release', 'fallback', 'settle'), default='prepare')
    parser.add_argument('--dispatch-id')
    parser.add_argument('--parent-id')
    parser.add_argument('--parent-host', choices=('auto', 'paseo', 'native', 'orca', 't3', 'herdr'), help='roll: host owning the parent session (default: auto-detected from environment)')
    parser.add_argument('--parent-address', help='roll: native host coordinator inbox address')
    parser.add_argument('--watch-seconds', type=float, default=3600)
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--usage-cache', type=Path, help='Read only this usage cache file (overrides ROUTER_USAGE_CACHE and the default locations).')
    parser.add_argument('--show', action='store_true', help='usage: print the merged usage windows (this is what the stage does; the flag only tells `agentcompass usage` not to collect).')
    parser.add_argument('--accounts', nargs='+', help='usage: exact account keys to inspect.')
    parser.add_argument('--judge', choices=('auto', 'sonnet', 'codex'), default='auto')
    parser.add_argument('--timeout', type=int, default=120, help='Bounded CLI timeout, in seconds.')
    parser.add_argument('--dry-run', action='store_true', help='delegate/select: print the reviewed payload without a model call. plan/record: compute the result but write no ledger row.')
    parser.add_argument('--demo', action='store_true', help='plan --dry-run: offline reference fleet and synthetic quotas; no local credentials needed.')
    parser.add_argument('--format', choices=('json', 'table'), help='Plan output (default: table for --demo, JSON otherwise).')
    parser.add_argument('--constraint', choices=CONSTRAINTS, help='record: why the route is forced (user_override, hard_constraint, dot_closed_loop).')
    parser.add_argument('--basis', help='record: quote the user instruction or constraint that forces this route.')
    parser.add_argument('--reason-category', choices=REASON_CATEGORIES,
                        help='record: why the override departs from the router: ' + ', '.join(REASON_CATEGORIES) + '.')
    parser.add_argument('--provider')
    parser.add_argument('--account')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--specialty')
    parser.add_argument('--trigger', choices=('workflow',),
                        help='record: the launch this record authorizes when it is not a worker roll starts; '
                             'workflow = a Claude Code Workflow the user asked for in this session (the spawn gate '
                             'accepts it only with --constraint user_override, from the same session).')
    parser.add_argument('--intent', help='override: stable intent key for idempotent updates (default: normalized user basis).')
    parser.add_argument('--window', help='override: window targeted by the reset expectation (default: weekly windows).')
    parser.add_argument('--resets-in', type=float, help='override: expected hours until reset; does not replace observed capacity.')
    parser.add_argument('--resets-at', help='override: expected ISO reset time; does not replace observed capacity.')
    parser.add_argument('--expires-in', type=float, help='override: hours until the subscription ends.')
    parser.add_argument('--expires-at', help='override: ISO time the subscription ends.')
    parser.add_argument('--boost', type=float, help='override: score added to the account (or --model) while it lasts; default 0.1.')
    parser.add_argument('--until', help='override: ISO time it lapses (default: the reset or expiry, else 24h).')
    parser.add_argument('--stop-on-reset', action='store_true', help='override: automatically stop and clear the override once usage telemetry shows the window reset has landed.')
    parser.add_argument('--reset-confirmation', choices=('cycle', 'replenishment'),
                        help='override: cycle requires an advanced reset date; replenishment explicitly accepts a one-off reset with the same reset date. Both require fresh observed quota recovery.')
    parser.add_argument('--rebaseline', action='store_true',
                        help='override: repair an existing intent baseline using the current fresh observation; never assign a new timestamp to old quota values.')
    parser.add_argument('--list', action='store_true', help='override: show the active overrides.')
    parser.add_argument('--clear', help='override: remove the override with this id, or "all".')
    args = parser.parse_args()
    if args.help_all:
        if args.stage:
            parser.prog = f'agentcompass {args.stage}'
        parser.print_help()
        return 0
    if args.help or args.stage is None:
        print(stage_help.stage_help(parser, args.stage) if args.stage else stage_help.overview())
        return 0 if args.help else 2
    decision_ledger.LEDGER_WRITES = not (args.dry_run and args.stage in ('plan', 'record', 'roll'))
    if args.stage in ('roll', 'plan', 'providers', 'select') and not args.demo:
        # This run keeps the reference set it starts with; a due refresh runs beside it and applies to the next run.
        public_data.refresh_in_background()
        antigravity_plan.refresh_in_background(public_data.state_dir())
    try:
        if args.demo and (args.stage != 'plan' or not args.dry_run):
            raise ValueError('--demo requires plan --dry-run; remove --demo to use live providers.')
        if not 1 <= args.timeout <= 600:
            raise ValueError('Timeout must be 1 to 600 seconds.')
        if not 1 <= args.score_timeout <= 120:
            raise ValueError('Score timeout must be 1 to 120 seconds.')
        if args.stage == 'scorer':
            result, code = scorer_cli(args)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return code
        if args.stage == 'score':
            if args.input is None:
                raise ValueError('score requires --input.')
            document = json.loads(args.input.read_text(encoding='utf-8-sig'))
            task = document.get('task')
            if not isinstance(task, dict) or not task:
                raise ValueError('Input requires task.')
            import task_intake
            capabilities = account_fleet.load_capabilities()
            policy = account_fleet.load_accounts()['scoring']
            profile, scorer = scorer_client.score_task(task, capabilities, args.score_timeout, use_model=not args.dry_run)
            if profile is None:
                result = {'stage': 'score', 'status': 'blocked', 'source': scorer['source'], 'task_profile': None,
                          'scorer': scorer, 'error': 'scorer_unavailable', 'next': SCORER_UNAVAILABLE_NEXT}
                print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
                return 2
            derived = task_intake.profile_difficulty(profile, policy)
            result = {'stage': 'score', 'source': scorer['source'], 'task_profile': profile, 'scorer': scorer,
                      'derived': {'kind': task_intake.task_kind({**task, 'profile': profile}, capabilities, policy),
                                  'difficulty': task.get('difficulty') or derived, 'profile_difficulty': derived}}
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.can_wait_hours is not None and (not math.isfinite(args.can_wait_hours) or args.can_wait_hours < 0):
            raise ValueError('--can-wait-hours must be a non-negative number of hours.')
        if args.stage == 'roll':
            briefs = args.brief or []
            if bool(briefs) == (args.task is not None):
                raise ValueError('roll requires exactly one of --task "<text>" (or --task - for stdin) or --brief <task.md>.')
            if len(briefs) > 1:
                result, code = roll.run_batch(args, sys.modules[__name__])
            else:
                args.brief = briefs[0] if briefs else None
                result, code = roll.run(args, sys.modules[__name__])
        elif args.stage in ('dispatch', 'launch'):
            if args.dry_run:
                raise ValueError('Dispatch/launch is a lifecycle operation; use an isolated fake host for tests')
            result = dispatch_flow.cli(args)
            code = 1 if result.get('state') in ('failed', 'create_failed') else 0
        elif args.stage == 'providers':
            accounts = account_fleet.load_accounts()
            result = {**accounts['_discovery'], 'host_policy_sync': accounts.get('_host_policy_sync'),
                      'public_data': public_data.status()}
            code = 0
        elif args.stage == 'usage':
            result = usage_reading.usage_snapshot(args.usage_cache, args.accounts)
            result = provider_inventory.merge_usage(result, account_fleet.load_accounts(), datetime.now(timezone.utc), args.accounts)
            code = 0 if result['read_ok'] else 1
        elif args.stage == 'override':
            result = quota_overrides.manage_overrides(args)
            code = 0
        elif args.stage == 'record':
            if args.constraint is None:
                raise ValueError('record requires --constraint.')
            result = record_constraint(args)
            code = 0
        elif args.stage == 'plan':
            if args.input is None and args.demo:
                from demo_plan import SAMPLE_DOCUMENT
                document = json.loads(json.dumps(SAMPLE_DOCUMENT))
            elif args.input is None:
                raise ValueError('plan requires --input.')
            else:
                document = json.loads(args.input.read_text(encoding='utf-8-sig'))
            result, code = plan_stage(document, args)
        else:
            if args.input is None:
                raise ValueError('Routing stages require --input.')
            document = json.loads(args.input.read_text(encoding='utf-8-sig'))
            payload = subscription_judge.build_request(args.stage, document, args.usage_cache)
            started = time.monotonic()
            if args.dry_run:
                result = payload
            else:
                result = decision_ledger.finalize(subscription_judge.evaluate(payload, args.judge, args.timeout), payload, args.judge,
                                  started, document.get('task'))
            code = 1 if result.get('source') == 'judge_unavailable' else 0
        if args.stage == 'plan' and (args.format == 'table' or (args.demo and args.format is None)):
            from demo_plan import format_table
            print(format_table(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, sqlite3.Error) as exc:
        failure = {'error': 'Routing input or local runtime failed', 'error_type': type(exc).__name__}
        if isinstance(exc, (ValueError, scorer_client.ScorerError)):
            # Only fixed texts written in this repository (caller input rules, scorer limits); other errors may carry paths or bodies.
            failure['detail'] = str(exc)[:400]
        failure['next'] = ('Fix the cause in detail, then roll again. A task that already has a Paseo dispatch comes back as that '
                           'worker (pending), not as a second launch.' if args.stage == 'roll' else
                           'Check reviewed input and usage cache; continue feasible parent work.')
        print(json.dumps(failure, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
