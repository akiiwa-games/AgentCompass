"""Quota economy and pricing: usd rates, the quota price of a window, and the five-axis economy ordering."""

import json
import math

import fast_modes
import provider_inventory
import axis_scores
import worker_sessions


def capacity_usd(spec, wid):
    """API-priced dollars one full window allows (the account's `capacity_usd`: community data by plan, or a metered
    account's own prepaid budget), or None when it is unknown."""
    value = (spec.get('capacity_usd') or {}).get(wid)
    return float(value) if value else None


EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def scoring_effort(bundle, info):
    """The effort a bundle is scored at: its own, else its model's `fixed_effort` (a harness that cannot set one runs
    at the upstream's fixed level; the launcher still passes no effort)."""
    return bundle.get('effort') or (info or {}).get('fixed_effort')


def unit_rate(spec, wid, model, info=None):
    """Percent of the window this bundle spends per worker-hour, or None when the window has no cited capacity: the hour's
    list-price dollars (the model's `usd_per_hour`, a rate, so effort is in the hours and not here) over the window's
    dollars (`capacity_usd`). A model with no cited usd is unpriced: 0.0, and rule B keeps it out of automatic routing on a
    metered account."""
    capacity, usd = capacity_usd(spec, wid), (info or {}).get('usd_per_hour')
    if capacity and usd:
        return 100 * usd / capacity
    return 0.0 if usd_unpriced(model, info) else None


def bundle_task_usd(spec, model, hours, info=None, context_burn=1.0):
    """Expected API-equivalent task budget in USD for `hours` active worker-hours (`hours * usd_per_hour * context_burn`)."""
    if spec.get('unlimited') or spec.get('unmetered'):
        return 0.0
    return hours * model_usd_per_hour(model, info) * context_burn


def duration_at(info, effort):
    """(multiplier, basis) of the time a task takes this model at `effort`, a multiple of the anchor's Artificial Analysis
    Time/Task at High: the data's `duration.by_effort` and the basis of that entry (a listing at the effort, a fitted arena
    listing, a series effort conversion or a declared same model). A bundle that names no effort runs the model's single
    configuration, which the data keys High. (None, None) when the data has no time at that effort: nothing stands in."""
    five = ((info or {}).get('five_axis') or {}).get('duration') or {}
    key = effort or 'high'
    mult = (five.get('by_effort') or {}).get(key)
    if isinstance(mult, (int, float)) and not isinstance(mult, bool) and mult > 0:
        return float(mult), (five.get('basis_by_effort') or {}).get(key) or {}
    return None, None


def usd_unpriced(model, info=None):
    """True when the model has no cited `usd_per_hour` (the data's value was removed, never defaulted). It prices at 0.0 and
    leaves automatic routing on a metered account."""
    return (info or {}).get('usd_per_hour') is None


def model_usd_per_hour(model, info=None):
    """List-price dollars of one active worker-hour at High (`usd_per_hour`). A model without one prices at 0 instead of a
    silent default: rule B keeps it out of automatic routing on a metered account, and a named dispatch carries a note
    that its cost is not priced."""
    usd = (info or {}).get('usd_per_hour')
    return 0.0 if usd is None else float(usd)


def five_axis_window_recovery(state, cap_usd):
    """One quota window on its own clock (quota economy v3): capacity C, used U (in-flight leases included), elapsed
    fraction e of the period and the reset. `line_now_usd` = C * e is the recovery line: the pace at which the period's
    capacity just runs out. The expected demand still to come at U = C * e equals the remaining capacity (the mean identity
    in `five_axis_need_tail`); `five_axis_need_tail` there is near one half (0.47 to 0.63 with the burst size of v3.1), so
    the line is a reference pace and not an exact probability contour.

    Starter allowances (`hours is None` or `inf`, `reset_hours` inf) never reset: the clock does not run, e = 0. Rolling
    windows that start their clock on first use (`state.get('unstarted')`) are at e = 0 with U = 0. Nothing here reads a
    history: only the period's own readings."""
    if not cap_usd or cap_usd <= 0 or not state or not state.get('known'):
        return None
    full = float(state.get('full', 100.0) or 100.0)
    pool_cap = cap_usd * (full / 100.0)
    rem_pct = float(state.get('remaining', full))
    used_usd = max(0.0, (full - rem_pct) / 100.0 * cap_usd)
    hours = state.get('hours')
    reset_h = state.get('reset_hours')
    if hours is None or reset_h is None or hours == float('inf') or reset_h == float('inf') or float(hours) <= 0:
        return {'capacity_usd': pool_cap, 'used_usd': used_usd, 'elapsed_frac': 0.0, 'line_now_usd': 0.0,
                'period_hours': None, 'reset_hours': float('inf'), 'unstarted': False}
    L = float(hours)
    if state.get('unstarted') and used_usd <= 1e-9:
        return {'capacity_usd': pool_cap, 'used_usd': 0.0, 'elapsed_frac': 0.0, 'line_now_usd': 0.0,
                'period_hours': L, 'reset_hours': L, 'unstarted': True}
    tau = max(0.0, min(L, float(reset_h)))
    elapsed = (L - tau) / L
    return {'capacity_usd': pool_cap, 'used_usd': used_usd, 'elapsed_frac': elapsed, 'line_now_usd': pool_cap * elapsed,
            'period_hours': L, 'reset_hours': tau, 'unstarted': False}


def _beta_continued_fraction(a, b, x):
    """Continued fraction of the incomplete beta function (modified Lentz), the series both tails use."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-13:
            break
    return h


def regularized_incomplete_beta(a, b, x):
    """I_x(a, b) for real a, b > 0 and 0 <= x <= 1 (pure Python; the continuous extension of the negative-binomial tail)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    direct = x < (a + 1.0) / (a + b + 2.0)
    if log_front < -700.0:      # the front factor underflows: the tail on that side is zero to double precision
        return 0.0 if direct else 1.0
    front = math.exp(log_front)
    if direct:
        return min(1.0, front * _beta_continued_fraction(a, b, x) / a)
    return max(0.0, 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b)


def five_axis_need_tail(rec, room_usd):
    """P(the demand that reaches this window before its reset is at least `room_usd`), from the period's own readings.

    Quota economy v3.1 (advisor report v3 section 1.2 with the burst size of v3.1, owner option A, round 15): consumption
    arrives in lumps of size q = `rec['lump_usd']`; the prior is one virtual lump at the recovery-line pace and the
    observation is the U / q lumps seen in the elapsed fraction e of the period. The lumps still to come are negative
    binomial, K ~ NegBin(n + 1, p) with n = U / q, s = q / C and p = (e + s) / (1 + s), so
    P(K * q >= room) = I_{(1 - e) / (1 + s)}(room / q, n + 1). The mean of the demand still to come is
    (U + q) (1 - e) / (e + s), which at U = e C is exactly the remaining capacity C - U for every q (the mean identity).
    `rec` is `five_axis_window_recovery`'s answer plus `lump_usd`, which `FiveAxisEconomy.lump` builds from the account's own
    candidates and the window's own reset clock; a record without it is an error, there is no fallback to C (round 15).
    Only long reset-period windows are priced: a 5-hour window never calls this."""
    lump = min(float(rec['capacity_usd']), float(rec['lump_usd']))      # KeyError when the record has no lump: no default
    if room_usd <= 0:
        return 1.0
    cap = float(rec['capacity_usd'])
    e = float(rec.get('elapsed_frac') or 0.0)
    if cap <= 0 or e >= 1.0 or lump <= 0:
        return 0.0
    s = lump / cap
    return regularized_incomplete_beta(room_usd / lump, float(rec['used_usd']) / lump + 1.0, (1.0 - e) / (1.0 + s))


def five_axis_need_probability(rec, level_usd):
    """pi_w(y): the probability the dollar at cumulative use `level_usd` of this window is needed before its reset."""
    return five_axis_need_tail(rec, float(rec['capacity_usd']) - float(level_usd))


def five_axis_substitute_slack(rec, spend_usd):
    """a_v: posterior survival probability under window `rec`'s negative-binomial arrival model that a displaced task of
    `spend_usd` finds room before `rec`'s reset (`1 - need_tail(C - U - spend)`). Derived from the conjugate
    Gamma(1, q/C)-Poisson arrival process updated by U/q observed lumps over elapsed fraction e; reads only this window's
    own state and no price, keeping the price graph acyclic with zero hand-shaped factors."""
    cap = float(rec['capacity_usd'])
    room = cap - float(rec['used_usd']) - float(spend_usd)
    if cap <= 0 or room <= 0:
        return 0.0
    return 1.0 - five_axis_need_tail(rec, room)


def five_axis_outside_value(alternatives, own_best_fit):
    """V: the expected best fit still available elsewhere when this account's work is displaced. `alternatives` is a list
    of (fit, slack); they are tried by fit descending, each is open with its own slack probability and counts at most
    `own_best_fit`; when none has room the loss is total (0)."""
    value, none_open = 0.0, 1.0
    for fit, slack in sorted(alternatives, key=lambda t: (-t[0], -t[1])):
        value += none_open * slack * min(fit, own_best_fit)
        none_open *= (1.0 - slack)
    return value


def five_axis_upper_hull(points, outside_fit):
    """Upper concave envelope, ascending in spend, of (spend, fit) points above the outside option (0, outside_fit) up to
    the cheapest highest-fit point. Each edge is a step of the account's value ladder; [] when no point beats the outside."""
    best = [(s, f) for s, f in points if s > 0 and f > outside_fit + 1e-15]
    if not best:
        return []
    fmax = max(f for _, f in best)
    top = min(s for s, f in best if f >= fmax - 1e-9)
    chosen, running = [], outside_fit
    for s, f in sorted((p for p in best if p[0] <= top + 1e-12), key=lambda p: (p[0], -p[1])):
        eps = 1e-15 if running == outside_fit else 1e-12
        if f > running + eps:           # dominated points (dearer and no better) cannot be on the envelope
            chosen.append((s, f))
            running = f
    hull = [(0.0, outside_fit)]
    for s, f in chosen:
        while len(hull) >= 2 and ((hull[-1][1] - hull[-2][1]) * (s - hull[-2][0])
                                  <= (f - hull[-2][1]) * (hull[-1][0] - hull[-2][0])):
            hull.pop()
        hull.append((s, f))
    return hull


def five_axis_value_ladder(hulls):
    """The account's merged value ladder from one upper hull per demand point: {'steps': [(rho_j, sigma_j)], 'full_usd'}
    with sigma ascending. A step is one edge of a hull (giving up the dearer end saves its spend and costs its fit, a slope
    sigma = d fit / d USD for this task); rho_j is the share of the full-demand spend still needed once every step
    cheaper in slope than sigma_j has been given up. Equal slopes are one group, priced at the group's rho."""
    steps, full = [], 0.0
    for hull in hulls:
        if len(hull) < 2:
            continue
        full += hull[-1][0]
        for lo, hi in zip(hull, hull[1:]):
            steps.append(((hi[1] - lo[1]) / (hi[0] - lo[0]), hi[0] - lo[0]))
    ladder, given_up = [], 0.0
    steps.sort()
    i = 0
    while i < len(steps):
        slope, j, group = steps[i][0], i, 0.0
        while j < len(steps) and steps[j][0] - slope <= 1e-12 * max(1.0, abs(slope)):
            group += steps[j][1]
            j += 1
        ladder.append(((full - given_up) / full, slope))
        given_up += group
        i = j
    return {'steps': ladder, 'full_usd': full}


def five_axis_quota_price(rec, ladder, level_usd):
    """lambda_w(y): fit lost elsewhere per unit at cumulative use y, integrated over the account's value ladder via the
    negative-binomial depletion tail `need_tail((C - y) / rho_j)`. Rises strictly with used share `y / C` and vanishes
    at the reset (`e -> 1`), while remaining exact-unit invariant (`$ * k` scales `lambda` by `1/k`)."""
    cap = float(rec['capacity_usd'])
    e = float(rec.get('elapsed_frac') or 0.0)
    if cap <= 0 or e >= 1.0:
        return 0.0
    room = cap - float(level_usd)
    price, prev = 0.0, 0.0
    for rho, slope in ladder['steps']:
        price += (slope - prev) * five_axis_need_tail(rec, room / rho)
        prev = slope
    return price


FIVE_AXIS_PRICE_NODES = 48     # nodes of the price table a window's candidates are integrated on (even)


def _quadratic_integral(lam, k, t):
    """Integral over [x_k, x_k + t * step], in units of `step`, of the quadratic through nodes c-1, c, c+1."""
    c = min(max(k, 1), len(lam) - 2)
    a = (lam[c + 1] - lam[c - 1]) / 2.0
    b = (lam[c + 1] - 2.0 * lam[c] + lam[c - 1]) / 2.0

    def prim(s):
        return lam[c] * s + a * s * s / 2.0 + b * s ** 3 / 3.0
    s0 = k - c
    return prim(s0 + t) - prim(s0)


def five_axis_price_table(rec, ladder, spend_max, nodes=FIVE_AXIS_PRICE_NODES):
    """lambda_w sampled at U + k * step, k = 0..nodes, with its cumulative integral (piecewise quadratic through three
    neighbouring nodes), so the cost of any spend up to `spend_max` is one lookup. None when there is nothing to price."""
    if spend_max <= 0 or not ladder['steps']:
        return None
    nodes += nodes % 2
    step = spend_max / nodes
    used = float(rec['used_usd'])
    lam = [five_axis_quota_price(rec, ladder, used + k * step) for k in range(nodes + 1)]
    cum = [0.0]
    for k in range(nodes):
        cum.append(cum[-1] + _quadratic_integral(lam, k, 1.0) * step)
    return {'start': used, 'step': step, 'nodes': nodes, 'lam': lam, 'cum': cum}


def five_axis_table_integral(table, spend_usd):
    """integral of lambda over [U, U + spend] from a `five_axis_price_table`; a spend past its range uses its last node."""
    if table is None or spend_usd <= 0:
        return 0.0
    x = min(spend_usd, table['step'] * table['nodes']) / table['step']
    k = min(int(x), table['nodes'] - 1)
    return table['cum'][k] + _quadratic_integral(table['lam'], k, x - k) * table['step']


def five_axis_spend_cost(rec, ladder, spend_usd, task_hours, spend_max=None, tables=None, fresh_lump=None):
    """cost_i = integral over [U, U + S] of lambda_w(y) dy: the fit the rest of the period loses because this candidate
    takes `spend_usd` of window `rec` (same unit as the fit, so N = F - cost needs no weight). A task that crosses the
    reset is split by time: the part before it is priced on the current period, the part after it on a fresh period
    (n = 0, e = 0). `spend_max` (default `spend_usd`) is the largest spend any candidate takes of this window; `tables`
    caches the price tables built for it ('now', 'fresh'). `fresh_lump` is the new period's untruncated burst size (the
    record's own `lump_usd` when not given). Returns {'cost', 'lambda_start', 'need_start', 'need_end',
    'spend_before', 'spend_after'}."""
    tables = {} if tables is None else tables
    spend = max(0.0, float(spend_usd))
    spend_max = max(spend, float(spend_max or 0.0))
    raw_hours = float(task_hours or 0.0)
    hours = max(1e-9, raw_hours)
    tau = rec.get('reset_hours')
    crossing = raw_hours > 0 and tau is not None and tau != float('inf') and float(tau) < hours - 1e-12
    before = spend * (float(tau) / hours) if crossing else spend
    after = spend - before
    if 'now' not in tables:
        tables['now'] = five_axis_price_table(rec, ladder, spend_max)
    table = tables['now']
    lam0 = table['lam'][0] if table else five_axis_quota_price(rec, ladder, rec['used_usd'])
    cost = five_axis_table_integral(table, before)
    need_start = five_axis_need_probability(rec, rec['used_usd'])
    need_end = five_axis_need_probability(rec, float(rec['used_usd']) + before)
    if crossing and after > 0:
        cap = float(rec['capacity_usd'])
        fresh = {'capacity_usd': cap, 'used_usd': 0.0, 'elapsed_frac': 0.0,
                 'lump_usd': rec['lump_usd'] if fresh_lump is None else fresh_lump}
        if 'fresh' not in tables:
            tables['fresh'] = five_axis_price_table(fresh, ladder, spend_max)
        cost += five_axis_table_integral(tables['fresh'], after)
        need_end = five_axis_need_probability(fresh, min(after, cap))
    return {'cost': cost, 'lambda_start': lam0, 'need_start': need_start, 'need_end': need_end,
            'spend_before': before, 'spend_after': after}


# The demands a window's value ladder is read over: every mix of the three thinking categories (tool_operation 0, so a
# window's price does not move with per-turn speed or image reading, which belong to the current task alone).
FIVE_AXIS_DEMAND_GRID = tuple(
    {'divergence': d1, 'coding': d2, 'aesthetics': d3, 'tool_operation': 0}
    for d1 in range(4) for d2 in range(4) for d3 in range(4) if (d1 or d2 or d3)
)


class FiveAxisEconomy:
    """Quota economy v3 for one plan (advisor report v3, owner decisions D1-D3 of round 13, burst size v3.1 option A).

    `pool` is the account's own candidates, evaluated with the current task's flags neutralised (`rank`'s economy pool), so
    a window's price does not move with local_only, vision, a named model or whether a candidate fits the
    remaining capacity now. The remaining capacity enters only through the window's own state (room, used, reset clock).

    Every long reset-period window w = (quota source, window id) is priced on its own state and clock: its value ladder
    comes from the candidates that draw it and, as the ladder's outside point only, from the startable candidates that do
    not (each with its own `five_axis_substitute_slack`, which reads that candidate's windows and no price). The price graph
    is therefore acyclic and one pass gives the unique answer. 5-hour windows (`short`) keep their feasibility role in
    `evaluate_bundle` and never enter a price, a tail or a ladder here."""

    def __init__(self, pool, accounts, policy, task, extra=()):
        self.grid = FIVE_AXIS_DEMAND_GRID
        targets = [axis_scores.five_axis_targets(d, accounts['_five_axis']['targets']) for d in self.grid]
        self.task_gi = len(self.grid)           # the extra fit slot: the task's own demand (diagnostics only)
        fit_cache = {}
        self.entries = []
        self.spend_max = {}
        self.units = {}
        self.unit_wkeys = {}
        self.points = {}
        self._tables, self._ladders, self._alts, self._recs, self._lumps = {}, {}, {}, {}, {}
        for c in [*pool, *extra]:
            qsrc = provider_inventory.quota_source(c['bundle']['account'], accounts)
            states = {wid: (st, cap) for wid, (_, st, cap, _) in (c.get('_five_window_states') or {}).items()
                      if not st.get('short')}
            if not states:
                continue        # no long-window state: nothing to ladder and never an outside point (a data error upstream)
            values = c.get('_five_values') or {}
            key = json.dumps(values, sort_keys=True)
            if key not in fit_cache:
                fit_cache[key] = tuple(
                    axis_scores.five_axis_fit(values, d, t)[0] if all(values.get(a) is not None for a, w in d.items() if w > 0) else -1.0
                    for d, t in zip(self.grid, targets)
                )
            w_spend = c.get('_five_window_spend')
            spend = float(w_spend) if w_spend is not None else float(c.get('expected_usd') or 0.0)
            hours = 0.0 if c.get('_five_cadence_instant') else float(c.get('_five_econ_hours') or c.get('expected_hours') or 0.0)
            entry = {'cand': c, 'qsrc': qsrc, 'startable': not c.get('_five_long_wait'),
                     'unmetered': bool(c.get('_five_unmetered')), 'metered': bool(c.get('_five_metered')),
                     'spend': spend, 'states': states, 'hours': hours,
                     'fits': fit_cache[key] + (float(c['components'].get('fit') or 0.0),),
                     'unit': (qsrc, frozenset(states))}
            self.entries.append(entry)
            fast = c.get('_five_fast_candidate')
            peak = max(entry['spend'], float(fast['step_usd']) if fast else 0.0)
            for wid in states:
                self.spend_max[(qsrc, wid)] = max(self.spend_max.get((qsrc, wid), 0.0), peak)
        for e in self.entries:
            if e['startable'] and not e['unmetered'] and not e['metered']:
                self.units.setdefault(e['unit'], []).append(e)
                self.unit_wkeys[e['unit']] = frozenset((e['qsrc'], wid) for wid in e['states'])
        drawn = {}
        for e in self.entries:
            for wid in e['states']:
                drawn.setdefault((e['qsrc'], wid), []).append(e)
        for wkey, rows in drawn.items():        # a window nobody can start on now is laddered by its own waiting candidates
            self.points[wkey] = [e for e in rows if e['startable']] or rows

    def rec(self, entry, wid):
        st, cap = entry['states'][wid]
        return self.record((entry['qsrc'], wid), st, cap)

    def record(self, wkey, st, cap):
        """The window's recovery record with its burst size `lump_usd` attached (None when the window is unreadable)."""
        rec = five_axis_window_recovery(st, cap)
        if rec is not None:
            rec['lump_usd'] = self.lump(wkey, rec['reset_hours'])
        return rec

    def lump(self, wkey, reset_hours=None):
        """q_w, the window's burst size (advisor v3.1, option A, owner round 15): the average over the demand grid of what one
        task like the current one spends of this window on the account's own best candidate for that demand (the cheapest
        of the highest fit), cut at the window's own reset: a task longer than the hours to the reset spends only the
        share `reset_hours / hours` of its dollars before it. `reset_hours` None means the untruncated size (a fresh
        period). Capped by the window's capacity. It reads only this account's candidates, their public `usd_per_hour` and
        durations, and the reset clock: no 5-hour window, no outside point, no constant."""
        tau = float('inf') if reset_hours is None else float(reset_hours)
        key = (wkey, tau)
        got = self._lumps.get(key)
        if got is None:
            own = [e for e in (self.points.get(wkey) or []) if e['spend'] > 0]
            total = 0.0
            for gi in range(len(self.grid)):
                if own:
                    top = max(own, key=lambda e: (round(e['fits'][gi], 9), -e['spend']))
                    share = 1.0 if top['hours'] <= 0 or tau == float('inf') else min(1.0, tau / top['hours'])
                    total += top['spend'] * share
            cap = max((float(e['states'][wkey[1]][1]) for e in own), default=0.0)
            got = self._lumps[key] = min(cap, total / len(self.grid)) if own else 0.0
        return got

    def _alt(self, unit, gi):
        """(fit, slack) of the best candidate of an outside unit at demand point gi (ties: cheapest)."""
        got = self._alts.get((unit, gi))
        if got is None:
            rows = self.units[unit]
            best = max(rows, key=lambda e: (round(e['fits'][gi], 9), -e['spend']))
            slack = 1.0
            for wid in best['states']:
                rec = self.rec(best, wid)
                if rec is not None:
                    slack = min(slack, five_axis_substitute_slack(rec, best['spend']))
            got = self._alts[(unit, gi)] = (best['fits'][gi], slack)
        return got

    def _outside(self, wkey, gi):
        return [self._alt(u, gi) for u in self.units if wkey not in self.unit_wkeys[u]]

    def ladder(self, wkey):
        """The account's value ladder for window `wkey`, averaged over the demand grid (owner D1)."""
        got = self._ladders.get(wkey)
        if got is None:
            own = self.points.get(wkey) or []
            hulls = []
            for gi in range(len(self.grid)):
                pts = [(e['spend'], e['fits'][gi]) for e in own if e['spend'] > 0]
                if pts:
                    outside = five_axis_outside_value([(f, a) for f, a in self._outside(wkey, gi) if f > 0],
                                                      max(f for _, f in pts))
                    hulls.append(five_axis_upper_hull(pts, outside))
            got = self._ladders[wkey] = five_axis_value_ladder(hulls)
        return got

    def outside_at_task(self, wkey):
        """(V, own best fit, P(some outside candidate has room)) at the task's own demand, for the report."""
        own = [e for e in (self.points.get(wkey) or []) if e['spend'] > 0]
        alts = [(f, a) for f, a in self._outside(wkey, self.task_gi) if f > 0]
        best = max((e['fits'][self.task_gi] for e in own), default=0.0)
        none_open = 1.0
        for _, a in alts:
            none_open *= (1.0 - a)
        return five_axis_outside_value(alts, best), best, 1.0 - none_open

    def window_cost(self, wkey, rec, spend, econ_hours):
        tables = self._tables.setdefault((wkey, round(rec['used_usd'], 6), round(rec['elapsed_frac'], 9),
                                          round(rec['capacity_usd'], 6), round(rec['lump_usd'], 6)), {})
        return five_axis_spend_cost(rec, self.ladder(wkey), spend, econ_hours, self.spend_max.get(wkey, spend), tables,
                                    fresh_lump=self.lump(wkey))

    def report(self):
        """One block per priced window for the plan's `system.quota_windows` (and the README page): its state and its
        price now. Only windows some eligible candidate draws appear."""
        out = []
        for wkey in sorted(self.points):
            e = self.points[wkey][0]
            rec = self.rec(e, wkey[1])
            if rec is None:
                continue
            ladder = self.ladder(wkey)
            outside, best, slack = self.outside_at_task(wkey)
            period = rec['period_hours']
            out.append({
                'label': f'{wkey[0]}/{wkey[1]}', 'quota_source': wkey[0], 'window': wkey[1],
                'capacity_usd': round(rec['capacity_usd'], 2), 'used_usd': round(rec['used_usd'], 2),
                'recovery_line_usd': round(rec['line_now_usd'], 2),
                'period_hours': None if period is None else round(period, 2),
                'elapsed_hours': None if period is None else round(period - rec['reset_hours'], 2),
                'reset_in_hours': None if rec['reset_hours'] == float('inf') else round(rec['reset_hours'], 2),
                'need_probability': round(five_axis_need_probability(rec, rec['used_usd']), 4),
                'lump_usd': round(rec['lump_usd'], 2),
                'quota_price': round(five_axis_quota_price(rec, ladder, rec['used_usd']), 6) if ladder['steps'] else 0.0,
                'ladder_steps': len(ladder['steps']), 'outside_fit': round(outside, 4), 'best_fit': round(best, 4),
                'substitute_slack': round(slack, 4), 'candidates': len(self.points[wkey]),
            })
        return out


def five_axis_make_fast(c, fast_cand, reason):
    """Turn candidate `c` into its fast-mode variant at the vendor's burn and speed (a separate candidate, priced at its
    real spend; round 13). The variant gets its own id (`fast_modes.variant_id`) and remembers the bundle it launches as
    (`base_id`), so a pinned bundle and the alternatives name exactly one variant (round 14)."""
    c['base_id'] = c['id']
    c['id'] = fast_modes.variant_id(c['id'])
    c['bundle']['fast'] = True
    c['components']['fast'] = True
    if reason:
        c['components']['fast_reason'] = reason
    c['expected_hours'] = round(fast_cand['hours'], 2)
    c['duration_hours'] = round(fast_cand['hours'], 4)
    c['expected_usd'] = round(fast_cand['step_usd'], 2)
    c['chain_usd'] = round(fast_cand['chain_usd'], 2)
    c['notes'].append(f"fast mode ({fast_cand['price_multiplier']:g}x burn, up to {fast_cand['speed_multiplier']:g}x "
                      f"output speed: the vendor's upper bound, applied to the whole duration)")


def apply_five_axis_economy(candidates, eligible, task, accounts, policy, pool=None):
    """Quota economy v3 (advisor report v3; owner decisions of round 13): cost_i = integral over the candidate's own spend
    of the window's quota price lambda_w(y) dy, N = F - cost, with lambda_w from the account's own value ladder (averaged
    over the 63-point demand grid) and the period's own need probability pi_w(y). Only long reset-period windows price; the
    5-hour windows keep their feasibility role. A multi-window candidate pays its most expensive window (never both).
    Fast-mode variants are separate candidates appended to `candidates` and `eligible` at their real spend (a forced fast
    replaces the base). Returns the per-window report for the plan's `system.quota_windows`."""
    # The ladders, outside points and lumps come from the account's own pool (`rank` evaluates it with the task's flags
    # neutralised, owner round 16): neither a task flag nor whether a candidate fits the remaining capacity reaches a price.
    econ = FiveAxisEconomy(eligible if pool is None else pool, accounts, policy, task)
    base_hours = max(1e-6, float(task['hours'] or 0.0))

    def eval_windows(c, w_states, own_qsrc, spend, chain_usd, econ_h):
        rows = []
        for wid, (wspec, st, cap_usd_w, _) in w_states.items():
            rec = (five_axis_window_recovery(st, cap_usd_w) if st.get('short')
                   else econ.record((own_qsrc, wid), st, cap_usd_w))
            if rec is None:
                continue
            rem = st['remaining']
            rem_usd = rem * cap_usd_w / 100.0
            step_pct = (100.0 * spend / cap_usd_w) if cap_usd_w > 0 else 0.0
            chain_pct = (100.0 * chain_usd / cap_usd_w) if cap_usd_w > 0 else 0.0
            reset_h = rec['reset_hours']
            end_h = st['end_hours']
            period = rec['period_hours']
            row = {
                'window': wid, 'quota_source': own_qsrc,
                'short': bool(st.get('short')),
                'unstarted': bool(rec.get('unstarted')),
                'remaining': rem, 'full': st['full'], 'after': rem - step_pct, 'step_pct': step_pct, 'chain_pct': chain_pct,
                'capacity_usd': round(cap_usd_w, 2), 'remaining_usd': round(rem_usd, 2),
                'reserved_usd': round(st.get('reserved_usd', 0.0), 2),
                'expected_usd': round(spend, 2), 'chain_usd': round(chain_usd, 2), 'after_usd': round(rem_usd - spend, 2),
                'used_usd': round(rec['used_usd'], 2), 'line_now_usd': round(rec['line_now_usd'], 2),
                'period_hours': None if period is None else round(period, 2),
                'elapsed_hours': None if period is None else round(period - reset_h, 2),
                'reset_hours': None if reset_h == float('inf') else reset_h,
                'end_hours': None if end_h == float('inf') else end_h, 'expires_first': end_h < reset_h,
                'stale': st['stale'], 'priced': False, 'cost': 0.0, 'opportunity_cost': 0.0,
                **({k: st[k] for k in ('natural_reset_hours', 'expected_reset_at') if k in st}),
            }
            if not row['short'] and spend > 0:
                got = econ.window_cost((own_qsrc, wid), rec, spend, econ_h)
                unit_cost = float((1.0 + float(got['cost'])) - 1.0)
                row.update(priced=True, cost=unit_cost, opportunity_cost=unit_cost,
                           quota_price=round(got['lambda_start'], 6), need_start=round(got['need_start'], 4),
                           need_end=round(got['need_end'], 4), spend_before_reset=round(got['spend_before'], 2),
                           spend_after_reset=round(got['spend_after'], 2))
            rows.append(row)
        if not rows:
            return None
        binding = max(rows, key=lambda r: (r['cost'], not r['short'], -r['after']))
        priced = [r for r in rows if r['priced']]
        return binding, {
            'windows': [{k: r[k] for k in ('quota_source', 'window', 'capacity_usd', 'used_usd', 'period_hours',
                                           'elapsed_hours', 'cost', 'quota_price', 'need_start',
                                           'need_end', 'spend_before_reset', 'spend_after_reset')}
                        | {'spend_usd': r['expected_usd'],
                           'reset_in_hours': None if r['reset_hours'] is None else round(r['reset_hours'], 2)}
                        for r in priced],
            'cost': binding['cost'], 'need_end_max': max((r['need_end'] for r in priced), default=0.0),
            'binding_window': binding['window'] if binding['priced'] else None,
        }

    def price(c, w_states, own_qsrc, spend, chain_usd, econ_h):
        got = eval_windows(c, w_states, own_qsrc, spend, chain_usd, econ_h)
        binding, summary = got if got else (None, {'windows': [], 'cost': 0.0, 'need_end_max': 0.0, 'binding_window': None})
        if binding is not None:
            c['capacity'] = binding
            c['expected_pct'] = round(binding['step_pct'], 2)
            c['chain_pct'] = round(binding['chain_pct'], 2)
        cost = float(summary['cost'])
        c['economy'] = summary
        c['components']['opportunity_cost'] = cost
        c['components']['quota_price'] = binding.get('quota_price', 0.0) if binding and binding['priced'] else 0.0
        c['components']['need_probability'] = binding.get('need_start', 0.0) if binding and binding['priced'] else 0.0
        c['components']['need_probability_end'] = summary['need_end_max']
        resets = [w['reset_in_hours'] for w in summary['windows'] if w['reset_in_hours'] is not None]
        c['components']['earliest_reset_hours'] = min(resets) if resets else None
        if binding is not None and binding['priced']:
            _, _, slack = econ.outside_at_task((own_qsrc, binding['window']))
            c['components']['substitute_slack'] = round(slack, 4)
        if isinstance(c.get('five_axis_price'), dict):
            c['five_axis_price'].update(cost=cost, quota_price=c['components']['quota_price'],
                                        need_probability=c['components']['need_probability'],
                                        need_probability_end=c['components']['need_probability_end'])

    variants = []
    for c in list(eligible):
        w_states = c.get('_five_window_states') or {}
        own_qsrc = provider_inventory.quota_source(c['bundle']['account'], accounts)
        w_spend_override = c.get('_five_window_spend')
        cad_instant = bool(c.get('_five_cadence_instant'))
        econ_h = 0.0 if cad_instant else float(c.get('_five_econ_hours') or base_hours)
        step_usd = float(w_spend_override) if w_spend_override is not None else float(c.get('expected_usd') or 0.0)
        chain_usd = float(w_spend_override) if w_spend_override is not None else float(c.get('chain_usd') or step_usd)
        fast_cand = c.get('_five_fast_candidate') if w_states else None
        if fast_cand and task.get('force_fast'):
            five_axis_make_fast(c, fast_cand, 'user_override')
            if task.get('worker') and not worker_sessions.worker_worn(task['worker'], policy):
                wb = task['worker'].get('bundle')
                w_ok = not (task.get('read_only_review') and task['worker'].get('read_only_review') is False)
                f_resumes = w_ok and (wb == c['id'] or (wb == c.get('base_id') and bool(task['worker'].get('fast'))))
                c['action'] = 'resume' if f_resumes else 'switch'
                c['components']['continuity'] = (
                    round(task['five_axis_continuity'], 4) if f_resumes and task['coupling'] in ('medium', 'high') else 0.0
                )
            price(c, w_states, own_qsrc, fast_cand['step_usd'], fast_cand['chain_usd'], fast_cand['econ_hours'])
            continue
        if fast_cand:
            twin = {**c, 'bundle': dict(c['bundle']), 'components': dict(c['components']), 'notes': list(c['notes']),
                    'excluded': list(c['excluded']),
                    'five_axis_price': dict(c['five_axis_price']) if isinstance(c.get('five_axis_price'), dict) else None}
            five_axis_make_fast(twin, fast_cand, 'net_score')
            if task.get('worker') and not worker_sessions.worker_worn(task['worker'], policy):
                wb = task['worker'].get('bundle')
                w_ok = not (task.get('read_only_review') and task['worker'].get('read_only_review') is False)
                f_resumes = w_ok and (wb == twin['id'] or (wb == twin.get('base_id') and bool(task['worker'].get('fast'))))
                twin['action'] = 'resume' if f_resumes else 'switch'
                twin['components']['continuity'] = (
                    round(task['five_axis_continuity'], 4) if f_resumes and task['coupling'] in ('medium', 'high') else 0.0
                )
            price(twin, w_states, own_qsrc, fast_cand['step_usd'], fast_cand['chain_usd'], fast_cand['econ_hours'])
            variants.append(twin)
        price(c, w_states, own_qsrc, step_usd, chain_usd, econ_h)
    candidates.extend(variants)
    eligible.extend(variants)
    report = econ.report()
    for c in candidates:
        for priv in ('_five_axis_windows', '_five_window_states', '_five_econ_hours', '_five_chain_hours',
                     '_five_context_burn', '_five_fast_candidate', '_five_values', '_five_short_reasons',
                     '_five_long_wait', '_five_unmetered', '_five_metered',
                     '_five_window_spend', '_five_cadence_instant', '_five_blind_msg'):
            c.pop(priv, None)
    return report
