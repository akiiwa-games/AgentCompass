"""Write measured model rows into references/accounts.json and the reference fixture.

`python -X utf8 apply_rows.py [--data DIR] [--dry-run] [model-id ...]` (default: every model in tiers.NAMES)

A row that exists keeps its prices, modalities and priors; its tier (and coding, when the model is on WebDev) is replaced
by the measurement and `score_estimated` is dropped. A new row starts from the router's own default profile for that id
(auto_bundles.default_profile: family prior or the unmeasured default) and takes the measurement on top. An unmeasured
model keeps an estimated row whose tier is the data's unmeasured default (accounts.json `unmeasured_models`).
Regenerate the capability views afterwards: `python -X utf8 ../scripts/capability_catalog.py`.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np

import efforts
import sources
import tiers

sys.path.insert(0, str(sources.REPO / 'scripts'))
import auto_bundles  # noqa: E402

FILES = [sources.ACCOUNTS, sources.REPO / 'scripts' / 'fixtures' / 'reference-accounts.json']
NEW_ROW_NOTE = ('New row; cost, usd_per_hour, modalities, other capabilities and subjective scores start from the '
                'router default profile ({source}) and are routing priors, not measurements.')


def coding_map(fitter):
    xs = np.array([fitter.boards['webdev'][n][0] for n in tiers.ANCHORS.values()])
    ys = np.array([fitter.models[k]['capabilities']['coding'] for k in tiers.ANCHORS])
    a, b = np.polyfit(xs, ys, 1)
    return lambda x: round(float(np.clip(a * x + b, 0.0, 1.0)), 2), float(np.corrcoef(xs, ys)[0, 1])


def rows(fitter, keys, acc, ef):
    coding, r = coding_map(fitter)
    unmeasured = acc['unmeasured_models']['generic']
    out = {}
    for key in keys:
        e = fitter.fit(key)
        existing = acc['models'].get(key)
        if isinstance(existing, dict):
            row, note = copy.deepcopy(existing), ''
        else:
            prof = auto_bundles.default_profile(key, acc)
            row = {k: v for k, v in prof['spec'].items() if k != 'basis'}
            note = ' ' + NEW_ROW_NOTE.format(source=prof['source'] + (f" {prof['target']}" if prof.get('target') else ''))
        if e['method'] == 'unmeasured':
            row['tier'] = unmeasured['tier']
            row['score_estimated'] = True
            row['basis'] = (f"{tiers.UNMEASURED.get(key, 'No comparable public measurement')}. tier = the unmeasured default "
                            '(accounts.json unmeasured_models.generic), below the hard floor.' + note)
        else:
            high, corr = ef.high_tier(key, e['tier']) if e['method'] == 'primary' else (e['tier'], {})
            row['tier'] = high
            row.pop('score_estimated', None)
            webdev = dict(e['sources']).get('webdev')
            if webdev:
                row.setdefault('capabilities', {})['coding'] = coding(webdev[0])
            at_high = (f" At High: {high} = {e['tier']} minus the Artificial Analysis effort gain of the listed variant over High "
                       f'beyond that of the anchors at the same tier ({corr}; calibration/efforts.py), so the effort curve is not '
                       'counted twice.') if corr and high != e['tier'] else ''
            row['basis'] = tiers.basis(e) + at_high + (f' coding = anchor regression on WebDev (r={r:.2f}).' if webdev else '') + note
        out[key] = row
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('models', nargs='*')
    ap.add_argument('--data')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    keys = args.models or list(tiers.NAMES)
    ef = efforts.Efforts(sources.data_dir(args.data), sources.accounts())
    new = rows(ef.fitter, keys, sources.accounts(), ef)
    for key, row in new.items():
        print(f"{key:30s} tier {row['tier']:<5} {'estimated' if row.get('score_estimated') else 'measured'}")
    if args.dry_run:
        return
    for path in FILES:
        raw = Path(path).read_bytes().decode('utf-8')
        crlf = '\r\n' in raw
        doc = json.loads(raw)
        doc['models'].update(copy.deepcopy(new))
        text = json.dumps(doc, indent=2, ensure_ascii=False) + '\n'
        Path(path).write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))


if __name__ == '__main__':
    sys.exit(main())
