"""Deterministic worker allocation: capability gates, task fit across the four R2 capability categories
(`divergence`, `coding`, `aesthetics`, `tool_operation`), continuity with a coupled chain's existing worker,
and the opportunity cost of the quota the work would use.

Scores are panel standard deviations (`z = (x - x_anchor) / sd_panel`, anchor `claude-opus-5-5` at High = 0) from
`references/five-axis-scores.json`. In `five_axis_fit`, each demanded category's zero point is its level-1 target `t1`
(the panel's weakest model), level 2 targets the panel median `t2`, and level 3 targets the anchor `t3`:
`fit = sum_a w_a * (min(z_a, c_a) - t1_a) / sum_a w_a`, the cap `c_a` being the target at levels 1 and 2 and the
category's ceiling (the best measured model) at level 3 (scores below `t1_a` may go below zero, and economy outside
alternatives require positive fit above zero). The formula is defined in `docs/technical-reference.md` section 1. Every number that drives a choice
lives in `references/accounts.json` or `references/five-axis-scores.json` and is returned in the per-candidate breakdown.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

# Also support the path-loaded allocator and python -I entrypoints.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_state
import host_policy
import live_models
import model_latch
import provider_retry
import provider_inventory
import accounts_override
import account_fleet
import axis_scores
import candidate_eval
import candidate_ranking
import quota_economy
import quota_overrides
import quota_pools
import task_intake
import worker_sessions


def named_pin_block(task, candidates):
    """Why what the user named cannot run, one line per row; [] when the task names nothing."""
    if not (task.get('constraint_model') or task.get('constraint_effort') or task.get('pinned_bundle')):
        return []
    off_pin = ('explicit model constraint', 'explicit effort constraint')
    pinned = task.get('pinned_bundle')
    rows = [c for c in candidates if (pinned and candidate_ranking.candidate_for_bundle([c], pinned, task.get('force_fast')) is c)
            or (not pinned and not any(r in off_pin for r in c['excluded']))]
    named = task.get('pinned_bundle') or ':'.join(x for x in (task.get('constraint_model'), task.get('constraint_effort')) if x)
    rows = [c for c in rows if 'marked unavailable by the parent' not in c['excluded']] or rows
    if not rows:
        return [f'{named}: no configured bundle offers it']
    return [f"{named} via {c['id']}: {'; '.join(c['excluded'])}" for c in rows if c['excluded']][:4]


def plan(raw_task, usage, now=None, unavailable=(), records=None, accounts=None, capabilities=None, overrides=None,
         host_policy_config=None, *, read_local_state=True):
    accounts = accounts_override.normalize(accounts or account_fleet.load_accounts())
    capabilities = capabilities or account_fleet.load_capabilities(accounts=accounts)
    now = now or datetime.now(timezone.utc)
    usage = provider_inventory.merge_usage(usage, accounts, now)
    active_overrides = (quota_overrides.load_overrides(now) if read_local_state else []) if overrides is None else overrides
    usage, accounts, applied = quota_overrides.apply_overrides(active_overrides, usage, accounts, now)
    accounts = axis_scores.five_axis_overlay(accounts, read_local_state=read_local_state)
    policy = accounts['scoring']
    task = task_intake.normalize_task(raw_task, policy, capabilities, accounts)
    if task['worker'] is None and task['chain_id'] and records:
        for record in reversed(records):
            if record.get('chain') == task['chain_id'] and (record.get('bundle') or {}).get('bundle_id'):
                w = {'bundle': record['bundle']['bundle_id'], 'from_ledger': True}
                wid = record.get('worker_id') or (record.get('worker') or {}).get('agent_id')
                if wid and read_local_state:
                    w['agent_id'] = wid
                    w.update(worker_sessions.session_state(wid))
                task['worker'] = w
                break
    candidates, eligible, system = candidate_ranking.rank(task, usage, policy, accounts, capabilities, now, unavailable, applied, records,
                                        host_policy_config, read_local_state=read_local_state)
    # Metered last resort: only when no subscription bundle is eligible, re-rank with checked metered bundles admitted.
    if not eligible and any(c['excluded'] == [candidate_eval.METERED_LAST_RESORT] for c in candidates):
        task['metered_fallback'] = True
        candidates, eligible, system = candidate_ranking.rank(task, usage, policy, accounts, capabilities, now, unavailable, applied,
                                            records, host_policy_config, read_local_state=read_local_state)
    worker_id = (task['worker'] or {}).get('bundle')
    resume = next((c for c in candidates if c['id'] == worker_id), None)
    decision = {'task': {k: task[k] for k in ('kind', 'difficulty', 'hours', 'chain_hours', 'coupling')}, 'system': system,
                'assumptions': assumptions(candidates, accounts)}
    for err in candidate_eval.QUOTA_DATA_ERRORS:
        unread = sorted({c['bundle']['account'] for c in candidates if any(err in str(r) for r in c['excluded'])})
        if unread:
            decision.setdefault('warnings', []).append(
                f"{', '.join(unread)}: {err}; out of automatic routing and no substitute for other accounts")
    aged = {}
    for c in candidates:
        at = c['components'].get('reading_at')
        if at and not c['excluded']:
            aged.setdefault(c['bundle']['account'], at)
    for name, at in sorted(aged.items()):
        decision.setdefault('warnings', []).append(
            f"{name}: priced from its last good usage reading, taken at {at} in the current reset period "
            f"(the latest read failed); a provider hold on a model or pool still wins over this reading")
    state_policy = (host_policy.load_policy() if read_local_state else {'allowed_accounts': list((accounts.get('accounts') or {}).keys())}) if host_policy_config is None else host_policy_config
    state_warnings = account_state.warnings(accounts.get('_discovery'), accounts['accounts'],
                                            lambda name: host_policy.account_allowed(state_policy, name))
    if state_warnings:
        decision.setdefault('warnings', []).extend(state_warnings)
    live_doc = system.pop('_live_models', None) or (live_models.load_or_refresh(
        accounts=accounts,
        allowed=lambda name: host_policy.account_allowed(state_policy, name),
        now=now,
    ) if read_local_state else {'accounts': {}})
    latches = model_latch.active(now=now) if read_local_state else {}
    latch_warnings = live_models.warnings(
        live_doc,
        accounts,
        lambda name: host_policy.account_allowed(state_policy, name),
        covered=set(latches),
    )
    latch_warnings += model_latch.warnings(latches, lambda name: host_policy.account_allowed(state_policy, name)) if read_local_state else []
    latch_warnings += provider_retry.warnings(provider_retry.active(now=now),
                                              lambda name: host_policy.account_allowed(state_policy, name)) if read_local_state else []
    if latch_warnings:
        # First: the roll answer shows only the first few warnings, and a model that cannot start must be seen.
        decision['warnings'] = [*latch_warnings, *decision.get('warnings', [])]
    decision['task'].update(difficulty_source=task['difficulty_source'], axis_demand=axis_scores.five_axis_demand(task),
                            max_recommended_effort=None)
    if task.get('wants_fast'):
        decision['task']['wants_fast'] = True
    if task.get('read_only_review'):
        decision['task']['read_only_review'] = True
    if task.get('metered_fallback'):
        decision['task']['metered_fallback'] = True
    if task['profile']:
        decision['task'].update(profile=task['profile'], profile_difficulty=task['profile_difficulty'])
    if applied:
        decision['overrides'] = applied
    ranked_candidates = [*eligible, *[c for c in candidates if c['excluded']]]
    result = {'stage': 'plan', 'source': 'deterministic_allocator', 'observed_at': now.isoformat(), 'decision': decision,
              'ranking': ranked_candidates, 'usage': usage}
    if not eligible:
        closest = sorted(
            (c for c in candidates if c['excluded']),
            key=lambda c: (len(c['excluded']), -(c['components']['fit'] if c['components'].get('fit') is not None else float('-inf'))),
        )[:2]
        named_block = named_pin_block(task, candidates)
        decision.update({'action': 'none', 'needs_user_decision': True,
                         'reason': 'No eligible bundle. Closest: ' + ' | '.join(
                             f"{c['id']}: {'; '.join(c['excluded'])}" for c in closest)})
        # When a bundle is held back only by quota, say when that quota returns, so the parent can wait.
        waits = sorted((h, c['id']) for c in candidates if c['excluded'] and (h := quota_pools.quota_wait_hours(c, usage)) is not None)
        if waits:
            hours, bundle_id = waits[0]
            decision['next_reset'] = {'bundle': bundle_id, 'hours': round(hours, 1),
                                      'at': (now + timedelta(hours=hours)).isoformat()}
            decision['reason'] += f'. {bundle_id} is held back by quota that returns in {hours:.1f}h: wait, or decide otherwise.'
        if named_block:
            # What the user named cannot run. Say why and stop; nothing else stands in for it.
            decision['blocked'] = 'named_model_unavailable'
            decision.setdefault('warnings', []).extend(named_block)
            decision['reason'] = named_block[0]
            result['next'] = ('The model the user named cannot run now: ' + '; '.join(named_block)[:300]
                              + '. Tell the user; do not substitute another model.')
        result['choice'] = 'none'
        return result
    can_wait = task['can_wait_hours']
    ready = [c for c in eligible if (c['available_in_hours'] or 0) <= can_wait]
    pool = ready or eligible
    pinned_cand = (
        candidate_ranking.candidate_for_bundle(pool, task['pinned_bundle'], task.get('force_fast'))
        if task.get('pinned_bundle') else None
    )
    top = pinned_cand or pool[0]
    better = eligible[0]
    if better is not top and better['available_in_hours']:
        decision['better_if_waiting'] = {'bundle': better['id'], 'hours': round(better['available_in_hours'], 1),
                                         'score_gain': round(better['score'] - top['score'], 4)}
    forced = pinned_cand is not None
    decision.update({'action': top['action'], 'needs_user_decision': False,
                     'continuity': ('high-coupling chain stays with its owner'
                                    if (top is resume and top['action'] == 'resume' and not forced) else None)})
    if (top['available_in_hours'] or 0) > can_wait:
        # can_wait_hours is a limit, not a preference: say so instead of quietly starting later than allowed
        decision.update({'needs_user_decision': True, 'wait_hours': round(top['available_in_hours'], 1),
                         'reason': (f"The chain owner {top['id']} can resume in {top['available_in_hours']:.1f}h"
                                    if top is resume and top['action'] == 'resume'
                                    else f"Nothing eligible can start within {can_wait:g}h; the best, {top['id']}, can start in {top['available_in_hours']:.1f}h")
                         + '. Wait, allow a longer wait, or mark it unavailable and replan.'})
    # A worker held only by the old route gives way when the user names something else.
    pin_moved = resume is not None and (
        ('explicit model constraint' in resume['excluded'] or 'explicit effort constraint' in resume['excluded'])
        or bool(task.get('pinned_bundle') and task['pinned_bundle'] != resume['id']))
    if (task['worker'] and not worker_sessions.worker_worn(task['worker'], policy) and task['coupling'] == 'high'
            and resume is not None and resume['excluded'] and not pin_moved):
        decision.update({'needs_user_decision': True, 'drift_risk': 'high',
                         'resume_blocked_by': resume['excluded'],
                         'wait_hours': resume.get('wait_until_hours'),
                         'reason': 'The chain owner cannot continue now. Waiting keeps one owner; switching delivers sooner with handoff and drift risk.'})
    # The judge weighs models, not efforts, harnesses or accounts of one model: the margin is to the best other model.
    rival = next((c for c in pool if c is not top
                  and account_fleet.row_key(c['bundle']['model'], accounts) != account_fleet.row_key(top['bundle']['model'], accounts)), None)
    margin = round(top['score'] - rival['score'], 4) if rival is not None and not forced else None
    decision['margin'] = margin
    decision['needs_judgment'] = bool(margin is not None and margin < policy['needs_judgment_margin'])
    if top.get('data_errors'):
        for note in top.get('notes') or ():
            if str(note).startswith('data error:'):
                w_note = f"{top['id']}: {note}"
                if w_note not in decision.get('warnings', ()):
                    decision.setdefault('warnings', []).append(w_note)
    if top['bundle'].get('fast'):
        decision['fast'] = True
        decision['fast_reason'] = top['components'].get('fast_reason')
    decision.setdefault('reason', explain(top, system))
    # No required ability: the fit and the economy chose this effort.
    chosen = top['bundle'].get('effort')
    decision['task']['max_recommended_effort'] = chosen if chosen in quota_economy.EFFORT_ORDER else None
    result['choice'] = top['id']
    result['selected_bundle'] = dict(top['bundle'], bundle_id=top.get('base_id') or top['id'], expected_usd=top.get('expected_usd'),
                                     expected_hours=top.get('expected_hours'))
    result['expected_usd'] = top.get('expected_usd')
    result['expected_hours'] = top.get('expected_hours')
    result['decision']['alternatives'] = list(dict.fromkeys(c['id'] for c in pool if c['id'] != top['id']))[:3]
    if top['available_in_hours']:
        decision.update({'action': 'defer', 'start_after_hours': round(top['available_in_hours'], 1)})
    return result


def assumptions(candidates, accounts):
    notes = []
    for c in candidates:
        if c['excluded']:
            continue
        spec = accounts['accounts'][c['bundle']['account']]
        if quota_pools.parse_time(spec.get('expires_at')) and spec.get('expires_precision') == 'date':
            notes.append(f"{c['bundle']['account']}: expiry is a date only; end of day assumed")
    return sorted(set(notes))


def explain(top, system):
    parts = [f"{top['id']} ({top['action']}) scored {top['score']:.4f}"]
    cap = top.get('capacity')
    if cap:
        rem_usd_str = f" (${cap['remaining_usd']:.2f})" if cap.get('remaining_usd') is not None else ""
        exp_usd_str = f" (~${top['expected_usd']:.2f})" if top.get('expected_usd') is not None else ""
        priced = (f"need probability {cap['need_start']:.2f} -> {cap['need_end']:.2f}, quota price "
                  f"{cap['quota_price']:.5f} fit/USD" if cap.get('priced') else 'a short window: feasibility only, not priced')
        members = round(cap['full'] / 100.0)
        left = (f"{cap['remaining']:.0f}% left" if members == 1
                else f"{cap['remaining']:.0f}% of one account left in a pool of {members} (full = {cap['full']:.0f}%)")
        parts.append(f"{cap['window']}: {left}{rem_usd_str}, work ~{top['expected_pct']}%{exp_usd_str} "
                     f"(chain ~{top['chain_pct']}%), {priced}, "
                     + (f"before its {'expiry' if cap['expires_first'] else 'reset'} in {cap['end_hours']:.0f}h, "
                        if cap.get('end_hours') is not None else 'with no reset, ')
                     + f"opportunity cost {top['components']['opportunity_cost']:.4f}")
    if top['bundle'].get('fast'):
        parts.append(f"fast mode enabled ({top['components'].get('fast_reason') or 'priced as its own candidate'})")
    if top['components'].get('boost'):
        parts.append(f"includes a temporary user-override boost of {top['components']['boost']:+.2f}")
    if top['components'].get('continuity'):
        parts.append(f"continuing its related worker adds {top['components']['continuity']:.2f} (one demand level)")
    if top.get('unscored_named_dispatch'):
        parts.append('unscored named dispatch (fit unmeasured)')
    return '; '.join(parts)
