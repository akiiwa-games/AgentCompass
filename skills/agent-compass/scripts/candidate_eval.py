"""Candidate evaluation: one bundle against one task, its gates, exclusions (rule B) and breakdown."""


import fast_modes
import host_policy
import launch_failures
import live_models
import model_latch
import provider_retry
import user_scores
import accounts_override
import auto_bundles
import account_fleet
import axis_scores
import quota_economy
import quota_overrides
import quota_pools
import worker_sessions
from model_resolution import split_model_effort


METERED_LAST_RESORT = 'metered access is a last resort'
QUOTA_READING_UNAVAILABLE = 'quota reading unavailable (no current observation of any window of this account, so its spend cannot be priced)'
# Round 15 (owner): every quota account has a weekly (long reset-period) window; one without is a surfaced data error, never a
# 5-hour-only account that stands in for other accounts' work as a free outside point.
QUOTA_LONG_UNREAD = ('weekly quota reading unavailable (the account has a long reset-period window but no current or last-good '
                     'observation of it, only its 5-hour window is read, so its spend cannot be priced)')
QUOTA_NO_LONG_WINDOW = ('no weekly quota window (neither the account configuration nor the public capacity data gives this '
                        'account a long reset-period window, so its spend cannot be priced)')
QUOTA_DATA_ERRORS = (QUOTA_READING_UNAVAILABLE, QUOTA_LONG_UNREAD, QUOTA_NO_LONG_WINDOW)


def model_pin_matches(task, bundle, model):
    """Does the task's named model (or bundle id) name this bundle? Compares the resolved key and the raw spelling."""
    named = task.get('constraint_model')
    if not named:
        return False
    raw = str((bundle or {}).get('model') or '')
    return (named in ((bundle or {}).get('id'), model, raw)
            or account_fleet.model_leaf(named).lower() == account_fleet.model_leaf(raw).lower())


def exact_pin(task, bundle, model):
    """The user named this bundle, or its model and effort: it keeps that effort and is only warned about when slow."""
    bid = (bundle or {}).get('id')
    if bid and bid in (task.get('pinned_bundle'), task.get('constraint_bundle'), task.get('constraint_model')):
        return True
    return bool(task.get('constraint_effort')) and model_pin_matches(task, bundle, model)


def bundle_named(task, bundle, model):
    return exact_pin(task, bundle, model) or model_pin_matches(task, bundle, model)


def has_fixed_effort(bundle, accounts):
    """`model:effort` and `model-effort` launch ids run at that level only."""
    if (bundle or {}).get('effort_fixed'):
        return True
    launch_id = str((bundle or {}).get('launch_model') or (bundle or {}).get('model') or '')
    known = accounts if isinstance(accounts, dict) and 'models' in accounts else {'models': {}}
    return split_model_effort(launch_id)[1] is not None or auto_bundles.split_effort(launch_id, known)[1] is not None


def named_effort_bundles(task, ctx):
    """Rows for the effort the user's pin names on a model the account has no row at that level for, so the score decides
    among them exactly as it does among written rows. The launch contract is what offers a level; each row is the
    model's own carrier with a distinct id, so it never passes for the carrier itself."""
    effort = task.get('constraint_effort')
    if not effort or task.get('constraint_bundle'):
        return {}
    accounts, groups = ctx['accounts'], {}
    for bundle in ctx['bundles'].values():
        model = account_fleet.row_key(bundle['model'], accounts)
        if task.get('constraint_model') and not model_pin_matches(task, bundle, model):
            continue
        groups.setdefault((bundle['account'], bundle.get('harness'), model), []).append(bundle)
    found = {}
    for (_, _, model), rows in groups.items():
        written = {str(quota_economy.scoring_effort(r, account_fleet.model_spec(accounts, r['model'])) or '').lower() for r in rows}
        carriers = [r for r in rows if (not r.get('disabled') or r.get('disabled_by_default'))
                    and not r.get('discovery_unavailable') and not has_fixed_effort(r, accounts)]
        # Each level goes to the first row that offers it, so the answer does not depend on the order of the rows.
        for carrier in carriers:
            allowed = account_fleet.dispatch_allowed_efforts(carrier, accounts, model)
            levels = [effort]
            if (effort not in allowed) if allowed is not None else not task.get('constraint_model'):
                levels = []
            for level in levels:
                if level in written or (isinstance(carrier.get('efforts'), list) and level not in carrier['efforts']):
                    continue
                written.add(level)
                row_id = f"{carrier['id']}@{level}"
                found[row_id] = {**{k: v for k, v in carrier.items()}, 'id': row_id, 'effort': level,
                                 'pin_carrier': carrier['id']}
    return found


def cooling_reason(bundle, entries, accounts, now=None):
    """launch_failures.reason for this row or any entry recorded under another spelling of its model: one model has one
    cooldown, and a family guess is its own model."""
    found = launch_failures.reason(bundle, entries, now=now)
    if found or not entries:
        return found
    own = account_fleet.row_key(bundle.get('model') or '', accounts)
    prefix = launch_failures.key(bundle.get('account'), '')
    for entry in entries:
        if entry.startswith(prefix) and account_fleet.row_key(entry[len(prefix):], accounts) == own:
            return launch_failures.reason({**bundle, 'model': entry[len(prefix):]}, entries, now=now)
    return None


def evaluate_bundle(bundle, task, ctx):
    policy, accounts = ctx['policy'], ctx['accounts']
    spec = accounts['accounts'][bundle['account']]
    # Tables read by the name's own resolution. `model` is its key, for the matching that follows; `own_model` is which
    # model the row runs, for what the user named.
    model = account_fleet.canonical(bundle['model'], accounts)
    own_model = account_fleet.row_key(bundle['model'], accounts)
    info = account_fleet.model_spec(accounts, bundle['model'])
    kind = task['kind']
    base_hours = task['hours']
    base_chain_hours = task['chain_hours']
    result = {'id': bundle['id'], 'bundle': {k: bundle.get(k) for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model', 'draws', 'pin_carrier')
                                              if bundle.get(k) is not None},
              'excluded': [], 'notes': [], 'components': {}, 'score': None}
    five = ctx['five_axis']
    now = ctx['now']

    # Economy pool mode (round 16, owner policy: a window's price must not depend on the current task's flags): the pool is
    # what an account can run at all, evaluated with the task's flags neutralised (`rank` builds that task). It keeps the
    # account-level exclusions (host policy, checkbox, discovery, holds, unverified, data errors, weekly window) and drops
    # the ones that belong to the task: local-only, outbound, file transfer, image input, a named constraint, the demanded
    # categories, whether the remaining capacity fits THIS task, and the 5-hour windows.
    pool = bool(task.get('_economy_pool'))

    def exclude(reason):
        result['excluded'].append(reason)

    def task_exclude(reason):
        """An exclusion that belongs to the current task, not to the account: the ranking keeps it, the pool does not."""
        if not pool:
            result['excluded'].append(reason)

    def short_exclude(reason):
        """An exclusion by a 5-hour window alone: the work cannot start now, but the account still has its long windows, so
        the economy (which prices only long windows) keeps this candidate as a substitute and in its own ladder."""
        if pool:
            return
        result['excluded'].append(reason)
        result.setdefault('_five_short_reasons', []).append(reason)

    execution_account = accounts_override.preference_account(bundle, bundle['account'], accounts)
    cwd_spec = accounts['accounts'].get(execution_account) or spec
    if not account_fleet.account_cwd_allowed(cwd_spec, task.get('cwd')):
        exclude(f"outside account cwd restriction ({cwd_spec['cwd_regex']})" if cwd_spec.get('cwd_regex') else
                f"outside account allowed_cwd ({cwd_spec.get('allowed_cwd')})")

    if not host_policy.account_allowed(ctx['host_policy'], bundle['account']):
        exclude('excluded by host policy')
    # Off by default (superseded in public data) is not the user's checkbox: naming the model still reaches it.
    if bundle.get('disabled') and not (bundle.get('disabled_by_default') and bundle_named(task, bundle, own_model)):
        exclude('unchecked in model management')
    if bundle.get('discovery_unavailable'):
        exclude(bundle['discovery_unavailable'] if isinstance(bundle['discovery_unavailable'], str)
                else 'model not detected by discovery for 7+ days')
    if bundle['id'] in ctx['unavailable']:
        exclude('marked unavailable by the parent')
    cooling = cooling_reason(bundle, ctx.get('launch_failures'), accounts)
    if cooling:
        exclude(cooling)
    latched_model = model_latch.reason(bundle, ctx.get('model_latches'))
    if latched_model:
        exclude(latched_model)
    retrying = provider_retry.reason(bundle, ctx.get('provider_retries'))
    if retrying:
        exclude(retrying)
    unoffered = live_models.reason(bundle, ctx.get('live_models'), bundle['account'], spec)
    if unoffered:
        exclude(unoffered)
    # Only subscription accounts join automatic routing. A checked metered (API) bundle is a last resort:
    # explicit selection, task.metered_ok, or plan()'s fallback pass when no subscription can take the work.
    # Unknown billing stays explicit selection only.
    billing = accounts_override.manual_only_billing(bundle['account'], accounts)
    if billing and not task['metered_ok'] and not (billing == 'metered' and task.get('metered_fallback')):
        if (not model_pin_matches(task, bundle, own_model) and task.get('pinned_bundle') != bundle['id']
                and not any(o.get('account') == bundle['account'] for o in ctx['overrides'])):
            exclude(METERED_LAST_RESORT if billing == 'metered'
                    else f'{billing} billing: explicit selection only')
    if not spec.get('verified', True) and not task['include_unverified']:
        exclude('account not verified as launchable')
    if info.get('advisor_only') and kind not in info.get('advisor_for', ()):
        exclude('advisor only; cannot execute')
    account_out = bool(result['excluded'])      # out by the host, the checkbox, a hold or billing: not by this task
    if info.get('excluded_when') == 'local_only' and task['local_only']:
        task_exclude("the parent marked this work local-only (it must run on this machine: in-place edits, local tools, secrets or live accounts); this worker runs remotely")
    if info.get('excluded_when') == 'local_only' and task.get('outbound_ok') is False:
        task_exclude('the parent disallowed outbound work; this worker runs remotely')
    # Harness input limits apply to every bundle on the harness, manual or auto (HARNESS_CAPABILITIES).
    harness = bundle.get('harness') or account_fleet.bundle_harness(bundle, bundle.get('account'), bundle.get('id'))
    if (info.get('text_only') or not account_fleet.harness_transfers_files(harness)) and task.get('file_transfer'):
        task_exclude('this text-only runner cannot transfer or apply local files' if info.get('text_only')
                     else 'this runner cannot transfer or apply local files')
    if task['needs_visual'] and not account_fleet.harness_accepts_visual(harness, model):
        task_exclude(f'{harness} cannot accept image attachments for {model}, so it cannot take visual or GUI work')
    if task['constraint_model'] and not model_pin_matches(task, bundle, own_model):
        task_exclude('explicit model constraint')
    if task.get('constraint_effort') and str(quota_economy.scoring_effort(bundle, info) or '').lower() != task['constraint_effort']:
        task_exclude('explicit effort constraint')
    # Rule B: a demanded category with no data at all, no cited usd on a metered account, or unknown image_input modality
    # when the task demands image input (tool_operation 3), leaves automatic routing; naming the model (--model, or its
    # bundle) still dispatches it, with the data error as a warning and its cost unpriced.
    state = info.get('five_axis') or {'missing': list(axis_scores.FIVE_AXES), 'usd_status': 'missing'}
    billed = not (spec.get('unmetered') or spec.get('unlimited'))
    named = not pool and (model_pin_matches(task, bundle, own_model) or task.get('pinned_bundle') == bundle['id'])
    img_info = state.get('image_input') or {'status': 'missing'}
    needs_image_input = task['five_axis_demand'].get('tool_operation', 0) >= axis_scores.IMAGE_INPUT_LEVEL and not pool
    eff = quota_economy.scoring_effort(bundle, info)
    values = axis_scores.five_axis_values(state, eff)
    if needs_image_input and img_info.get('status') == 'no':
        exclude(f"model does not accept image input ({img_info.get('source') or 'public data'})")

    def _missing_targets(axis, level):
        if level < five['demanded_level']:
            return []
        if axis == 'tool_operation':
            selected = axis_scores.TOOL_LEVEL_FACETS.get(level) or ()
            if not selected:
                return []
            return [f'tool_operation.{f}' for f in selected if (values.get('tool_operation') or {}).get(f) is None]
        if values.get(axis) is not None:
            return []
        cat_cell = (state.get('categories') or {}).get(axis) or {}
        facets = cat_cell.get('facets') if isinstance(cat_cell, dict) else None
        req = user_scores.CATEGORY_FACETS.get(axis) or ()
        if isinstance(facets, dict) and len(req) > 1:
            miss_f = [f'{axis}.{f}' for f in req if axis_scores._at_effort(facets.get(f), eff) is None]
            if miss_f and len(miss_f) < len(req):
                return miss_f
        return [axis]

    blind, missing_targets = [], []
    if not pool:
        for a, w in task['five_axis_demand'].items():
            miss = _missing_targets(a, w)
            if miss:
                blind.append(a)
                missing_targets.extend(miss)
    if blind:
        first_target = missing_targets[0]
        eff_note = f' at effort {eff}' if eff and any(
            ((state.get('categories') or {}).get(a) or {}).get('value') is not None for a in blind
        ) else ''
        blind_msg = (
            f'no trusted data for the demanded category {", ".join(blind)} on {model}{eff_note} '
            f'(missing {", ".join(missing_targets)}; fill with `agentcompass score set {model} {first_target} <1..3>` '
            f'or in the tray Manual Scores panel)'
        )
    else:
        blind_msg = None
    if blind_msg and not pool:
        result['_five_blind_msg'] = blind_msg
    errors = (
        ([blind_msg] if blind_msg else [])
        + (['usd_per_hour has no cited source on a metered account'] if billed and state.get('usd_status') != 'ok' else [])
        + (['image_input modality has no cited source'] if needs_image_input and img_info.get('status') not in ('yes', 'no') else [])
    )
    for error in errors:
        if named:
            unpriced = ' (its cost is not priced)' if error.startswith('usd_per_hour') else ''
            result['notes'].append(f'data error: {error}; dispatched because it was named{unpriced}')
            result.setdefault('data_errors', []).append(error)
        else:
            exclude(f'data error: {error} (out of automatic routing; name the model to dispatch it)')
    manual_applied = state.get('manual_scores') or {}
    if manual_applied:
        summary_parts = [f"{t}={r['level']:g} (z={r['z']:+.4f}, {r['kind']}, {r['as_of']})"
                         for t, r in sorted(manual_applied.items())]
        result['notes'].append('manual capability scores applied: ' + '; '.join(summary_parts))
        result['manual_scores'] = dict(manual_applied)
    estimated = state.get('estimates') or {}
    if estimated:
        result['notes'].append('facets estimated (same-series sibling or the fit on the tier; no board of their own): '
                               + ', '.join(sorted(estimated)))
        result['estimated_axes'] = sorted({name.split('.')[0] for name in estimated})
    # One basis for the time a task takes, its spend and the window it needs (`duration_at`): Time/Task at this effort. Where
    # the data has none, the candidate leaves automatic routing; a named dispatch carries the task's own hours estimate
    # unscaled and the data error, and nothing stands in for the model's pace.
    dur_mult, dur_basis = quota_economy.duration_at(info, eff)
    if dur_mult is None:
        error = (f'no Time/Task for {model} at {eff or "its single configuration"} (Artificial Analysis, arena.ai Agent on '
                 f'its scale, or a same-series effort conversion)')
        if named:
            result['notes'].append(f'data error: {error}; dispatched because it was named, hours are the task\'s own estimate')
            result.setdefault('data_errors', []).append(error)
        else:
            exclude(f'data error: {error} (out of automatic routing at this effort; name the model to dispatch it)')
    hours = base_hours if dur_mult is None else base_hours * dur_mult
    chain_hours = base_chain_hours if dur_mult is None else base_chain_hours * dur_mult
    result['components']['duration_source'] = (dur_basis or {}).get('method') or 'unknown'
    if dur_mult is not None:
        result['notes'].append(f'time at {eff or "its single configuration"}: {hours:.2f}h from '
                               f'{(dur_basis or {}).get("method")} ({(dur_basis or {}).get("source") or "series"})')
    expires = quota_pools.parse_time(spec.get('expires_at'))
    if expires is not None and (expires - now).total_seconds() < hours * 3600:
        task_exclude('subscription ends before the work would finish')

    worker = task['worker']
    worker_bundle = worker.get('bundle') if worker else None
    worker_is_fast = bool(worker and (worker.get('fast') or (isinstance(worker_bundle, str) and worker_bundle.endswith(fast_modes.ID_SUFFIX))))
    worker_base = (worker_bundle[:-len(fast_modes.ID_SUFFIX)]
                   if isinstance(worker_bundle, str) and worker_bundle.endswith(fast_modes.ID_SUFFIX)
                   else worker_bundle)
    same_worker_base = (
        bool(worker)
        and worker_base == bundle['id']
        and not (task.get('read_only_review') and worker.get('read_only_review') is False)
    )
    resumes = same_worker_base and not worker_is_fast
    worn = worker_sessions.worker_worn(worker, policy)
    context_extra = 0.0
    if resumes:
        carried = worker_sessions.worker_historical_tokens(worker)
        if not worn:
            limit = max(250000, policy.get('rotate_after_carried_tokens', 2000000))
            context_extra = policy['context_burn_extra_at_250k'] * quota_pools.clamp(carried / limit, 0, 1)
        if worker.get('telemetry_status') == 'partial':
            result['notes'].append('session telemetry partial; carried tokens are a lower bound')
        if worker.get('compacted_tokens') is None and worker.get('compactions') is None and worker.get('historical_context_tokens') is None:
            result['notes'].append('compaction history unknown (give chain.worker.agent_id); resuming')
        elif worn:
            result['notes'].append(f"the worker has carried {carried / 1e6:.1f}M historical tokens of context over "
                                   f"{worker.get('compactions', '?')} compactions; rotate with a handoff packet")
    context_burn = 1 + context_extra
    # `usd_per_hour` is the model's dollars per hour of its own work (a rate), so the spend is usd/h x the hours this
    # candidate takes at its effort, and the window's quota rate carries no second effort factor.
    econ_hours, econ_chain_hours = hours, chain_hours
    step_usd = quota_economy.bundle_task_usd(spec, model, econ_hours, info, context_burn)
    chain_usd = quota_economy.bundle_task_usd(spec, model, econ_chain_hours, info, context_burn)
    available_at = None
    five_window_states = {}
    long_applicable = False         # the bundle draws at least one long reset-period window (read or not)
    if spec.get('unlimited'):
        result['notes'].append('unlimited usage')
    boost = sum(float(o.get('boost') or 0) for o in ctx.get('overrides', ())
                if o['account'] == bundle['account'] and (not o.get('model') or account_fleet.canonical(o['model'], accounts) == model)
                and (quota_overrides.expected_reset_time(o) is None or quota_overrides.expected_reset_time(o) <= now))
    for intent in ctx.get('overrides', ()):
        if (intent['account'] == bundle['account'] and intent.get('expected_reset_at')
                and (not intent.get('model') or account_fleet.canonical(intent['model'], accounts) == model)):
            result['notes'].append(f"expected reset {intent['expected_reset_at']}: {intent['reset_expectation_status']}; capacity requires a fresh observation")
    if boost:
        result['components']['boost'] = round(boost, 3)
        result['notes'].append(f'temporary boost {boost:+.2f} from a user override')
    # Purpose-limited unmetered channels (Dot, ChatGPT Web) have no quota to relieve: never another window's substitute.
    result['_five_unmetered'] = bool(spec.get('unmetered') or spec.get('unlimited'))
    # A metered (API) account is a last resort: it has a ladder of its own but is never another window's substitute.
    result['_five_metered'] = billing == 'metered'
    if not spec.get('unmetered'):
        acct_usage = ctx['usage'].get('accounts', {}).get(bundle['account']) or {}
        acct_stale = acct_usage.get('status') == 'stale' or acct_usage.get('stale') is True
        latched = quota_pools.latched_windows(accounts, bundle['account'])
        named = not pool and (model_pin_matches(task, bundle, own_model) or task.get('pinned_bundle') == bundle['id'])
        for wid, wspec, observed in quota_pools.applicable_windows(spec, model, acct_usage, bundle, accounts):
            result.setdefault('_five_axis_windows', []).append(wid)
            long_applicable = long_applicable or not quota_pools.window_is_short(wspec, policy)
            observed = quota_pools.cycle_observation(wspec, observed, acct_usage, now, read_failed_ok=True)
            refusal = quota_pools.window_refusal(wid, wspec, observed, acct_usage, now, latched)
            if refusal:
                (short_exclude if quota_pools.window_is_short(wspec, policy) else exclude)(refusal)
            expected = quota_overrides.window_reset_expectation(ctx.get('overrides', ()), bundle['account'], model, wid, spec, accounts, now)
            state = quota_pools.window_state(wid, wspec, observed, spec, now, policy['short_window_hours'],
                                 account_stale=acct_stale, expected_reset_at=expected)
            unit = quota_economy.unit_rate(spec, wid, model, info)
            cap_usd_w = quota_economy.capacity_usd(spec, wid)
            if cap_usd_w is None:
                if state['known'] and state['remaining'] <= policy['safety_margin_pct']:
                    exclude(f'{wid} has {state["remaining"]:.0f}% left, inside the {policy["safety_margin_pct"]}% reserve '
                            '(capacity unknown, so the need cannot be priced)')
                    result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
                    continue
                err = f'{wid} capacity_unknown (quota unknown, spend unpriced)'
                if named:
                    local_u = spec.get('local_spend_usd')
                    local_note = f'; local opencode.db lower-bound spend ~${local_u:.2f}' if isinstance(local_u, (int, float)) and local_u > 0 else ''
                    result['notes'].append(f'data error: {err}; dispatched because it was named{local_note}')
                    result.setdefault('data_errors', []).append(err)
                else:
                    exclude(f'data error: {err} (out of automatic routing; name the model to dispatch it)')
                continue
            if not state['known']:
                if state['short']:
                    w_len = float(wspec.get('hours') or 0) or econ_hours
                    step = None if unit is None else min(econ_hours, w_len) * unit * context_burn
                    margin_pct = quota_pools.short_window_limits(spec, wid, policy, now)[2]
                    if step is not None and 100.0 - step < margin_pct:
                        short_exclude(f'{wid} full capacity (100%) is short of the ~{step:.1f}% the work needs plus {margin_pct}% margin')
                    elif (observed and (reset_dt := quota_pools.parse_time(observed.get('resets_at'))) is not None
                          and reset_dt > now and observed.get('reset_time_passed') is not True):
                        wait = (reset_dt - now).total_seconds() / 3600
                        short_exclude(f'{wid} has an active reset in {wait:.1f}h with unknown remaining percentage')
                continue
            state, reserved_usd, reserved_pct = quota_pools.reserve_inflight(
                bundle['account'], spec, wid, wspec, state, ctx.get('records'), accounts, policy, now)
            if reserved_usd > 0:
                result['notes'].append(f'{wid}: -${reserved_usd:.2f} (-{reserved_pct:.1f}%) reserved by in-flight {bundle["account"]} dispatch')
            if state['short']:
                w_len = float(wspec.get('hours') or 0) or econ_hours
                tau = state.get('reset_hours', w_len)
                h_before = min(econ_hours, w_len) if (tau is None or tau == float('inf') or state.get('unstarted')) else min(econ_hours, max(0.0, float(tau)))
                step = None if unit is None else h_before * unit * context_burn
                step_period = None if unit is None else min(max(0.0, econ_hours - h_before), w_len) * unit * context_burn
                small_short, floor_pct, margin_pct = quota_pools.short_window_limits(spec, wid, policy, now)
                empty = state['remaining'] <= floor_pct
                if empty or (step is not None and state['remaining'] - step < margin_pct):
                    wait = state['reset_hours']
                    what = 'is empty' if empty else f'has {state["remaining"]:.0f}%, short of the ~{step:.1f}% the work needs,'
                    post_period = None if unit is None else min(econ_hours, w_len) * unit * context_burn
                    if post_period is not None and 100.0 - post_period < margin_pct:
                        short_exclude(f'{wid} full capacity (100%) is short of the ~{post_period:.1f}% the work needs plus {margin_pct}% margin')
                    elif (not small_short and not state['stale'] and 0 < wait <= policy['defer_if_reset_within_hours']
                          and wait >= state.get('natural_reset_hours', wait)):
                        available_at = max(available_at or 0, wait)
                        result['notes'].append(f'{wid} {what} for {wait:.1f}h; start after its reset')
                        renewed = quota_pools.after_reset(state, w_len)
                        if renewed is not None:
                            renewed['unstarted'] = True
                            five_window_states[wid] = (wspec, renewed, cap_usd_w, unit)
                    else:
                        short_exclude(f'{wid} {what} for {wait:.1f}h')
                    continue
                if step_period is not None and 100.0 - step_period < margin_pct:
                    short_exclude(f'{wid} full capacity (100%) is short of the ~{step_period:.1f}% the work needs plus {margin_pct}% margin')
                    continue
                five_window_states[wid] = (wspec, state, cap_usd_w, unit)
                continue
            if unit is None:
                continue
            w_len = wspec.get('hours')
            tau = state.get('reset_hours')
            req_hours = econ_chain_hours if task['coupling'] == 'high' else econ_hours
            h_before = req_hours if (tau is None or tau == float('inf') or w_len is None) else min(req_hours, max(0.0, float(tau)))
            need = h_before * unit * context_burn
            if (not pool and state['remaining'] - need < policy['safety_margin_pct'] and w_len is not None
                    and state['reset_hours'] <= policy['defer_if_reset_within_hours']):
                renewed = quota_pools.after_reset(state, w_len)
                if renewed is not None and renewed['expiry_hours'] is not None and renewed['expiry_hours'] < hours:
                    renewed = None
                if renewed is not None and 100.0 - min(req_hours, float(w_len)) * unit * context_burn >= policy['safety_margin_pct']:
                    state = renewed
                    available_at = max(available_at or 0, renewed['deferred_hours'])
                    result['_five_long_wait'] = True        # a long window must renew first: no absorber of other work now
                    need = min(req_hours, float(w_len)) * unit * context_burn
                    result['notes'].append(f'{wid} renews in {renewed["deferred_hours"]:.1f}h; start then (assumes a full renewal)')
            if state['remaining'] - need < policy['safety_margin_pct']:
                if state['reset_hours'] == float('inf'):
                    task_exclude(f'{wid} has {state["remaining"]:.0f}% left; the work needs ~{need:.1f}% plus a {policy["safety_margin_pct"]}% margin '
                                 '(starter allowance does not reset)')
                else:
                    task_exclude(f'{wid} has {state["remaining"]:.0f}% left; the work needs ~{need:.1f}% plus a {policy["safety_margin_pct"]}% margin '
                                 f'and it resets in {state["reset_hours"] / 24:.1f} days')
                    result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
            elif w_len is not None and req_hours > h_before:
                rem_need = min(req_hours - h_before, float(w_len)) * unit * context_burn
                if 100.0 - rem_need < policy['safety_margin_pct']:
                    task_exclude(f'{wid} full capacity (100%) is short of the ~{rem_need:.1f}% the work needs per period plus {policy["safety_margin_pct"]}% margin')
            five_window_states[wid] = (wspec, state, cap_usd_w, unit)
            if state['stale'] and 'capacity from a stale observation' not in result['notes']:
                result['notes'].append('capacity from a stale observation')
            if acct_usage.get('status') != 'fresh' and acct_usage.get('fetched_at') and not result['components'].get('reading_at'):
                # Priced from the last successful reading of this reset period (owner decision of round 13b): labelled
                # with its read time so plan, roll and the tray say how old the quota figure is.
                result['components']['reading_at'] = acct_usage['fetched_at']
                result['notes'].append(f"{wid}: priced from the reading taken at {acct_usage['fetched_at']} "
                                       f"(the latest read did not succeed; it is the last good reading of this reset period)")
        if spec.get('capacity_unknown') and not result.get('_five_axis_windows'):
            err = 'capacity_unknown (quota unknown, spend unpriced)'
            if named:
                local_u = spec.get('local_spend_usd')
                local_note = f'; local opencode.db lower-bound spend ~${local_u:.2f}' if isinstance(local_u, (int, float)) and local_u > 0 else ''
                result['notes'].append(f'data error: {err}; dispatched because it was named{local_note}')
                result.setdefault('data_errors', []).append(err)
            else:
                exclude(f'data error: {err} (out of automatic routing; name the model to dispatch it)')
        if (not spec.get('unlimited') and not account_out and not result.get('data_errors')
                and not any(not st.get('short') for _, st, _, _ in five_window_states.values())):
            # Priced on the weekly window or not at all (round 15): a candidate with no long reset-period window in its
            # state cannot be priced and cannot stand in for another account's work, so it leaves automatic routing as
            # a surfaced data error. Three causes, three messages: nothing of the account can be read (round 13); the
            # weekly window is declared but not read while the 5-hour window is; no weekly window exists at all.
            if not result.get('_five_axis_windows') or not long_applicable:
                err = QUOTA_NO_LONG_WINDOW if not long_applicable else QUOTA_READING_UNAVAILABLE
            elif five_window_states:
                err = QUOTA_LONG_UNREAD
            else:
                err = QUOTA_READING_UNAVAILABLE
            if named:
                result['notes'].append(f'data error: {err}; dispatched because it was named')
                result.setdefault('data_errors', []).append(err)
            else:
                exclude(f'data error: {err} (out of automatic routing; name the model to dispatch it)')
        result['_five_window_states'] = five_window_states
        result['_five_econ_hours'] = econ_hours
        result['_five_chain_hours'] = econ_chain_hours
        result['_five_context_burn'] = context_burn
        if not five_window_states and not result['excluded'] and not result.get('data_errors'):
            result['notes'].append('no current capacity observation; unknown is not exhausted')
    elif not spec.get('unlimited'):
        acct_usage = ctx['usage'].get('accounts', {}).get(bundle['account']) or {}
        cad_got = quota_pools.cadence_window_state(bundle['account'], spec, acct_usage, ctx.get('records'), now)
        if cad_got is not None:
            wspec, state, cap_count = cad_got
            result.setdefault('_five_axis_windows', []).append('cadence')
            need_pct = 100.0 / cap_count
            if not pool and state['remaining'] + 1e-9 < need_pct:
                if state['reset_hours'] <= policy['defer_if_reset_within_hours']:
                    renewed = quota_pools.after_reset(state, wspec['hours'])
                    if renewed is not None:
                        state = renewed
                        available_at = max(available_at or 0, renewed['deferred_hours'])
                        result['_five_long_wait'] = True
                        result['notes'].append(
                            f'cadence renews in {renewed["deferred_hours"]:.1f}h; start then (assumes a full renewal)'
                        )
                if state['remaining'] + 1e-9 < need_pct:
                    task_exclude(f'cadence has 0 of {cap_count:g} dispatches left and resets in {state["reset_hours"]:.1f}h')
                    result['wait_until_hours'] = max(result.get('wait_until_hours', 0), state['reset_hours'])
            five_window_states['cadence'] = (wspec, state, cap_count, None)
            result['_five_window_states'] = five_window_states
            result['_five_window_spend'] = 1.0
            result['_five_cadence_instant'] = True
            result['_five_econ_hours'] = econ_hours
            result['_five_chain_hours'] = econ_chain_hours
            result['_five_context_burn'] = context_burn
    if (pool or (not task.get('disable_fast') and available_at is None)) and not result['excluded'] and not result.get('_five_long_wait'):
        fast_info = ((info.get('five_axis') or {}).get('fast_mode')) or {}
        if fast_info.get('status') == 'ok' and fast_modes.supported(harness, model, info):
            f_price = float(fast_info.get('price_multiplier') or 2.0)
            f_speed = float(fast_info.get('speed_multiplier') or 1.0)
            f_econ_h = econ_hours / f_speed if f_speed > 0 else econ_hours
            f_chain_h = econ_chain_hours / f_speed if f_speed > 0 else econ_chain_hours
            f_hours = hours / f_speed if f_speed > 0 else hours
            f_step_usd = step_usd * f_price
            f_chain_usd = chain_usd * f_price
            fast_ok = bool(five_window_states)
            for wid, (wspec, st, cap_usd_w, unit) in five_window_states.items():
                if pool and st.get('short'):
                    continue
                if unit is None:
                    fast_ok = False
                    break
                if pool:
                    continue        # the pool asks what the account can run, not whether it fits the remaining capacity now
                f_unit = unit * f_price * f_speed * context_burn     # per fast hour; x f_econ_h is price x the normal spend
                w_len = wspec.get('hours')
                tau = st.get('reset_hours')
                if st['short']:
                    w_l = float(w_len or 0) or f_econ_h
                    h_bef = min(f_econ_h, w_l) if (tau is None or tau == float('inf') or st.get('unstarted')) else min(f_econ_h, max(0.0, float(tau)))
                    f_step = h_bef * f_unit
                    f_per = min(max(0.0, f_econ_h - h_bef), w_l) * f_unit
                    _, fl_pct, mg_pct = quota_pools.short_window_limits(spec, wid, policy, now)
                    if st['remaining'] <= fl_pct or st['remaining'] - f_step < mg_pct or 100.0 - f_per < mg_pct:
                        fast_ok = False
                        break
                else:
                    req_h = f_chain_h if task['coupling'] == 'high' else f_econ_h
                    h_bef = req_h if (tau is None or tau == float('inf') or w_len is None) else min(req_h, max(0.0, float(tau)))
                    f_need = h_bef * f_unit
                    if st['remaining'] - f_need < policy['safety_margin_pct']:
                        fast_ok = False
                        break
                    if w_len is not None and req_h > h_bef:
                        f_rem = min(req_h - h_bef, float(w_len)) * f_unit
                        if 100.0 - f_rem < policy['safety_margin_pct']:
                            fast_ok = False
                            break
            if task.get('force_fast') and not fast_ok:
                exclude(f'fast mode ({f_price:g}x burn) exceeds remaining window safety margin')
            elif fast_ok:
                result['_five_fast_candidate'] = {
                    'price_multiplier': f_price, 'speed_multiplier': f_speed,
                    'econ_hours': f_econ_h, 'chain_hours': f_chain_h, 'hours': f_hours,
                    'step_usd': f_step_usd, 'chain_usd': f_chain_usd, 'source': fast_info.get('source'),
                }
        elif task.get('force_fast'):
            err = f"fast_mode has no cited public pricing/speed for {model} ({fast_info.get('reason') or fast_info.get('status') or 'missing'})"
            result['notes'].append(f'data error: {err}; fast mode not enabled')
            result.setdefault('data_errors', []).append(err)
    result['capacity'] = None           # the economy sets the priced window (apply_five_axis_economy)
    result['expected_hours'] = round(hours, 2)
    result['duration_hours'] = round(hours, 4)
    if dur_mult is not None:
        result['components']['duration_multiplier'] = round(dur_mult, 4)
    result['expected_pct'] = result['chain_pct'] = None
    result['expected_usd'] = None if spec.get('billing') == 'unknown' else round(step_usd, 2)
    result['chain_usd'] = None if spec.get('billing') == 'unknown' else round(chain_usd, 2)
    result['available_in_hours'] = available_at
    action = 'start'
    if worker:
        if worn:
            action = 'rotate'
        else:
            action = 'resume' if resumes else 'switch'
    result['action'] = action
    # Quality is the continuous fit of the category scores at this effort to the demand, and nothing else (the effort's
    # extra burn is priced in its usd and its gain in the categories' effort shift). The economy decides.
    fit, axis_rows = axis_scores.five_axis_fit(values, task['five_axis_demand'], task['five_axis_targets'])
    result['_five_values'] = values         # the economy re-reads this candidate's scores at its effort over the demand grid
    # The candidate's four category scores at this effort: what the radar and the explanation show (tool_operation: the
    # weaker of its facets that have a value).
    tool = [z for z in values['tool_operation'].values() if z is not None]
    shown = {**{a: values[a] for a in axis_scores.FIVE_AXES if a != 'tool_operation'}, 'tool_operation': min(tool) if tool else None}
    result['components'].update({a: None if v is None else round(v, 4) for a, v in shown.items()})
    result['components'].update({f'share_{a}': v for a, v in
                                 axis_scores.five_axis_shares(values, ctx['accounts']['_five_axis']['targets']).items()})
    result['components'].update(five_axis_level=round(axis_scores.five_axis_level(axis_rows), 4),
                                five_axis_shortfall=round(sum(r['demand'] * r['shortfall'] for r in axis_rows.values())
                                                          / max(1, sum(r['demand'] for r in axis_rows.values())), 4))
    result['five_axis_demand'] = task['five_axis_demand']
    result['five_axis_fit'] = axis_rows
    usd_rate = 0.0 if spec.get('unlimited') or spec.get('unmetered') or spec.get('billing') == 'unknown' else         quota_economy.model_usd_per_hour(model, info)       # dollars per hour of this candidate's own work; effort is in its duration
    result['five_axis_price'] = {'usd_per_hour': round(usd_rate, 4)}
    result['components'].update({'fit': round(fit, 3), 'continuity': 0.0})
    if action == 'resume' and task['coupling'] in ('medium', 'high'):
        # a related task (--resume or an auto-matched continuation of the same chain) continues its worker: one demand level's worth
        result['components']['continuity'] = round(task['five_axis_continuity'], 4)
    return result
