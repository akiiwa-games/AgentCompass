"""The decision snapshot recorded in the ledger."""

import copy

import axis_scores


SNAPSHOT_FULL, SNAPSHOT_COMPACT, SNAPSHOT_EXCLUDED = 6, 8, 16
SNAPSHOT_CAPACITY_KEYS = ('window', 'remaining', 'after', 'step_pct', 'chain_pct', 'capacity_usd', 'remaining_usd',
                          'expected_usd', 'reset_hours', 'end_hours', 'expires_first',
                          'opportunity_cost', 'line_now_usd', 'used_usd', 'quota_source', 'period_hours', 'elapsed_hours',
                          'priced', 'cost', 'quota_price', 'need_start', 'need_end', 'spend_before_reset',
                          'spend_after_reset', 'stale')
SNAPSHOT_DECISION_KEYS = ('action', 'needs_user_decision', 'margin', 'needs_judgment', 'reason', 'alternatives', 'continuity',
                          'fast', 'fast_reason', 'wait_hours', 'start_after_hours', 'better_if_waiting', 'warnings')


def _round(value, places=4):
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    return round(value, places)


def _snapshot_row(candidate, detail):
    """One candidate as it was scored. `detail` keeps every component, the binding window and the notes; the rest keep
    what the comparison table and radar read. Floats are rounded to 4 places to keep a ledger line small."""
    bundle = {k: v for k, v in (candidate.get('bundle') or {}).items() if v is not None and k != 'launch_model'}
    comps = candidate.get('components') or {}
    excluded = [str(r)[:220] for r in (candidate.get('excluded') or [])[:3]]
    row = {'id': candidate.get('id'), 'bundle': bundle, 'score': _round(candidate.get('score')), 'action': candidate.get('action'),
           'excluded': excluded}
    if candidate.get('estimated_axes'):
        row['estimated_axes'] = list(candidate['estimated_axes'])
    if candidate.get('manual_scores'):
        row['manual_scores'] = copy.deepcopy(candidate['manual_scores'])
    if candidate.get('unscored_named_dispatch'):
        row['unscored_named_dispatch'] = True
    if detail:
        row['components'] = {k: _round(v) for k, v in comps.items()}
        row.update({k: _round(candidate.get(k)) for k in ('expected_pct', 'chain_pct', 'expected_usd', 'chain_usd',
                                                           'expected_hours', 'available_in_hours') if candidate.get(k) is not None})
        cap = candidate.get('capacity')
        if isinstance(cap, dict):
            row['capacity'] = {k: _round(cap[k]) for k in SNAPSHOT_CAPACITY_KEYS if k in cap}
        notes = [str(n)[:200] for n in (candidate.get('notes') or [])[:4]]
        if notes:
            row['notes'] = notes
    elif excluded:
        row['components'] = {}
    else:
        shares = tuple(f'share_{a}' for a in axis_scores.FIVE_AXES)
        keep = ('fit', 'opportunity_cost', *axis_scores.FIVE_AXES, *shares, 'continuity',
                'boost',
                'five_axis_level', 'quota_price', 'need_probability', 'need_probability_end', 'substitute_slack',
                'decided_by', 'earliest_reset_hours', 'reading_at', 'duration_multiplier', 'duration_source')
        # a category score or share of 0 is a real value (the anchor's score, the panel's weakest share), kept
        row['components'] = {k: _round(comps[k]) for k in keep
                             if comps.get(k) is not None and (k in axis_scores.FIVE_AXES or k in shares or comps[k])}
        for k in ('fit', 'opportunity_cost'):
            if k in comps:
                row['components'][k] = _round(comps[k])
    if candidate.get('five_axis_headroom') and not excluded:
        row['five_axis_headroom'] = {a: {k: _round(v) if k != 'model' else v for k, v in h.items()}
                                     for a, h in candidate['five_axis_headroom'].items()}
    econ = candidate.get('economy')
    if isinstance(econ, dict) and not excluded:
        # What the README page reads (round 13): per priced window the candidate's spend, cost and need probability at the
        # start and at the end of that spend, plus the key that separated it from the next row.
        row['economy'] = {'cost': _round(econ.get('cost')), 'need_end_max': _round(econ.get('need_end_max')),
                          'binding_window': econ.get('binding_window'),
                          'decided_by': comps.get('decided_by'),
                          'windows': [{k: _round(v) for k, v in w.items()}
                                      for w in econ.get('windows') or ()]}
        if 'expected_usd' not in row and candidate.get('expected_usd') is not None:
            row['expected_usd'] = _round(candidate['expected_usd'])
        if 'expected_hours' not in row and candidate.get('expected_hours') is not None:
            row['expected_hours'] = _round(candidate['expected_hours'])
        line = candidate_quota_line(candidate)
        if line is not None:
            row['quota_line'] = line
    return row


def candidate_quota_line(candidate):
    """The window this candidate's cost is binding on, as the README quota page reads it (round 16, J1): {window: 'source/window'
    (the key in the snapshot's quota_line.windows), hours: the candidate's hours, spend_usd: its spend of that window}. None
    when the candidate prices no long window. The allocator is the one writer; `economy` and `quota_windows` stay as they are."""
    econ = candidate.get('economy')
    bound = econ.get('binding_window') if isinstance(econ, dict) else None
    if not bound:
        return None
    for w in econ.get('windows') or ():
        if w.get('window') == bound and w.get('quota_source') is not None:
            hours = candidate.get('expected_hours')
            return {'window': f"{w['quota_source']}/{bound}", 'hours': _round(hours if hours is not None else 0.0),
                    'spend_usd': _round(w.get('spend_usd'))}
    return None


def snapshot_quota_line(quota_windows):
    """`snapshot.quota_line` for the README quota page: every priced long window of the decision, keyed by its label, with the
    capacity C, the use U and the clock the page draws (elapsed hours since the period's start, hours to the reset; None =
    never resets). `account` and `window_id` are the usage collector's keys, where the page may look its readings up."""
    windows = {}
    for w in quota_windows or ():
        label = w.get('label')
        if not label:
            continue
        windows[label] = {'label': label, 'capacity_usd': w.get('capacity_usd'), 'used_usd': w.get('used_usd'),
                          'elapsed_hours': w.get('elapsed_hours'), 'reset_in_hours': w.get('reset_in_hours'),
                          'account': w.get('quota_source'), 'window_id': w.get('window')}
    return {'windows': windows} if windows else None


def with_quota_line(result):
    """`result` with the quota-line block the quota page reads, derived when the result does not carry one: a fresh plan
    (a simulation or a recomputed replay) or a snapshot recorded without it still holds `decision.system.quota_windows` and
    each candidate's `economy`, and the block is built from them by the same two functions the snapshot uses. A result
    that carries a block, predates per-candidate `economy`, or has no recorded quota windows, comes back unchanged."""
    if not isinstance(result, dict) or isinstance(result.get('quota_line'), dict):
        return result
    eligible = [c for c in (result.get('ranking') or ()) if isinstance(c, dict) and not c.get('excluded')]
    if eligible and not any(isinstance(c.get('economy'), dict) or isinstance(c.get('quota_line'), dict) for c in eligible):
        return result
    block = snapshot_quota_line(((result.get('decision') or {}).get('system') or {}).get('quota_windows'))
    if block is None:
        return result
    ranking = []
    for c in result.get('ranking') or ():
        line = candidate_quota_line(c) if isinstance(c, dict) and 'quota_line' not in c and not c.get('excluded') else None
        ranking.append({**c, 'quota_line': line} if line is not None else c)
    return {**result, 'quota_line': block, 'ranking': ranking}


def decision_snapshot(result, task=None):
    """The decision as the allocator made it, small enough for one ledger line, or None when `result` has no ranking.

    Holds the task demand, the scoring system in force (weights, harness order) and the ranking
    with each candidate's component scores: every component for the best `SNAPSHOT_FULL` eligible candidates, the
    comparison-table fields for the next `SNAPSHOT_COMPACT`, and the first reasons for `SNAPSHOT_EXCLUDED` blocked ones.
    Readers replay this as written and never re-plan the task against later quota or configuration."""
    ranking = result.get('ranking') if isinstance(result, dict) else None
    if not isinstance(ranking, list):
        return None
    decision = result.get('decision') or {}
    eligible = [c for c in ranking if not c.get('excluded') and c.get('score') is not None]
    blocked = [c for c in ranking if c.get('excluded') or c.get('score') is None]
    system = decision.get('system') or {}
    rows = ([_snapshot_row(c, True) for c in eligible[:SNAPSHOT_FULL]]
            + [_snapshot_row(c, False) for c in eligible[SNAPSHOT_FULL:SNAPSHOT_FULL + SNAPSHOT_COMPACT]]
            + [_snapshot_row(c, False) for c in blocked[:SNAPSHOT_EXCLUDED]])
    d_task = decision.get('task') or {}
    task_in = task if isinstance(task, dict) else (result.get('task') if isinstance(result.get('task'), dict) else {})
    raw_profile = d_task.get('profile') if isinstance(d_task.get('profile'), dict) else (
        task_in.get('profile') if isinstance(task_in.get('profile'), dict) else None)
    clean_profile = {k: v for k, v in raw_profile.items() if k != 'reason'} if isinstance(raw_profile, dict) else None
    task_meta = {k: v for k, v in (
        ('objective', (task_in.get('objective') or '')[:200] or None),
        ('work_kind', task_in.get('work_kind') or d_task.get('kind')),
        ('profile', clean_profile),
    ) if v is not None}
    return {
        'v': 1, 'stage': result.get('stage'), 'source': result.get('source'), 'observed_at': result.get('observed_at'),
        'task': task_meta,
        'choice': result.get('choice'),
        'selected_bundle': result.get('selected_bundle'),
        'expected_usd': _round(result.get('expected_usd')),
        **({'quota_line': quota_line} if (quota_line := snapshot_quota_line(system.get('quota_windows'))) else {}),
        'decision': {**{k: decision[k] for k in SNAPSHOT_DECISION_KEYS if decision.get(k) is not None},
                     'task': {k: v for k, v in d_task.items() if k != 'profile'} | (
                         {'profile': clean_profile} if clean_profile is not None else {}),
                     'system': {'weights': system.get('weights'),
                                'quota_windows': system.get('quota_windows')}},
        'ranking': rows,
        'ranking_total': len(ranking), 'ranking_eligible': len(eligible),
        'ranking_omitted': {'eligible': max(0, len(eligible) - SNAPSHOT_FULL - SNAPSHOT_COMPACT),
                            'excluded': max(0, len(blocked) - SNAPSHOT_EXCLUDED)},
    }
