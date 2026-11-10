"""Write measured model rows into references/accounts.json and the reference fixture.

`python -X utf8 apply_rows.py [--data DIR] [--dry-run] [model-id ...]` (default: every model in tiers.NAMES)
`python -X utf8 apply_rows.py --estimates-only [--dry-run]`: only re-estimate the scores without a direct measurement on
the rows as they are (no tier refit), see `estimate`.

A row that exists keeps its prices and modalities; its tier (and coding, when the model is on WebDev) is replaced by the
measurement and `score_estimated` is dropped. A new row starts from the router's own default profile for that id
(auto_bundles.default_profile: the same-series row, else the generic row) and takes the measurement on top. A model
without a tier measurement takes the tier of its nearest same-series sibling, else the data's unmeasured default
(accounts.json `unmeasured_models`).

Scores without a direct measurement (round 058, user 2026-11-10: "后续同系列新出，缺少实测数据的情况下，先按照同系列的全分数
进行折算，不要留下任何一个默认值或者fallback"): a row's text capabilities (visual and gui included), subjective scores and
usd_per_hour that are still the router default the row started from (or an earlier conversion or estimate) are filled by
`estimate`, the same way for every model:

- Series conversion, for a row with a same-series sibling (calibration/series.py) that carries a direct value for the
  score. The base is the nearest such sibling (series.nearest: the AA Intelligence Index, else the cited boards both have, tier only when they share no reading). A score moves from the base
  value along the calibration's fitted slope by the difference of a cited reading both rows have, read the same way for
  both and not clamped: the score's own AA board (the lowest leave-one-out error among those), else the AA Intelligence
  Index; the result stays inside the score range the fit's measured points reach, so a 0-1 score cannot leave it. With no
  shared reading the base value is taken as it is. usd_per_hour is the base's value times the ratio of the two models'
  cited API list prices over the full community token mix (public-measurements.json `capacity.token_mix`).
- For a row with no such sibling, a public-data estimate: from Artificial Analysis readings (candidates in PREDICTORS),
  only where the fit shows out-of-sample skill on the rows that carry the score as a value: a linear fit passing
  five_axis.Scorer.fit (>= MIN_FIT_POINTS distinct points, positive slope) whose leave-one-out errors beat both the
  leave-one-out sample mean and the router default rows on the same rows by more than one standard error of the paired
  difference in squared error; the skilled candidate with the lowest leave-one-out error is used, a reading outside the
  sample is clamped to it (`x_clamped`). Otherwise the generic row stays (this is the only place one can).

Each value is recorded in the row's `score_estimates` (method, base or predictor, readings, fit, replaced value). A value
this step did not write (hand-edited, or measured: coding from WebDev) is never overwritten and its record is dropped; a
direct measurement replaces a converted value on the next run. `refresh.py` runs this step after rebuilding the
measurements.

Regenerate the capability views afterwards: `python -X utf8 ../scripts/capability_catalog.py`.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np

import efforts
import five_axis
import measurements
import series
import sources
import tiers

sys.path.insert(0, str(sources.REPO / 'scripts'))
import auto_bundles  # noqa: E402

FILES = [sources.ACCOUNTS, sources.REPO / 'scripts' / 'fixtures' / 'reference-accounts.json']
NEW_ROW_NOTE = ('New row; cost, modalities, capabilities, subjective scores and usd_per_hour without a direct '
                'measurement are filled by `estimate` ({source}; see score_estimates).')
# Scores the public-data estimate fills for a row with no same-series sibling. `visual` and `gui` admit a modality (image
# input, GUI control) and are never inferred from general ability; the series conversion takes them from the base.
ESTIMATED = {'capabilities': ('reasoning', 'coding', 'exploration', 'anti_hallucination'),
             'subjective': ('divergence', 'global_rigor', 'module_precision')}
SCORES = {'capabilities': ESTIMATED['capabilities'] + ('visual', 'gui'), 'subjective': ESTIMATED['subjective']}
USD = 'usd_per_hour'
MEASURED = ('capabilities.coding',)
WEBDEV_CODING = 'coding = anchor regression on WebDev'   # written into `basis` by `rows` when it measures coding     # scores this calibration also measures directly (coding_map on WebDev)
# Score path -> candidate AA predictors (the lowest leave-one-out error among the skilled ones wins; the order only
# breaks exact ties): the AA board measurements.py maps to the matching component
# (aa.terminalbench_4 -> terminal coding, aa.omniscience_index -> anti_hallucination, aa.lcr -> long context, which
# `exploration` scales with in the router), then the Intelligence Index. The router's tier is no predictor (round 6).
PREDICTORS = {
    'capabilities.reasoning': ('aa.index',),
    'capabilities.coding': ('aa.terminalbench_4', 'aa.index'),
    'capabilities.exploration': ('aa.lcr', 'aa.index'),
    'capabilities.anti_hallucination': ('aa.omniscience_index', 'aa.index'),
    'subjective.divergence': ('aa.index',),
    'subjective.global_rigor': ('aa.lcr', 'aa.omniscience_index', 'aa.index'),
    'subjective.module_precision': ('aa.terminalbench_4', 'aa.index'),
}


def coding_map(fitter):
    xs = np.array([fitter.boards['webdev'][n][0] for n in tiers.ANCHORS.values()])
    ys = np.array([fitter.models[k]['capabilities']['coding'] for k in tiers.ANCHORS])
    a, b = np.polyfit(xs, ys, 1)
    return lambda x: round(float(np.clip(a * x + b, 0.0, 1.0)), 2), float(np.corrcoef(xs, ys)[0, 1])


def rows(fitter, keys, acc, ef, measured=None):
    """Measured rows for `keys`; `measured` (optional dict) receives {key: {score path}} for the scores measured here."""
    measured = {} if measured is None else measured
    coding, r = coding_map(fitter)
    unmeasured_default = acc['unmeasured_models']['generic']
    meas = json.loads(Path(measurements.OUT).read_text(encoding='utf-8')) if Path(measurements.OUT).exists() else {}
    index = {k: v[0] for k in acc['models'] if (v := readings(k, meas).get('aa.index'))}
    out = {}
    for key in keys:
        e = fitter.fit(key)
        existing = acc['models'].get(key)
        if isinstance(existing, dict):
            row, note = copy.deepcopy(existing), ''
        else:
            # Started from the default row the gap check reads (recorded values put back), so `estimate` fills it.
            prof = auto_bundles.default_profile(key, without_estimates(acc))
            row = {k: v for k, v in prof['spec'].items() if k != 'basis'}
            note = ' ' + NEW_ROW_NOTE.format(source=prof['source'] + (f" {prof['target']}" if prof.get('target') else ''))
        if e['method'] == 'unmeasured':
            # A same-series sibling with a measured tier gives its tier (series conversion; no shared reading moves it,
            # since a model with an AA reading is measured by tiers.py); only a model without one gets the generic tier.
            models_ = dict(acc['models'], **{key: row})
            sibs = series.groups(dict(acc, models=models_)).get(key, [])
            bases = [b for b in series.nearest(key, sibs, index, series.boards(meas), {})
                     if not models_[b].get('score_estimated') and isinstance(models_[b].get('tier'), (int, float))]
            row['score_estimated'] = True
            if bases:
                row['tier'] = models_[bases[0]]['tier']
                row['basis'] = (f"{tiers.UNMEASURED.get(key, 'No comparable public measurement')}. tier = series "
                                f"conversion from {bases[0]} (no shared public reading)." + note)
            else:
                row['tier'] = unmeasured_default['tier']
                row['basis'] = (f"{tiers.UNMEASURED.get(key, 'No comparable public measurement')}. tier = the unmeasured "
                                'default (accounts.json unmeasured_models.generic; no same-series sibling), below the hard '
                                'floor.' + note)
        else:
            high, corr = ef.high_tier(key, e['tier']) if e['method'] == 'primary' else (e['tier'], {})
            row['tier'] = high
            row.pop('score_estimated', None)
            webdev = dict(e['sources']).get('webdev')
            if webdev:
                row.setdefault('capabilities', {})['coding'] = coding(webdev[0])
                measured.setdefault(key, set()).add('capabilities.coding')
                (row.get('score_estimates') or {}).pop('capabilities.coding', None)
                if row.get('score_estimates') == {}:
                    row.pop('score_estimates')
            at_high = (f" At High: {high} = {e['tier']} minus the Artificial Analysis effort gain of the listed variant over High "
                       f'beyond that of the anchors at the same tier ({corr}; calibration/efforts.py), so the effort curve is not '
                       'counted twice.') if corr and high != e['tier'] else ''
            row['basis'] = tiers.basis(e) + at_high + (f' {WEBDEV_CODING} (r={r:.2f}).' if webdev else '') + note
        out[key] = row
    return out


def get(row, path):
    if path == USD:
        return row.get(USD)
    group, d = path.split('.')
    return (row.get(group) or {}).get(d)


def put(row, path, value):
    if path == USD:
        row[USD] = value
    else:
        group, d = path.split('.')
        row.setdefault(group, {})[d] = value
        if path == 'capabilities.visual' and row.get('visual') is not False and 'visual' in row:
            row['visual'] = value          # the legacy field stays equal to the vector


def without_estimates(acc):
    """`acc` with every recorded value put back to the one it replaced, so the default rows the router derives from
    sibling rows never read a conversion or an estimate."""
    models = {}
    for k, row in (acc.get('models') or {}).items():
        if isinstance(row, dict) and row.get('score_estimates'):
            row = copy.deepcopy(row)
            for p, e in row.pop('score_estimates').items():
                if get(row, p) == e.get('value'):
                    if e.get('prior') is None and p == USD:
                        row.pop(USD, None)
                    elif e.get('prior') is not None:
                        put(row, p, e['prior'])
        models[k] = row
    return dict(acc, models=models)


def prior_specs(key, acc):
    """The default rows the router starts `key` from: its default profile without its own row (the same-series row's
    values, else the generic row), and the generic rows."""
    rest = dict(acc, models={k: v for k, v in (acc.get('models') or {}).items() if k != key})
    return [auto_bundles.default_profile(key, rest)['spec']] + [
        v for v in (acc.get('unmeasured_models') or {}).values() if isinstance(v, dict)]


def unmeasured(key, row, acc, measured=()):
    """{path: value it replaced} for what `row` has no direct measurement for: a recorded value still equal to what this
    step wrote (an edited value is a measurement and wins), a missing usd_per_hour, or a score still equal to the default
    row it started from (the group's estimated scores, apart from those measured here, all match one default row, and
    the score itself equals it)."""
    measured = set(measured) | ({'capabilities.coding'} if WEBDEV_CODING in (row.get('basis') or '') else set())
    gaps = {}
    for p, e in (row.get('score_estimates') or {}).items():
        if p not in measured and get(row, p) == e.get('value'):
            gaps[p] = e.get('prior')
    if USD not in gaps and not isinstance(row.get(USD), (int, float)):
        gaps[USD] = None
    for spec in prior_specs(key, acc):
        for group, dims in ESTIMATED.items():
            have, prior = row.get(group) or {}, spec.get(group) or {}
            core = [d for d in dims if f'{group}.{d}' not in MEASURED]
            if prior and all(d in prior and (have.get(d) == prior[d] or f'{group}.{d}' in gaps) for d in core):
                for d in SCORES[group]:
                    p = f'{group}.{d}'
                    if p not in measured and p not in gaps and d in prior and have.get(d) == prior[d]:
                        gaps[p] = prior[d]
    return gaps


def readings(key, meas):
    """{AA source: (value, citation)} for the AA readings of `key` in public-measurements.json."""
    wanted = {n for preds in PREDICTORS.values() for n in preds}
    out = {}
    for entries in ((meas.get('models') or {}).get(key) or {}).values():
        for e in entries or ():
            src, v = e.get('source'), e.get('value')
            if src in wanted and src not in out and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[src] = (v, {k: e[k] for k in ('source', 'as_of', 'listed_as') if e.get(k)})
    return out


def _rmse(errors):
    return float(np.sqrt(np.mean(np.square(errors)))) if errors else None


def skill(points):
    """(fit, check) for [(x, y, prior)]: the five-axis fit over the distinct points and its leave-one-out check against
    the sample mean and the priors on the same rows. `check['skill']` is True only when the fit beats both."""
    pts = sorted(set(points), key=lambda p: (p[0], p[1], -1 if p[2] is None else p[2]))
    fit = five_axis.Scorer.fit([(x, y) for x, y, _ in pts])
    if not fit:
        return None, None
    fit_err, mean_err = [], []
    for i, (x, y, _) in enumerate(pts):
        rest = pts[:i] + pts[i + 1:]
        xs, ys = np.array([p[0] for p in rest]), np.array([p[1] for p in rest])
        b, a = np.polyfit(xs, ys, 1)
        guess = float(np.clip(a + b * float(np.clip(x, xs.min(), xs.max())), ys.min(), ys.max()))
        fit_err.append(guess - y)
        mean_err.append(float(ys.mean()) - y)
    prior_err = [pr - y for _, y, pr in pts if pr is not None]
    fit_on_prior = [e for e, (_, _, pr) in zip(fit_err, pts) if pr is not None]
    check = {'loo_rmse': _rmse(fit_err), 'mean_loo_rmse': _rmse(mean_err), 'prior_rmse': _rmse(prior_err),
             'beats_mean': clearly_better(fit_err, mean_err),
             'beats_prior': clearly_better(fit_on_prior, prior_err) if prior_err else True}
    check['skill'] = check['beats_mean'] and check['beats_prior']
    return fit, {k: round(v, 4) if isinstance(v, float) else v for k, v in check.items()}


def clearly_better(errors, baseline):
    """True when `errors` beat `baseline` on the same rows by more than one standard error of the paired difference in
    squared error (the one-standard-error rule of cross-validation): a gain smaller than its own noise is no skill."""
    d = np.square(np.asarray(baseline, float)) - np.square(np.asarray(errors, float))
    if len(d) < 2:
        return False
    return bool(d.mean() > d.std(ddof=1) / np.sqrt(len(d)))


def token_mix(meas):
    mix = (meas.get('capacity') or {}).get('token_mix') or {}
    shares = {k: v for k, v in mix.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    return shares if sum(shares.values()) > 0 else {}


def list_price(key, meas):
    return five_axis.pick_price(((meas.get('models') or {}).get(key) or {}).get('price'))


def convert(path, target, base, models, reads, fits, meas):
    """(value, provenance) of the series conversion of `path` from `base` (module docstring), or (None, None) when the
    base has no value or usd_per_hour has no cited list price on both sides."""
    base_value = get(models[base], path)
    if not isinstance(base_value, (int, float)) or isinstance(base_value, bool):
        return None, None
    prov = {'method': 'calibration/apply_rows.py series conversion', 'base_model': base, 'base_value': base_value}
    if path == USD:
        mix, prices = token_mix(meas), {m: list_price(m, meas) for m in (target, base)}
        if not mix or any(p is None or any(not isinstance(p['value'].get(c), (int, float)) for c in mix)
                          for p in prices.values()):
            return None, None
        cost = {m: sum(prices[m]['value'][c] * share for c, share in mix.items()) for m in prices}
        if cost[base] <= 0:
            return None, None
        ratio = cost[target] / cost[base]
        return round(base_value * ratio, 3), {**prov, 'ratio': round(ratio, 4), 'token_mix': mix,
                                              'prices': {m: {k: p[k] for k in ('value', 'source', 'as_of', 'url') if p.get(k)}
                                                         for m, p in prices.items()}}
    both = [n for n in PREDICTORS.get(path, ()) if n in reads[target] and n in reads[base] and fits[(path, n)][0]]
    own = [n for n in both if n != 'aa.index']
    name = min(own, key=lambda n: fits[(path, n)][1]['loo_rmse']) if own else ('aa.index' if 'aa.index' in both else None)
    if not name:
        return base_value, {**prov, 'predictor': None}
    f = fits[(path, name)][0]
    (x, cite), (xb, cite_b) = reads[target][name], reads[base][name]
    raw = base_value + f['b'] * (x - xb)
    value = round(float(np.clip(raw, f['y_min'], f['y_max'])), 2)
    prov.update(predictor=name, x=x, x_base=xb, input=cite, input_base=cite_b, shift=round(f['b'] * (x - xb), 4),
                fit={k: round(v, 4) for k, v in f.items()})
    if not f['y_min'] <= raw <= f['y_max']:
        prov['y_clamped'] = f['y_min'] if raw < f['y_min'] else f['y_max']
    return value, prov


def estimate(models, meas, acc, measured=None):
    """Fill, in place, what each row has no direct measurement for (module docstring): the series conversion where a
    same-series sibling carries a direct value, else the skill-gated public-data estimate. Returns the estimate fits
    {(path, predictor): (fit, check)}."""
    measured = measured or {}
    acc = without_estimates(acc)
    rows_ = {k: v for k, v in models.items() if isinstance(v, dict) and ('tier' in v or 'capabilities' in v)}
    gaps = {k: unmeasured(k, r, acc, measured.get(k, ())) for k, r in rows_.items()}
    reads = {k: readings(k, meas) for k in rows_}
    priors = {k: (prior_specs(k, acc) or [{}])[0] for k in rows_}
    sibs = series.groups(dict(acc, models=rows_))
    index = {k: reads[k]['aa.index'][0] for k in rows_ if 'aa.index' in reads[k]}
    tier = {k: r.get('tier') for k, r in rows_.items()}
    board = series.boards(meas)
    fits = {}
    for group, dims in ESTIMATED.items():
        for d in dims:
            path = f'{group}.{d}'
            for name in PREDICTORS[path]:
                pts = [(reads[k][name][0], (r.get(group) or {})[d], (priors[k].get(group) or {}).get(d))
                       for k, r in rows_.items()
                       if name in reads[k] and path not in gaps[k] and isinstance((r.get(group) or {}).get(d), (int, float))]
                fits[(path, name)] = skill(pts)
    for k, r in rows_.items():
        done = {}
        for path, prior in sorted(gaps[k].items()):
            bases = [b for b in series.nearest(k, sibs[k], index, board, tier)
                     if path not in gaps[b] and isinstance(get(rows_[b], path), (int, float))]
            value = None
            for b in bases:
                value, prov = convert(path, k, b, rows_, reads, fits, meas)
                if value is not None:
                    break
            if value is not None:
                put(r, path, value)
                done[path] = {'value': value, 'prior': prior, **prov}
                continue
            if path not in PREDICTORS:
                if prior is not None:
                    put(r, path, prior)
                continue
            skilled = [n for n in PREDICTORS[path] if n in reads[k] and (fits[(path, n)][1] or {}).get('skill')]
            name = min(skilled, key=lambda n: fits[(path, n)][1]['loo_rmse']) if skilled else None
            if not name:
                if prior is not None:
                    put(r, path, prior)               # no sibling and no skilled fit: the generic row stays
                continue
            (f, check), (x, cite) = fits[(path, name)], reads[k][name]
            value = round(five_axis.Scorer.predict(f, x), 2)
            put(r, path, value)
            done[path] = {'value': value, 'prior': prior, 'predictor': name, 'x': x, 'input': cite,
                          'fit': {key: round(v, 4) for key, v in f.items()}, 'skill': check,
                          'method': 'calibration/apply_rows.py estimate'}
            if not f['x_min'] <= x <= f['x_max']:
                done[path]['x_clamped'] = f['x_min'] if x < f['x_min'] else f['x_max']
        if USD in done:
            e = done[USD]
            r['usd_per_hour_basis'] = (f"Series conversion from {e['base_model']}: {e['base_value']} x API list price "
                                       f"ratio {e['ratio']} over the community token mix (score_estimates.usd_per_hour).")
        if done:
            r['score_estimates'] = done
        else:
            r.pop('score_estimates', None)
    return fits


def refresh_estimates(meas=None, dry_run=False):
    """Re-fill the shipped rows as they are and write the rows that changed (refresh.py, --estimates-only)."""
    acc = sources.accounts()
    meas = meas if meas is not None else json.loads(Path(measurements.OUT).read_text(encoding='utf-8'))
    models = copy.deepcopy(acc['models'])
    before = copy.deepcopy(models)
    fits = estimate(models, meas, acc)
    touched = {k: models[k] for k in models if models[k] != before[k]}
    if touched and not dry_run:
        write({}, touched, before)
    return models, fits, touched


def write(new, estimated=None, before=None):
    """`new`: whole measured rows (copied into every file). `estimated`: {key: row}; each file's own row of that key takes
    only the filled values that changed against `before` (or are recorded) and `score_estimates`, so the rest of a
    file's row stays its own."""
    paths = [f'{g}.{d}' for g, dims in SCORES.items() for d in dims] + [USD]
    for path in FILES:
        raw = Path(path).read_bytes().decode('utf-8')
        crlf = '\r\n' in raw
        doc = json.loads(raw)
        doc['models'].update(copy.deepcopy(new))
        for key, row in (estimated or {}).items():
            own = doc['models'].get(key)
            if not isinstance(own, dict):
                continue
            old = (before or {}).get(key) or {}
            recorded = row.get('score_estimates') or {}
            for p in paths:
                value = get(row, p)
                if value is not None and (p in recorded or value != get(old, p)):
                    put(own, p, value)
            if USD in recorded:
                own['usd_per_hour_basis'] = row['usd_per_hour_basis']
            if recorded:
                own['score_estimates'] = copy.deepcopy(recorded)
            else:
                own.pop('score_estimates', None)
        text = json.dumps(doc, indent=2, ensure_ascii=False) + '\n'
        Path(path).write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))


def report(fits, models):
    for (path, name), (fit, check) in sorted(fits.items()):
        if check:
            print(f"fit {path:32s} ~ {name:22s} n {fit['n']:>2} r {fit['r']:.2f} loo {check['loo_rmse']:.3f} "
                  f"mean {check['mean_loo_rmse']:.3f} prior {check['prior_rmse']} vs mean {check['beats_mean']} vs prior "
                  f"{check['beats_prior']} skill {check['skill']}")
        else:
            print(f'fit {path:32s} ~ {name:22s} no fit (points or slope)')
    for key, row in models.items():
        for path, e in ((row.get('score_estimates') or {}).items() if isinstance(row, dict) else ()):
            how = (f"from {e['base_model']} {e['base_value']}" + (f" x {e['ratio']}" if 'ratio' in e else '')
                   + (f" ~ {e['predictor']} {e['x_base']} -> {e['x']}" if e.get('predictor') else '')
                   + (f" (kept at {e['y_clamped']})" if 'y_clamped' in e else '')) if 'base_model' in e else (
                f"~ {e['predictor']} {e['x']}" + (f" (clamped to {e['x_clamped']})" if 'x_clamped' in e else ''))
            print(f"{key:30s} {path:32s} {e['prior']} -> {e['value']} {how}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('models', nargs='*')
    ap.add_argument('--data')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--estimates-only', action='store_true', help='re-estimate unmeasured scores on the rows as they are')
    args = ap.parse_args()
    if args.estimates_only:
        models, fits, _ = refresh_estimates(dry_run=args.dry_run)
        report(fits, models)
        return
    acc = sources.accounts()
    meas = json.loads(Path(measurements.OUT).read_text(encoding='utf-8'))
    models = copy.deepcopy(acc['models'])
    measured = {}
    ef = efforts.Efforts(sources.data_dir(args.data), acc)
    new = rows(ef.fitter, args.models or list(tiers.NAMES), acc, ef, measured)
    for key, row in new.items():
        print(f"{key:30s} tier {row['tier']:<5} {'estimated' if row.get('score_estimated') else 'measured'}")
    models.update(new)
    before = copy.deepcopy(models)
    fits = estimate(models, meas, acc, measured)
    report(fits, models)
    if args.dry_run:
        return
    write({k: models[k] for k in new}, {k: models[k] for k in models if models[k] != before[k]}, before)


if __name__ == '__main__':
    sys.exit(main())
