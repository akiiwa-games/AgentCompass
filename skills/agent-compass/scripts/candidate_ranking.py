"""Ranking and ordering of evaluated candidates."""


import host_policy
import launch_failures
import live_models
import model_latch
import provider_retry
import account_fleet
import axis_scores
import candidate_eval
import quota_economy


def five_axis_order(candidate):
    """Lexicographic order on the five-axis path (quota economy v3, owner decision of round 13b + R2-F4), in tuple order:
    1. Net score N = F - cost descending (exact float, never rounded: the quota economy C is a term of N, never a tie-break)
    2. Wait before the start ascending (a candidate that waits for a reset trails one that starts now at equal N)
    3. Faster completion time (wait_hours + expected_hours) ascending; speed breaks exact ties on N (including C ≈ 0 near reset)
    4. Fewer list-price USD spent ascending
    5. Demand-weighted category level descending (five_axis_level), then sibling harness place ascending."""
    k = candidate['components']
    wait_h = float(candidate.get('available_in_hours') or 0.0)
    done_key = round(wait_h + float(candidate['duration_hours']), 4)
    usd = float(candidate.get('expected_usd') if candidate.get('expected_usd') is not None else float('inf'))
    return (-float(candidate['score']), round(wait_h, 4), done_key,
            round(usd, 4), -round(float(k.get('five_axis_level', 0.0)), 4), k.get('harness_place', 0))


FIVE_AXIS_ORDER_KEYS = ('net_score', 'wait_hours', 'completion_hours', 'usd', 'axis_level', 'harness_place')


def five_axis_deciding_key(first, second):
    """The name of the first key of `five_axis_order` on which `first` beats `second` ('tie' when none does)."""
    for name, a, b in zip(FIVE_AXIS_ORDER_KEYS, five_axis_order(first), five_axis_order(second)):
        if a != b:
            return name
    return 'tie'


def candidate_for_bundle(rows, bundle_id, fast=False):
    """The ranking row for a bundle id and a fast flag: `<id>` is the plain variant, `<id>+fast` the fast one, and a fast
    flag with a plain id is the fast variant of that bundle. None when there is no such row."""
    for c in rows:
        if c['id'] == bundle_id and (not fast or c['bundle'].get('fast')):
            return c
    return next((c for c in rows if c.get('base_id') == bundle_id and bool(c['bundle'].get('fast')) == bool(fast)), None)


def rank(task, usage, policy, accounts, capabilities, now, unavailable=(), overrides=(), records=(),
         host_policy_config=None, *, read_local_state=True):
    state_policy = (host_policy.load_policy() if read_local_state else {'allowed_accounts': list((accounts.get('accounts') or {}).keys())}) if host_policy_config is None else host_policy_config
    ctx = {'policy': policy, 'accounts': accounts, 'capabilities': capabilities, 'usage': usage, 'now': now,
           'unavailable': set(unavailable), 'bundles': account_fleet.bundles(accounts),
           'host_policy': state_policy,
           'overrides': list(overrides), 'records': list(records or ()),
           'launch_failures': launch_failures.active() if (read_local_state and launch_failures.enabled()) else {},
           'model_latches': model_latch.active(now=now) if read_local_state else {},
           'provider_retries': provider_retry.active(now=now) if read_local_state else {},
           'live_models': live_models.load_or_refresh(
               accounts=accounts,
               allowed=lambda name: host_policy.account_allowed(state_policy, name),
               now=now,
           ) if read_local_state else {'accounts': {}}}
    pool_bundles = dict(ctx['bundles'])         # the catalog, before a task-named effort adds bundles to it
    ctx['bundles'].update(candidate_eval.named_effort_bundles(task, ctx))
    five = ctx['five_axis'] = axis_scores.five_axis_config(policy)
    demand = axis_scores.five_axis_demand(task)
    panel_targets = accounts['_five_axis']['targets']
    task = {**task, 'five_axis_demand': demand, 'five_axis_targets': axis_scores.five_axis_targets(demand, panel_targets),
            'five_axis_continuity': axis_scores.five_axis_continuity_value(demand, panel_targets)}
    candidates = [candidate_eval.evaluate_bundle(b, task, ctx) for b in ctx['bundles'].values()]
    # R2-F7: when `--model` is named without `--effort` (and no bundle is pinned), never let an unscored effort beat a
    # scored effort of the same model via a silent 0.0 default. Only when ALL eligible candidates lack trusted scores
    # do they proceed together as an explicit `unscored_named_dispatch` with `fit = None`.
    if task.get('constraint_model') and not task.get('constraint_effort') and not task.get('pinned_bundle'):
        elig_now = [c for c in candidates if not c['excluded']]
        if any(not c.get('_five_blind_msg') for c in elig_now):
            for c in elig_now:
                msg = c.get('_five_blind_msg')
                if msg:
                    blind_note = f'data error: {msg}; dispatched because it was named'
                    c['notes'] = [n for n in c['notes'] if n != blind_note]
                    if 'data_errors' in c:
                        c['data_errors'] = [e for e in c['data_errors'] if e != msg]
                        if not c['data_errors']:
                            c.pop('data_errors', None)
                    c['excluded'].append(
                        f'data error: {msg} (excluded because another effort of {task["constraint_model"]} has trusted data)'
                    )
    for c in candidates:
        if not c['excluded'] and c.get('_five_blind_msg'):
            c['components']['fit'] = None
            c['components']['unscored_named_dispatch'] = True
            c['unscored_named_dispatch'] = True
    eligible = [c for c in candidates if not c['excluded']]
    pool_task = {**task, 'local_only': False, 'outbound_ok': None, 'file_transfer': False,
                 'needs_visual': False, 'needs_gui': False, 'wants_fast': False,
                 'read_only_review': False, 'constraint_model': None, 'constraint_effort': None, 'pinned_bundle': None,
                 'metered_ok': True, 'metered_fallback': False, 'disable_fast': False, 'force_fast': False,
                 '_economy_pool': True}
    pool = [c for c in (candidate_eval.evaluate_bundle(b, pool_task, ctx) for b in pool_bundles.values()) if not c['excluded']]
    quota_windows = quota_economy.apply_five_axis_economy(candidates, eligible, task, accounts, policy, pool=pool)
    # Net score N = fit + continuity (both in panel standard deviations) + a temporary boost - the economy's cost (already
    # in score units). No normalization over the candidate set.
    weights = dict(five['weights'])
    kind = task['kind']
    for c in eligible:
        raw_score = (sum(weights[k] * (0.0 if c['components'].get(k) is None else c['components'][k]) for k in weights)
                     + c['components'].get('boost', 0)
                     - c['components']['opportunity_cost'])
        # Evaluate at IEEE-754 unit scale so F = 0 has the same ~2.2e-16 float64 resolution as F > 0 (shift invariance).
        c['score'] = float((1.0 + raw_score) - 1.0)
    # Equal scores go to the candidate with no wait, then faster completion, then fewer USD,
    # then higher level; the harness order only separates siblings of one model, account and effort.
    harness_places(eligible, kind, policy, capabilities, (task['profile'] or {}).get('interaction_style'))
    eligible.sort(key=five_axis_order)
    for i, c in enumerate(eligible):
        c['components']['decided_by'] = (five_axis_deciding_key(c, eligible[i + 1]) if i + 1 < len(eligible)
                                         else 'only_candidate')
    system = {'weights': {k: round(v, 3) for k, v in weights.items()}, '_live_models': ctx['live_models'],
              'quota_windows': quota_windows}
    return candidates, eligible, system


def sibling_key(candidate):
    """Bundles that differ only in harness (Codex or Pi running the same model on the same account)."""
    bundle = candidate['bundle']
    return bundle['account'], bundle['model'], bundle.get('effort')


def harness_group(candidate):
    """Siblings that would also do the same thing: resuming the owner is not the same choice as starting its twin."""
    return sibling_key(candidate) + (candidate['action'],)


def harness_places(eligible, kind, policy, capabilities, style=None):
    """Five-axis path: each candidate's place behind its best sibling (same account, model, effort and action) in the
    harness order `harness_places` computes, as `harness_place`; the score is not touched."""
    by_style = ((policy.get('profile') or {}).get('harness_by_style') or {}).get(style) if style else None
    order = (capabilities['routes'].get(kind) or {}).get('harness_order') or by_style or policy.get('harness_order') or []

    def place(candidate):
        b = candidate['bundle']
        harness = b.get('harness') or account_fleet.bundle_harness(b, b.get('account'), b.get('id'))
        return order.index(harness) if harness in order else order.index(b.get('provider')) if b.get('provider') in order else len(order)

    groups = {}
    for c in eligible:
        groups.setdefault(harness_group(c), []).append(c)
    for siblings in groups.values():
        best = min(place(c) for c in siblings)
        for c in siblings:
            c['components']['harness_place'] = place(c) - best
