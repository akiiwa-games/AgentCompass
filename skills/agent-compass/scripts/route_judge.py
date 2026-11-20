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
import caller_input
import cwd_policy
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
import state_upgrade
import subscription_judge
import usage_reading


CONSTRAINTS = ('user_override', 'hard_constraint', 'dot_closed_loop')
# Why an override departs from the router, so overrides can later be learned from as preferences: the task was
# classified wrongly, the router's pick is not good enough, it is stronger than needed, the chain should stay with
# its owner, the pick cannot run, or something else (say what in --basis).
REASON_CATEGORIES = ('misclassified', 'quality_insufficient', 'overqualified', 'continuity', 'unavailable', 'other')
# The constraint of the one record the spawn gate accepts for a Workflow (setup/router_gate_hook.check_workflow_record
# decides; `record` says it up front). Two holders of one rule: test_record_says_why drives the gate on the rows record
# writes, so they cannot drift apart.
WORKFLOW_TRIGGER, WORKFLOW_CONSTRAINT = 'workflow', 'user_override'


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

    A name matches over every bundle existing on the addressed account/provider (account_fleet.pin_pool ->
    pin_matching_bundles), before any disabled or effort filter: a bare name matches every bundle of its row (`row_key`),
    a prefixed `P/name` obeys `P` (falling back to identity only when `P` names nothing in the pool), and a family guess
    matches its own rows, never the flagship's. Disabled and effort filters then apply to the named bundles only. With no
    effort named, the model's default effort (else High) leads, not whichever row the document lists first."""
    import account_fleet
    import model_resolution
    if args.model not in (None, ''):
        model_resolution.check_model_pin(args.model, '--model')
    pool = account_fleet.pin_pool(accounts, account=args.account, provider=args.provider)
    named = account_fleet.pin_matching_bundles(args.model, pool, accounts)
    matches = [
        b for b in named
        if (include_disabled or not b.get('disabled'))
        and (args.effort is None or b.get('effort') == args.effort)
    ]
    if not matches:
        return None
    default = account_fleet.model_spec(accounts, matches[0]['model']).get('default_effort') or 'high'
    return next((b for b in matches if b.get('effort') == default), matches[0])['id']


NAMED_ROW_FIELDS = ('modeId', 'draws', 'launch_model', 'harness', 'billing')


def record_constraint(args, task=None, classified=None):
    """Log a route the caller is forcing. No judge is called; the allocator is run on the same task so the
    ledger shows whether the router would have chosen it and, if not, what the override cost.

    `classified` is classify_pinned's answer for a pinned roll: the allocator then plans the classified task, the
    pinned bundle carries that plan's price, and the launch is reserved from the same profile."""
    basis = (args.basis or '').strip()
    if refusal := roll.named_route_refusal(args, require=True):      # roll owns what a named route needs
        raise ValueError(refusal)
    if len(basis) > decision_ledger.BASIS_LIMIT:
        # A cut quote is not the user's quote: the ledger keeps the words whole or not at all.
        raise ValueError(f"--basis is {len(basis)} characters; a record keeps the user's words whole, up to "
                         f'{decision_ledger.BASIS_LIMIT}. Quote the words that name this route within that limit.')
    if getattr(args, 'trigger', None) == WORKFLOW_TRIGGER and args.constraint != WORKFLOW_CONSTRAINT:
        raise ValueError(f'--trigger {WORKFLOW_TRIGGER} is accepted by the spawn gate only on a record with --constraint '
                         f'{WORKFLOW_CONSTRAINT} (the user asked for the Workflow in this request, in their words in '
                         f'--basis); a {args.constraint} record does not authorize it. Record it with --constraint '
                         f'{WORKFLOW_CONSTRAINT} when the user asked for the Workflow, or drop --trigger.')
    import model_resolution
    model_resolution.check_model_pin(args.model, '--model')
    started = time.monotonic()
    task = _record_task(args, task)
    no_task = classified is None and not (set(task) - {'parent_agent_id'})
    import account_fleet
    import auto_bundles
    import axis_scores
    import model_resolution
    import provider_retry
    accounts = account_fleet.load_accounts()
    now = datetime.now(timezone.utc)
    usage = usage_reading.usage_snapshot(args.usage_cache, args.accounts, now)
    usage = provider_inventory.merge_usage(usage, accounts, now, args.accounts)
    unavailable = provider_inventory.unavailable_reason(accounts.get('_discovery') or {}, args.account, args.provider)
    if unavailable:
        raise ValueError(unavailable)
    if refusal := named_pair_refusal(accounts, args.account, args.provider):
        raise ValueError(refusal)
    # `model:effort` and `model-effort` name an effort; --effort, when given too, wins.
    raw_spelled, raw_effort = model_resolution.split_model_effort(args.model)
    pin = copy.copy(args)
    read_as = {}

    def _sync_pin_as_read(cat_doc):
        acct_pool = account_fleet.pin_pool(cat_doc, account=args.account, provider=args.provider)
        as_read = account_fleet.effective_pin(args.model, acct_pool, cat_doc)
        pin.model, named_effort = model_resolution.split_model_effort(as_read)
        served = account_fleet.account_providers(cat_doc, args.account) if '/' not in pin.model else ()
        res = account_fleet.resolve_model(pin.model, cat_doc, served)
        read_as.update(res)
        pin.effort = args.effort or named_effort or raw_effort or res['effort']
        return acct_pool, res['source'] in model_resolution.DIRECT_SOURCES

    acct_pool, direct = _sync_pin_as_read(accounts)
    own_rows = account_fleet.pin_matching_bundles(args.model, acct_pool, accounts)
    listed = None
    if not own_rows:
        # No row of this model on the account: the account's provider decides, from its live model list (read once, as
        # every named route reads it). What a name resembles is another model, never a substitute.
        import live_models
        live_doc = live_models.load_or_refresh([args.provider], named=(args.account,))
        rows, why = provider_inventory.listed_rows(
            accounts, args.account, args.provider, raw_spelled, pin.effort, live_doc)
        if rows is None and pin.model != raw_spelled:
            rows, why = provider_inventory.listed_rows(
                accounts, args.account, args.provider, pin.model, pin.effort, live_doc)
        if rows is None:
            raise ValueError(f"named_model_unavailable: {pin.model}{':' + pin.effort if pin.effort else ''} has no row on "
                             f"{args.account}/{args.provider}, and {why}. Tell the user; do not substitute another model.")
        listed = {'account': args.account, 'rows': rows}
        accounts = provider_inventory.with_listed_rows(accounts, listed)
        acct_pool, direct = _sync_pin_as_read(accounts)
        own_rows = account_fleet.pin_matching_bundles(args.model, acct_pool, accounts)
    accepted = {b.get('effort') for b in own_rows} | {
        e for b in own_rows for e in (account_fleet.dispatch_allowed_efforts(b, accounts) or ())}
    if pin.effort and pin.effort not in accepted:
        # The launch contract is the fact of what the harness accepts.
        raise ValueError(account_fleet.unaccepted_effort(account_fleet.bundle_harness(own_rows[0], args.account),
                                                         pin.model, pin.effort, accepted))
    chosen = match_bundle(accounts, pin)
    disabled_match = match_bundle(accounts, pin, include_disabled=True) if chosen is None else None
    # The model-management checkbox says which models the router chooses by itself, like host policy for accounts. The
    # user's own naming reaches an unchecked model (with a warning below); a project rule does not override the
    # user's checkbox. Off by default (superseded in public data) is not the user's checkbox: any naming reaches it.
    if disabled_match is not None:
        if (args.constraint == 'user_override'
                or account_fleet.bundles(accounts)[disabled_match].get('disabled_by_default')):
            chosen = disabled_match
        else:
            raise ValueError(_unchecked_refusal(args, pin.model))
    if (args.constraint != 'user_override' and own_rows
            and all(b.get('disabled') and not b.get('disabled_by_default') for b in own_rows)):
        # Every level unchecked is the model unchecked, also at a level with no row of its own.
        raise ValueError(_unchecked_refusal(args, pin.model))
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
        raise ValueError(cwd_policy.refusal(args.account, acc_spec.get('cwd_regex'), (task or {}).get('cwd'))
                         + '; a named route does not reach past it. Tell the user; do not substitute another account.')
    # External runners dispatch directly after record; apply the same registered
    # window protection used by prepared dispatches.
    base_b = account_fleet.bundles(accounts).get(chosen) or {}
    row_model = base_b.get('model') or sibling.get('model') or account_fleet.stored_form(pin.model, accounts, args.account)
    quota_bundle = {**discovered, **inherited, 'account': args.account, 'provider': args.provider, 'model': row_model,
                    'effort': pin.effort or discovered.get('effort')}
    if chosen:
        quota_bundle['id'] = chosen
        quota_bundle.setdefault('bundle_id', chosen)
    _check_record_launch(args, pin, quota_bundle, accounts, usage, now)
    unobserved = quota_unobserved(accounts, usage, quota_bundle)
    # A model the user names on a held account or model is the probe that can lift the hold: it goes, with a warning.
    retrying = provider_retry.reason(quota_bundle, provider_retry.active(now=now))
    c_model = account_fleet.canonical(row_model, accounts)
    fast_requested = bool(getattr(args, 'fast', False))
    if fast_requested:
        fast_modes.validate(account_fleet.bundle_harness(discovered or sibling or {'provider': args.provider}, args.account),
                                      c_model, account_fleet.row_spec(accounts, c_model))
    warnings = []
    if unobserved:
        warnings.append(unobserved)
    if args.effort and raw_effort and raw_effort != args.effort:
        warnings.append(f'--effort {args.effort} and --model {args.model} name different efforts: {args.effort} was used '
                        '(--effort wins over an effort spelled in --model).')
    effort = pin.effort or discovered.get('effort')
    reviewed_task = task
    if classified:
        # What is planned and reserved; the decision stays bound to the caller's task.
        task = {**classified['task'], **({'parent_agent_id': task['parent_agent_id']} if task.get('parent_agent_id') else {})}
    if effort is None and not sibling:
        effort = auto_bundles.HARNESS_DEFAULT_EFFORT.get(account_fleet.bundle_harness({'provider': args.provider, 'model': c_model}, args.account))
    bundle = {**inherited, **base_b, 'provider': args.provider, 'account': args.account, 'model': row_model,
              'effort': effort, **({'bundle_id': chosen} if chosen else {}), **({'fast': True} if fast_requested else {})}
    if not direct:
        bundle.setdefault('launch_model', pin.model)
    if read_as.get('key') and read_as['key'] != read_as.get('bare'):
        # A moving name, a preview name, an alias or a family guess: the row it read as is what launches.
        by = f" of provider {read_as['provider']}" if read_as.get('provider') else ''
        warnings.append(f"--model {raw_spelled} was read as the row {row_model} (model resolution: "
                        f"{read_as['source']}{by}); that row is what launches.")
    if no_task:
        warnings.append(NO_TASK_COMPARED)
    bundle['harness'] = account_fleet.bundle_harness(bundle, args.account, chosen)
    if 'modeId' not in bundle and bundle['harness'] in auto_bundles.HARNESS_MODE:
        bundle['modeId'] = auto_bundles.HARNESS_MODE[bundle['harness']]
    if fast_requested and bundle['harness'] == 'claude':
        bundle['fast_billing'] = fast_modes.claude_billing(accounts['accounts'][args.account])
    result = {'stage': 'record', 'choice': 'constraint', 'source': 'constraint_record',
              'constraint': args.constraint, 'basis': basis, 'specialty': args.specialty,
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
    # Host policy says which accounts the router chooses by itself, the model-management checkbox which models. Only
    # the user's own naming reaches past them; a project rule (hard_constraint) is shared between machines and does
    # not override one machine's switch. Account loading may enroll a newly discovered account; check the resulting
    # policy. The ledger marks the row (decision_ledger.automatic_routing_marker).
    policy = host_policy.load_policy()
    if args.constraint == 'user_override':
        warnings = [*(f'{text}; launched because the user named it in this request'
                      for text in host_policy.exclusion_texts(bundle, accounts, policy)), *warnings]
    elif not host_policy.account_allowed(policy, args.account):
        outside = host_policy.outside_routing(args.account)
        result['host_policy_violation'] = {
            'account': args.account, 'dispatch_allowed': False, 'policy_path': str(host_policy.policy_path()[0]),
            'reason': (f'{outside}; a {args.constraint} record does not override that, only a route the user named '
                       "in this request (--constraint user_override with the user's words in --basis)")}
        # Written all the same: the spawn gate reads this row and refuses a launch on it (check_route_authority).
        result.update(status='blocked', cause=outside, next_action=(
            f"Dispatch on {args.account} is not allowed: {result['host_policy_violation']['reason']}. Do not launch on "
            'it. If the user named this route in this request, record it again with --constraint user_override; '
            'otherwise roll without naming a route, and the router chooses among the accounts in automatic routing.'))
    if warnings:
        result['warnings'] = warnings
    if unobserved and not usage.get('read_ok'):
        result['alerts'] = [{'code': 'no_usage_data', 'severity': 'warn', 'account': args.account, 'message': unobserved}]
    if classified:
        result['_allocation_task'] = task
        if classified.get('scorer') is not None:
            result.update(scorer=classified['scorer'], task_profile=task.get('profile'))
        if classified.get('unclassified'):
            unclassified_data_error(result, classified['unclassified'], task['expected_hours'])
        if classified.get('hours_estimate'):
            result['hours_estimate'] = classified['hours_estimate']
        if classified.get('alerts'):
            result['alerts'] = [*result.get('alerts', []), *classified['alerts']]
    # What this record decides beyond the task: kept on the decision, passed to the plan as route fields
    # (task_intake.ROUTE_FIELDS), never written into a task.
    route = {**({'unclassified': classified['unclassified']} if (classified or {}).get('unclassified') else {}),
             **({'force_fast': True} if fast_requested else {})}
    if route:
        result['_route'] = route
    if fast_requested:
        # A verified fast contract with no cited price launches (candidate_eval), carrying that data error.
        fast_gap = fast_modes.price_gap(c_model, account_fleet.row_spec(overlaid_accounts, c_model))
        if fast_gap:
            result['data_errors'] = [*result.get('data_errors', []), fast_gap]
            result['warnings'] = [*result.get('warnings', []), f"data error: {fast_gap}; launched fast because the user "
                                  "named it, reserved at the plain mode's spend"]
    if listed:
        result['_listed_rows'] = listed     # issue and prepare plan with them (provider_inventory.with_listed_rows)
        listed_gap = (f'{args.account}/{pin.model} has no row in the shipped data: unscored and unpriced until the public '
                      'data has it')
        result['data_errors'] = [*result.get('data_errors', []), listed_gap]
        result.setdefault('warnings', []).append(
            f'data error: {listed_gap}; launched because the user named it and provider {args.provider} lists it')
    _compare_with_router(result, task, usage, now, chosen, fast_requested, classified)
    return decision_ledger.finalize(result, None, 'auto', started, reviewed_task if classified else task)


def record_resume(args, task, classified, bundle, agent_id):
    """`roll --resume <agent_id>`: the caller named a worker, so the route is that worker's own bundle, whatever an
    automatic plan would choose for the task (an account out of automatic routing included). The task is classified
    like any roll (`classify_pinned`) so the turn is priced and reserved; the facts about the worker (busy, worn, a
    refusing window, leftover meter fields, a model its account no longer offers) are decided where they live: roll's
    resume checks, the gate's turn check and `dispatch_registry.prepare`. Returns the finalized decision."""
    started = time.monotonic()
    import account_fleet
    accounts = account_fleet.load_accounts()
    now = datetime.now(timezone.utc)
    usage = usage_reading.usage_snapshot(args.usage_cache, args.accounts, now)
    usage = provider_inventory.merge_usage(usage, accounts, now, args.accounts)
    plan_task = {**classified['task'],
                 **({'parent_agent_id': task['parent_agent_id']} if task.get('parent_agent_id') else {})}
    chosen = bundle.get('bundle_id') or bundle.get('id')
    result = {'stage': 'record', 'choice': 'constraint', 'source': 'explicit_resume', 'constraint': 'explicit_resume',
              'basis': f'roll --resume {agent_id}', 'selected_bundle': dict(bundle), 'judge': None,
              'observed_at': now.isoformat(), 'usage': usage, '_allocation_task': plan_task}
    if classified.get('scorer') is not None:
        result.update(scorer=classified['scorer'], task_profile=plan_task.get('profile'))
    if classified.get('unclassified'):
        unclassified_data_error(result, classified['unclassified'], plan_task['expected_hours'])
        result['_route'] = {'unclassified': classified['unclassified']}
    if classified.get('hours_estimate'):
        result['hours_estimate'] = classified['hours_estimate']
    if classified.get('alerts'):
        result['alerts'] = list(classified['alerts'])
    warnings = [f'{text}; the turn goes to worker {agent_id!r} because --resume named it'
                for text in host_policy.exclusion_texts(bundle, accounts, host_policy.load_policy())]
    _compare_with_router(result, plan_task, usage, now, chosen, bool(bundle.get('fast')), classified)
    if result.get('agrees') is False and result.get('router_choice') not in (None, 'none'):
        warnings.append(f"The turn goes to worker {agent_id!r} on its existing bundle {chosen!r} as requested "
                        f"(--resume); the router would otherwise have chosen {result['router_choice']!r}.")
    if warnings:
        result['warnings'] = warnings
    return decision_ledger.finalize(result, None, 'auto', started, task)


def unclassified_data_error(result, reason, hours):
    """The data error of a named launch without a classifier profile, in the result and its ledger row."""
    error = (f"unclassified ({reason}): no task profile; priced and reserved from --hours "
             f"{float(hours):g} at the named effort")
    result['data_errors'] = [*result.get('data_errors', []), error]
    result['warnings'] = [*result.get('warnings', []), f'data error: {error}']


def _unchecked_refusal(args, model):
    return (f'{host_policy.unchecked_routing(args.account, model)}; a {args.constraint} record does not override that, '
            "only a route the user named in this request (--constraint user_override with the user's words in --basis)")


NO_TASK_COMPARED = ("No task was given (record reads one only from --input), so the route was not compared with the "
                    "router's own choice for it.")


def named_pair_refusal(accounts, account, provider):
    """Why `account` / `provider` names no route on this machine, before any model list is read; None when it does: an
    account the fleet does not have, or a provider the account does not launch through (`auto_bundles.launch_providers`,
    and the providers of its bundles)."""
    import account_fleet
    import auto_bundles
    specs = accounts.get('accounts') or {}
    spec = specs.get(account)
    if not isinstance(spec, dict):
        names = ', '.join(sorted(name for name, row in specs.items() if isinstance(row, dict)))
        return (f'account {account} is not configured on this machine (its accounts: {names}). Ask the user which one '
                'they meant; do not substitute another account.')
    served = auto_bundles.launch_providers(account, spec)
    if provider in served or account_fleet.pin_pool(accounts, account=account, provider=provider):
        return None
    return (f"account {account} does not launch through provider {provider} (it launches through {', '.join(served)}). "
            'Name the provider it launches through; do not substitute another account.')


def quota_unobserved(accounts, usage, bundle):
    """Which quota windows of a named launch's account were not read before it, in a sentence; None when every window
    that binds the bundle's model was read (or none binds it, or the account's allowance never blocks a launch). The
    windows are those `quota_pools.applicable_windows` finds for the model (window_scope): one the account never
    reports for that model is named, one that does not bind it is not. Not read is not exhausted: the launch goes ahead
    and says so."""
    import quota_pools
    account = bundle['account']
    spec = (accounts.get('accounts') or {}).get(account) or {}
    if spec.get('unmetered') or spec.get('five_axis_free_pool'):
        return None     # a purpose-limited channel or a free starter allowance never blocks a launch (launch_blockers)
    row = (usage.get('accounts') or {}).get(account) or {}
    fresh = bool(usage.get('read_ok')) and row.get('status') == 'fresh'
    windows = quota_pools.applicable_windows(spec, bundle.get('model'), row if fresh else {}, bundle=bundle,
                                             accounts=accounts)
    unread = [wid for wid, _, observed in windows if observed is None]
    if not unread:
        return None
    if not usage.get('read_ok'):
        why = (f'no usage snapshot was read (the collector has not produced one; `{router_command()} setup` starts '
               'it)')
    elif not fresh:
        why = f"{account} has no fresh usage reading ({', '.join(str(v) for v in (row.get('status') or 'no reading', row.get('reason')) if v)})"
    else:
        why = f'the fresh reading of {account} does not report {"it" if len(unread) == 1 else "them"}'
    return (f"quota not observed: {account} window{'s' if len(unread) > 1 else ''} {', '.join(unread)} "
            f"{'was' if len(unread) == 1 else 'were'} not read before this launch: {why}. Not read is not exhausted; "
            f"the launch went ahead without checking {'it' if len(unread) == 1 else 'them'}.")


def _record_task(args, task):
    """The task a record is for: the caller's, else the --input document's; parented to this host session when it
    names no parent."""
    import task_intake
    if task is None:
        task = {}
    document = not task and args.input is not None
    if document:
        loaded = json.loads(caller_input.read_text(args, args.input))
        task = loaded.get('task') if isinstance(loaded, dict) and isinstance(loaded.get('task'), dict) else {}
    task_intake.check_caller_task(task)
    if document:        # as plan --input: a document's local-only mark is the caller's, whatever flag it names
        task = task_intake.own_local_only_mark(task)
    if not task.get('parent_agent_id'):
        # The gate matches same-session rows first; a record without the session ID loses to any
        # roll this session made inside the window. Only real host IDs, never the ppid fallback.
        from worker_context import detect_host_session
        parent = (getattr(args, 'parent_id', None) or '').strip() or detect_host_session()
        parent = parent.removeprefix('claude:')
        if parent and not parent.startswith(('claude-session-', 'codex-session-')):
            task = {**task, 'parent_agent_id': parent}
    return task


def _check_record_launch(args, pin, quota_bundle, accounts, usage, now):
    """Refuse a forced route that cannot launch now: no quota left in a window it draws, a launch cooling down, or a
    latched model. External runners dispatch directly after record, so this is the protection prepared dispatches get."""
    import candidate_eval
    import launch_failures
    import model_latch
    import quota_pools
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


def _compare_with_router(result, task, usage, now, chosen, fast_requested, classified):
    """Add to a record what the router would have chosen for the same task, and what the override cost."""
    import account_fleet
    import allocator
    import candidate_ranking
    if task and not isinstance(task.get('profile'), dict):
        # record / roll --provider never calls the classifier, and the router plans only a classified task: with no
        # caller profile there is nothing to compare the user's choice with.
        result.update({'router_choice': None, 'agrees': None, 'classifier_skipped': True})
    elif task:
        try:
            has_cadence = any(
                isinstance((spec or {}).get('cadence'), dict)
                for spec in (account_fleet.load_accounts().get('accounts') or {}).values()
            )
            plan_records = (dispatch_flow.reservation_records(read_only=not decision_ledger.LEDGER_WRITES)
                            if not ((task.get('chain') or {}).get('id') or has_cadence) else
                            decision_ledger.read_ledger() + dispatch_flow.reservation_records(read_only=not decision_ledger.LEDGER_WRITES))
            plan = allocator.plan(task, usage, now, records=plan_records,
                                  route={'force_fast': True} if fast_requested else None)
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


def _with_effort(pin, effort):
    other = copy.copy(pin)
    other.effort = effort
    return other




def router_command():
    """The public entry as this install spells it (entry.py)."""
    return entry.command()


ONBOARD_HINT = (f'Run `{router_command()} setup` to detect the CLIs you are signed in to and start usage collection.')
NO_USAGE_DATA = ('No usage data is available (the collector has not produced a snapshot), so quota-aware ranking is '
                 'running blind and the pick is a default, not a measured choice. ' + ONBOARD_HINT)


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
        return {'code': 'no_usage_data', 'fatal': False, 'message': NO_USAGE_DATA}
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
    classified_kind = task_intake.profile_kind(profile)
    derived = task_intake.profile_difficulty(profile, policy)
    order = list(task_intake.DIFFICULTY_ORDER)
    if task.get('difficulty') in order and order.index(task['difficulty']) > order.index(derived):
        alerts.append({
            'code': 'difficulty_above_profile', 'severity': 'info', 'account': None,
            'message': f"The caller's difficulty {task['difficulty']!r} is above the classifier profile's {derived!r} "
                       f"(primary_level {profile['primary_level']}); the classifier's {derived!r} was used. "
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
        needs = {**needs, 'local_only': True, 'local_only_by': 'classifier',
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
            hours_estimate = {'hours': profile['expected_hours'], 'source': 'classifier_estimate'}
    return profile, task, needs, scorer, hours_estimate, alerts


def unclassified_reason(scorer):
    return (scorer or {}).get('fallback_detail') or (scorer or {}).get('fallback_reason') or 'scorer_unavailable'


def pinned_unclassified_result(task, scorer, started):
    """A pinned roll with no task profile and no --hours: there is nothing to price or reserve the launch from, and
    AgentCompass does not invent an hours figure. With --hours the named route launches unclassified (classify_pinned)."""
    reason = (f'Pinned roll: the System-One classifier is unavailable ({unclassified_reason(scorer)}) and --hours was not '
              'given, so there is nothing to price or reserve the launch from, and AgentCompass does not invent an '
              'estimate; nothing was launched.')
    result = scorer_unavailable_result(task, scorer, started)
    result.update(reason=reason, decision=dict(result['decision'], reason=reason), pinned=True,
                  next='Roll it again with --hours <n> (your estimate of the work) to launch the named route priced '
                       'from those hours, unclassified; or roll again when the classifier answers '
                       '(`agentcompass scorer --action test` checks it).')
    return result


def _unclassified(task, scorer, hours, started):
    """classify_pinned's answer when no classifier profile exists: with --hours the named route launches, planned
    without a profile (task_intake `unclassified`: no category demand, hours from --hours at the named effort); without
    --hours it stops (pinned_unclassified_result)."""
    if hours is None:
        return {'result': pinned_unclassified_result(task, scorer, started)}
    return {'task': task, 'unclassified': unclassified_reason(scorer), 'scorer': scorer, 'hours_estimate': None,
            'alerts': []}


def classify_pinned(task, args):
    """`roll --provider/--account/--model`: only the model choice is pinned. The task gets the same System-One profile
    and hours estimate as an automatic roll, so the pinned bundle is priced and reserved like any other launch.

    Returns {'task', 'scorer', 'hours_estimate', 'alerts'} (and 'unclassified', the reason, when no classifier answered
    and --hours was given); or {'result': ...} when the roll stops here (split, or no profile to price with)."""
    started = time.monotonic()
    hours = task.get('expected_hours')
    if isinstance(task.get('profile'), dict):
        estimate = None
        if hours is None and task.get('hours_source') == 'classifier_estimate':
            given = task['profile'].get('expected_hours')
            estimate = {'hours': given, 'source': 'caller_profile'} if given else None
        return {'task': task, 'scorer': None, 'hours_estimate': estimate, 'alerts': []}
    if getattr(args, 'dry_run', False) and not getattr(args, 'score', False):
        # A preview launches and reserves nothing, so it needs no price: the pin is checked, the classifier is not called.
        return {'task': task, 'scorer': None, 'hours_estimate': None, 'alerts': []}
    if getattr(args, 'no_score', False):
        scorer = {'source': 'scorer_unavailable', 'model': None, 'fallback_reason': 'no_score_without_profile',
                  'fallback_detail': '--no-score was given and the task carries no profile'}
        return _unclassified(task, scorer, hours, started)
    import account_fleet
    import task_intake
    capabilities = account_fleet.load_capabilities()
    policy = account_fleet.load_accounts()['scoring']
    needs = dict(task.get('needs')) if isinstance(task.get('needs'), dict) else {}
    profile, classified, _, scorer, estimate, alerts = classify_task(
        task, needs, capabilities, policy, getattr(args, 'score_timeout', scorer_client.SCORER_TIMEOUT_SECONDS),
        use_model=not getattr(args, 'dry_run', False))
    if profile is None:
        return _unclassified(task, scorer, hours, started)
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
    task = _plan_input_task(document, args)
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
    kind = task_intake.task_kind(task, capabilities)
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
    _add_plan_alerts(planned, task, capabilities, dot_down)
    reference_alert = public_data.reference_alert()
    if reference_alert:
        planned.setdefault('alerts', []).append(reference_alert)
    if task.get('hours_source') == 'classifier_estimate':
        planned['hours_estimate'] = hours_estimate or {
            'hours': (planned.get('decision', {}).get('task') or {}).get('hours'), 'source': 'caller_profile'}
    if scorer is not None:
        planned['scorer'], planned['task_profile'] = scorer, task['profile']
    planned['_allocation_task'] = task     # carries the profile, so the launch-time re-plan scores alike
    result = decision_ledger.finalize(planned, None, 'auto', started, reviewed_task)
    return result, 0 if result.get('choice') != 'none' else 2


def _plan_input_task(document, args):
    """The document's task with --hours and --can-wait-hours applied where it gives none; it must carry its hours unless
    the classifier is to estimate them."""
    task = document.get('task')
    if not isinstance(task, dict) or not task:
        raise ValueError('Input requires task.')
    import task_intake
    task_intake.check_caller_task(task)
    if getattr(args, 'stage', None) != 'roll':      # roll.build_task wrote its own token from the flag it read
        task = task_intake.own_local_only_mark(task)
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
    return task


def _add_plan_alerts(planned, task, capabilities, dot_down):
    """The plan's alerts about Dot, worker rotation and an unknown work_kind."""
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


def scorer_cli(args):
    """`agentcompass scorer --action show|set|test`. Keys are never printed, only masked."""
    path = scorer_client.scorer_config_path()
    if args.action == 'set':
        if not args.scorer_type:
            raise ValueError('scorer set requires --type.')
        api_key = caller_input.stdin_line(args).strip() if args.api_key == '-' else args.api_key
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


def score_stage(args):
    """`agentcompass score --input task.json`: the System-One profile of one task, without allocating it."""
    import account_fleet
    if args.input is None:
        raise ValueError('score requires --input.')
    document = json.loads(caller_input.read_text(args, args.input))
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
        return result, 2
    derived = task_intake.profile_difficulty(profile, policy)
    result = {'stage': 'score', 'source': scorer['source'], 'task_profile': profile, 'scorer': scorer,
              'derived': {'kind': task_intake.task_kind({**task, 'profile': profile}, capabilities),
                          'difficulty': task.get('difficulty') or derived, 'profile_difficulty': derived}}
    return result, 0


def build_parser():
    """The one flat option set every stage accepts (stage_help shows each stage its own)."""
    parser = argparse.ArgumentParser(prog='agentcompass', description=__doc__, add_help=False)
    parser.add_argument('stage', nargs='?', choices=tuple(stage_help.STAGES))
    parser.add_argument('-h', '--help', action='store_true', help='Show the options of this stage.')
    parser.add_argument('--help-all', action='store_true', help='Show every option the script accepts.')
    parser.add_argument('--brief', type=Path, action='append', help='roll: a Markdown task file (use instead of --task). It is classified and sent to the worker unchanged. Repeat it to roll several one-line briefs: they are classified at the same time, then routed and launched one after another.')
    parser.add_argument('--task', help='roll: the task as inline text, or - to read it from stdin (any length, kept whole); use instead of --brief. --hours is then optional.')
    parser.add_argument('--file', type=Path, action='append', default=[], help='roll: a file Dot needs (repeatable); a local worker does not receive it, and the result says so.')
    parser.add_argument('--apply-to', type=Path, help='roll: where returned Dot files land (default: cwd, only with --file).')
    parser.add_argument('--local-only', metavar='REASON', help='roll: keep the work on this machine when no --workspace is given, with the concrete local dependency.')
    parser.add_argument('--keep-whole', metavar='REASON', help='roll/plan: send a brief the classifier split into several lines of work to one worker anyway, with the concrete reason.')
    parser.add_argument('--hours', type=float, help='roll/plan: expected active worker-hours (positive number). Optional: when omitted, the classifier estimates it and says so under assumptions.')
    parser.add_argument('--can-wait-hours', type=float, metavar='HOURS', help='roll/plan: Optional. Hours this non-urgent task may wait for a stronger model\'s quota window to reset and, on roll, for a transient provider failure to clear before AgentCompass relaunches the brief on the next candidate (default 0: launch the best model available now).')
    parser.add_argument('--fast', action='store_true', help='roll/plan/record: prefer fast mode when quota is plentiful or during a pre-reset sprint; a result that did not apply it says why.')
    parser.add_argument('--verbose', action='store_true', help='roll: print the full result instead of the short one (status, next_action, pointers). The full result is saved to disk either way.')
    parser.add_argument('--title', help='roll: worker title (default: first line of the brief).')
    parser.add_argument('--workspace', help='roll: existing directory this worker writes in and claims exclusively (default: parent cwd, warning only). It makes the work local-only, so remote runners are not considered (the answer says so).')
    parser.add_argument('--task-id', help='roll: stable id for this task (default: derived from the brief).')
    parser.add_argument('--resume', metavar='AGENT_ID', help='roll: resume this specific idle worker owned by the parent session, on its own route and in its own directory; when it cannot take the turn, nothing else is started.')
    parser.add_argument('--new-worker', action='store_true', help='roll: always create a new dispatch and worker: no resume, and no handing back an open dispatch of the same brief.')
    parser.add_argument('--access', choices=('read-only', 'write'), help='roll: explicit override for whether this brief only reads or writes its workspace (default: classified by System-One, else write). Only a writer is held to one per --workspace.')
    parser.add_argument('--score', action='store_true',
                        help='plan/roll: profile the task first with the System-One scorer (default when objective is present; with --dry-run only when given).')
    parser.add_argument('--no-score', action='store_true',
                        help='plan/roll: skip the System-One scorer model call and use only caller-supplied task fields/profile.')
    parser.add_argument('--score-timeout', type=int, default=scorer_client.SCORER_TIMEOUT_SECONDS,
                        help='score/plan/roll/scorer: scorer time limit in seconds (1-120); a CLI process tree is stopped on timeout.')
    parser.add_argument('--action', choices=('show', 'set', 'test'), default='show',
                        help='scorer: show the System-One scorer config (keys masked), set it, or test it live.')
    parser.add_argument('--type', dest='scorer_type', choices=tuple(scorer_client.SCORER_TYPES),
                        help='scorer set: which small model scores tasks; scorer test: test this type instead of the saved one.')
    parser.add_argument('--api-key', help='scorer set: API key for jev/gemini_decision/openai_compatible ("-" reads it from stdin, "" clears it).')
    parser.add_argument('--base-url', help='scorer set: API base URL for jev/gemini_decision/openai_compatible ("" clears it).')
    parser.add_argument('--fallback-to-agy', choices=('on', 'off'), help='scorer set: fall back to agy when the chosen scorer fails.')
    parser.add_argument('--objective', help='scorer test: the sample task to classify.')
    parser.add_argument('--input', type=Path, help='plan/score/delegate/select/dispatch prepare: the input JSON document. '
                        'record: a task document ({"task": ...}), to compare the forced route with the router\'s own choice.')
    parser.add_argument('--dispatch-action', choices=('prepare', 'start', 'status', 'reconcile', 'release', 'fallback', 'settle'), default='prepare')
    parser.add_argument('--dispatch-id')
    parser.add_argument('--parent-id', help='roll/record/launch/dispatch release: the parent session id (default for roll and '
                        'record: detected from the host session).')
    parser.add_argument('--parent-host', choices=('auto', 'paseo', 'native', 'orca', 't3', 'herdr'), help='roll: host owning the parent session (default: auto-detected from environment)')
    parser.add_argument('--parent-address', help='roll: native host coordinator inbox address')
    parser.add_argument('--watch-seconds', type=float, default=3600)
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--usage-cache', type=Path, help='Read only this usage cache file (overrides ROUTER_USAGE_CACHE and the default locations).')
    parser.add_argument('--show', action='store_true', help='usage: print the merged usage windows (this is what the stage does; the flag only tells `agentcompass usage` not to collect).')
    parser.add_argument('--accounts', nargs='+', help='usage: exact account keys to inspect. record (and a roll that names its route): read usage of only these '
                        'accounts; an account left out has no window read, and the result says so.')
    parser.add_argument('--judge', choices=('auto', 'sonnet', 'codex'), default='auto')
    parser.add_argument('--timeout', type=int, default=120, help='Bounded CLI timeout, in seconds.')
    parser.add_argument('--dry-run', action='store_true', help='delegate/select: print the reviewed payload without a model call. plan/record: compute the result but write no ledger row. roll: record and launch nothing.')
    parser.add_argument('--demo', action='store_true', help='plan --dry-run: offline reference fleet and synthetic quotas; no local credentials needed.')
    parser.add_argument('--format', choices=('json', 'table'), help='Plan output (default: table for --demo, JSON otherwise).')
    parser.add_argument('--constraint', choices=CONSTRAINTS, help='record: why the route is forced (user_override, hard_constraint, dot_closed_loop).')
    parser.add_argument('--basis', help='record/roll: quote the user instruction or constraint that forces this route; roll needs it with --provider, --account, --model or --effort, and ignores it (with a warning) without them.')
    parser.add_argument('--reason-category', choices=REASON_CATEGORIES,
                        help='record (and a roll that names its route): why the override departs from the router: ' + ', '.join(REASON_CATEGORIES) + '.')
    parser.add_argument('--provider')
    parser.add_argument('--account')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--specialty', help='record (and a roll that names its route): a free-text label kept on the decision and its ledger row.')
    parser.add_argument('--trigger', choices=(WORKFLOW_TRIGGER,),
                        help='record (and a roll that names its route, which records it): the launch this record '
                             'authorizes when it is not a worker roll starts; workflow = a Claude Code Workflow the user '
                             'asked for in this request (the spawn gate accepts it only on a record of the route the user '
                             'named, from the same session).')
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
    return parser


def render(result, table=False):
    """The text a stage prints: the plan table, or strict JSON (no NaN or Infinity). The acceptance of a reference set
    serializes its offline plans with this too (public_data.accept)."""
    if table:
        from demo_plan import format_table
        return format_table(result)
    return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    if sys.argv[1:2] == ['score'] and len(sys.argv) >= 3 and sys.argv[2] in ('set', 'list', 'clear', 'suggest'):
        import user_scores
        return user_scores.cli_main(sys.argv[2:])
    parser = build_parser()
    args = parser.parse_args()
    if args.help_all:
        if args.stage:
            parser.prog = f'agentcompass {args.stage}'
        parser.print_help()
        return 0
    if args.help or args.stage is None:
        print(stage_help.stage_help(parser, args.stage) if args.stage else stage_help.overview())
        return 0 if args.help else 2
    if args.stage in ('roll', 'plan', 'providers', 'select') and not args.demo:
        # This run keeps the reference set it takes; a due check runs in its own process and applies to the next run.
        public_data.refresh_in_background()
        antigravity_plan.refresh_in_background(state_upgrade.state_dir())
    try:
        # The caller's input (stdin, the --input and --brief files) is read once, here, before the part that may run
        # again; a re-run receives what was read (caller_input). A reader failing on a fetched reference set with a
        # data error before this run had an effect (a ledger row, a reservation, a launch: public_data.effect) runs the
        # stage again on the shipped set; after an effect, for a refusal or an environment failure, the error is this
        # run's answer (public_data.with_fallback).
        caller = caller_input.capture(args)
        return public_data.with_fallback(lambda: _run_stage(caller_input.attach(build_parser().parse_args(), caller)))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, *public_data.DATA_ERRORS) as exc:
        # Every failure, a data-class error (KeyError, TypeError, ...) included, is reported as the named error, never
        # as a traceback.
        print(json.dumps(failure_answer(exc, args.stage), ensure_ascii=False))
        return 1


def failure_cause(exc):
    """What a failed run says about its error: the text only where this repository wrote it or the system did (caller
    input rules, scorer limits, the operating system's reason and the path that was asked for, a database's own message,
    the name of a missing data field). A RuntimeError's text may carry the reply of a host, so only its type is named."""
    kind = type(exc).__name__
    if isinstance(exc, (ValueError, scorer_client.ScorerError)):
        return str(exc)[:400]
    if isinstance(exc, OSError):
        named = f': {exc.filename}' if getattr(exc, 'filename', None) else ''
        return f'{kind}: {exc.strerror or "operating system error"}{named}'[:400]
    if isinstance(exc, sqlite3.Error):
        return f'{kind}: {exc}'[:400]
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        return f'{kind}: the field {exc.args[0][:80]!r} is missing from reference or local data'
    return (f'{kind}: its text is not printed here (it may carry the reply of a host); run `agentcompass doctor` to check '
            'the installation and local state')


def failure_answer(exc, stage):
    """The answer of a run that failed outside the result path, in the shape of every other answer: `status`, `cause`,
    `next_action`, and the error class (`error`, `error_type`)."""
    failure = {'status': 'error', 'error': 'Routing input or local runtime failed', 'error_type': type(exc).__name__,
               'cause': failure_cause(exc)}
    if isinstance(exc, (ValueError, scorer_client.ScorerError)):
        failure['detail'] = failure['cause']        # the text of a rule written in this repository, as it always was
    elif public_data.is_data_error(exc):
        failure['error'] = 'Reference or local data could not be read'
    if isinstance(exc, (ValueError, scorer_client.ScorerError)):
        # A rule of this repository refused the request as given: what to change is in the flags or the brief.
        fix = 'The request was refused as given. Change what cause names (a flag, the brief or a value in it), then'
    else:
        fix = 'Fix the cause named in cause (a missing or unreadable file, the local state, the installation), then'
    failure['next_action'] = (
        f'{fix} roll again. A task that already has a Paseo dispatch comes back as that worker (pending), not as a second launch.'
        if stage == 'roll' else
        f'{fix} check the reviewed input and the usage cache, and continue feasible parent work.')
    return failure


ALWAYS_READ = ('stage', 'help', 'help_all')     # what every stage reads: which stage, and whether to show its help
# What a roll reads only when it records a route (the flags `record_constraint` reads beside the route itself), and of
# those, what `record_resume` reads for `--resume`: a roll that takes another path does nothing with them.
ROUTE_RECORD_FLAGS = ('trigger', 'specialty', 'reason_category', 'accounts')
RESUME_RECORD_FLAGS = ('accounts',)


def given_flags(args):
    """The parser actions of the flags the caller gave (a value other than the parser's default), in the parser's order."""
    return [action for action in build_parser()._actions
            if action.option_strings and getattr(args, action.dest, action.default) != action.default]


def unread_flags(args):
    """The flags the caller gave that `args.stage` does not read, as spelled on the command line, in the parser's order.
    What a stage reads is its row of `stage_help.STAGES`."""
    summary, usage, basic, extra = stage_help.STAGES[args.stage]
    listed = {*basic, *extra, *ALWAYS_READ}
    return [action.option_strings[0] for action in given_flags(args) if action.dest not in listed]


def unread_flag_note(stage, flags):
    """The note a result carries for flags its stage did not read (a note, not a refusal: the stage ran without them)."""
    return (f"{', '.join(flags)} {'is' if len(flags) == 1 else 'are'} not read by {stage} and had no effect "
            f'(`{router_command()} {stage} -h` lists what {stage} reads).')


def roll_path_notes(args):
    """What a roll that records no route did not do with the flags only a recorded route reads (ROUTE_RECORD_FLAGS),
    from the path this roll takes: `--resume` records the worker's own route (`record_resume`), a named route records
    it (`record_constraint`), any other roll is planned and records nothing of the kind. Read before the roll runs."""
    if roll.named_route_given(args) and not (getattr(args, 'resume', None) or '').strip():
        return []
    reads = RESUME_RECORD_FLAGS if (getattr(args, 'resume', None) or '').strip() else ()
    unused = [action for action in given_flags(args) if action.dest in ROUTE_RECORD_FLAGS and action.dest not in reads]
    if not unused:
        return []
    path = "a roll with --resume records the worker's own route" if reads else 'this roll names no route'
    note = (f"{', '.join(a.option_strings[0] for a in unused)} had no effect: roll reads "
            f"{'it' if len(unused) == 1 else 'them'} only for a route it records (--provider, --account, --model and "
            f'--basis), and {path}.')
    if any(a.dest == 'trigger' for a in unused):
        note += f' No record that authorizes a Workflow was written: --trigger {WORKFLOW_TRIGGER} needs `record`.'
    return [note]


def _run_stage(args):
    """One stage, from freshly parsed arguments (a re-run gets its own). The checks every stage shares come first; the
    stage itself runs in `_stage_result`, and its answer gets here the notes of what it did not read, for every stage."""
    notes = []
    if args.stage in stage_help.STAGES:
        if unread := unread_flags(args):
            notes.append(unread_flag_note(args.stage, unread))
        if args.stage == 'roll':
            notes.extend(roll_path_notes(args))
    if args.demo and (args.stage != 'plan' or not args.dry_run):
        raise ValueError('--demo requires plan --dry-run; remove --demo to use live providers.')
    if not 1 <= args.timeout <= 600:
        raise ValueError('Timeout must be 1 to 600 seconds.')
    if not 1 <= args.score_timeout <= 120:
        raise ValueError('Score timeout must be 1 to 120 seconds.')
    if (args.stage not in ('scorer', 'score') and args.can_wait_hours is not None
            and (not math.isfinite(args.can_wait_hours) or args.can_wait_hours < 0)):
        raise ValueError('--can-wait-hours must be a non-negative number of hours.')
    if args.stage in ('dispatch', 'launch') and args.dry_run:
        raise ValueError('Dispatch/launch is a lifecycle operation; use an isolated fake host for tests')
    result, code, table = _stage_result(args)
    if notes and isinstance(result, dict):
        result['warnings'] = [*(result.get('warnings') or []), *notes]
    print(render(result, table=table))
    return code


def _stage_result(args):
    """(result, exit code, whether to print it as the plan table) of the stage `args.stage`; prints nothing."""
    import account_fleet
    decision_ledger.LEDGER_WRITES = not (args.dry_run and args.stage in ('plan', 'record', 'roll'))
    if args.stage == 'scorer':
        return (*scorer_cli(args), False)
    if args.stage == 'score':
        return (*score_stage(args), False)
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
        # A record the host's switches do not let launch is still written (the spawn gate reads it and refuses the
        # launch); its answer says so, and the exit is not success.
        code = 2 if result.get('status') == 'blocked' else 0
    elif args.stage == 'plan':
        if args.input is None and args.demo:
            from demo_plan import sample_document
            document = sample_document()
        elif args.input is None:
            raise ValueError('plan requires --input.')
        else:
            document = json.loads(caller_input.read_text(args, args.input))
        result, code = plan_stage(document, args)
    else:
        if args.input is None:
            raise ValueError('Routing stages require --input.')
        document = json.loads(caller_input.read_text(args, args.input))
        payload = subscription_judge.build_request(args.stage, document, args.usage_cache)
        started = time.monotonic()
        if args.dry_run:
            result = payload
        else:
            result = decision_ledger.finalize(subscription_judge.evaluate(payload, args.judge, args.timeout), payload, args.judge,
                              started, document.get('task'))
        code = 1 if result.get('source') == 'judge_unavailable' else 0
    return result, code, args.stage == 'plan' and (args.format == 'table' or (args.demo and args.format is None))


if __name__ == '__main__':
    raise SystemExit(main())
