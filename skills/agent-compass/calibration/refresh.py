"""Keep the five-axis data current (round 047, round 4): fetch every public source again, rebuild
`references/public-measurements.json` and `references/five-axis-scores.json`, and report each source's age.

`python -X utf8 refresh.py [--data DIR] [--no-fetch] [--report PATH]`

- Fetch: `sources.fetch` downloads the benchmark boards and the community pages into DIR and writes the fetch
  manifest (url, fetched_at, sha256 per file). A source that fails is recorded missing and its old copy deleted, so a
  value is never carried over silently from an earlier download.
- Rebuild: `measurements.py` then `five_axis.py` on that directory; every value keeps its data date (`as_of`) and the
  fetch time of its file (`fetched_at`). A facet estimated from a sibling or the tier fit is rebuilt each time: a direct
  measurement that arrived replaces it.
- Report: the community usd-per-hour check rows against the usd_per_hour in use, then per source its
  kind, data date, age in days and status: `ok`; `stale` (older than five_axis.STALE_DAYS, a
  yellow note: still used); `missing` (not fetched, or a community sentence no longer on the page: red, no value).
  Capacity windows with an empty capacity ladder are listed as red.

Exit status: 0 all fresh, 1 something stale (yellow), 2 something missing (red), so a scheduler can alert on it.

Scheduling (nothing is installed by this script): run it weekly, after wokey.ai's Monday remeasurement, from the
maintainer's checkout or a CI job, review the diff and the report, and commit the two references files. Installs pick
the committed files up with the next release.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import five_axis
import measurements
import sources

LEVEL = {'ok': 0, 'unused': 0, 'stale': 1, 'missing': 2}   # unused: fetched, left out by design (METR)


def rebuild(target, now=None, meas_out=measurements.OUT, scores_out=five_axis.OUT):
    acc = sources.accounts()
    meas = measurements.Builder(target, acc).build()
    raw = (json.dumps(meas, indent=1, ensure_ascii=False) + '\n').encode('utf-8')
    Path(meas_out).write_bytes(raw)
    plans = json.loads(five_axis.PLANS.read_text(encoding='utf-8'))
    doc = five_axis.build(meas, acc, plans, meas_bytes=raw, now=now)
    Path(scores_out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    return meas, doc


def staleness(doc):
    """(rows, worst status): one row per source and per capacity window left without a value."""
    rows = []
    stale_capability = {e['source']: e for e in doc.get('source_errors') or () if e.get('reason') == 'stale_source'}
    for src, row in sorted((doc.get('source_ages') or {}).items()):
        if row.get('missing'):
            status = 'unused' if str(row.get('reason') or '').startswith('not used') else 'missing'
        else:
            status = 'stale' if row.get('stale') else 'ok'
        rows.append({'what': src, 'kind': row.get('kind'), 'as_of': row.get('as_of'), 'age_days': row.get('age_days'),
                     'status': status, 'note': row.get('reason') or (
                         f"stale capability source ({stale_capability[src]['component']}): "
                         f"{stale_capability[src]['effect']}" if src in stale_capability else row.get('as_of_kind') or '')})
    for e in doc.get('capacity_errors') or ():
        if e.get('level') != 'red':
            continue
        what = e.get('source') or '/'.join(str(e.get(k)) for k in ('family', 'plan', 'window') if e.get(k))
        if e.get('source') and e.get('reason') == 'reading_not_found':
            what = f"{e['source']} -> {e.get('family')}/{e.get('plan')}/{e.get('window')}"
        rows.append({'what': what, 'kind': 'community' if e.get('source') else 'capacity', 'as_of': None, 'age_days': None,
                     'status': 'missing', 'note': e.get('reason')})
    worst = max((LEVEL[r['status']] for r in rows), default=0)
    return rows, {0: 'ok', 1: 'stale', 2: 'missing'}[worst]


def markdown(rows, generated_at, checks=()):
    out = []
    if checks:
        out += ['Community usd per worker-hour / the usd_per_hour in use (1.0 = agreement)', '',
                '| Model | Community $/h | usd_per_hour | Community / usd_per_hour | Source | Data date |',
                '| :--- | ---: | ---: | ---: | :--- | :--- |']
        out += [f"| {c['model']} | {c['value']} | {c['usd_per_hour']} | {c['community_over_usd']} | {c['source']} "
                f"| {c['as_of']} |" for c in checks]
        out.append('')
    out += [f'Source ages at {generated_at} (stale after {five_axis.STALE_DAYS} days)', '',
           '| Source / window | Kind | Data date | Age (days) | Status | Note |', '| :--- | :--- | :--- | ---: | :--- | :--- |']
    for r in rows:
        out.append(f"| {r['what']} | {r['kind'] or ''} | {r['as_of'] or ''} | {'' if r['age_days'] is None else r['age_days']} "
                   f"| {r['status']} | {r['note']} |")
    return '\n'.join(out) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data')
    ap.add_argument('--no-fetch', action='store_true', help='rebuild from the downloads already in DIR')
    ap.add_argument('--report', help='also write the source-age table (markdown) here')
    args = ap.parse_args()
    target = sources.data_dir(args.data)
    if not args.no_fetch:
        sources.fetch(target)
    _, doc = rebuild(target)
    rows, worst = staleness(doc)
    text = markdown(rows, datetime.now(timezone.utc).isoformat(timespec='seconds'), doc.get('usd_checks') or ())
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8', newline='\n')
    return LEVEL[worst]


if __name__ == '__main__':
    sys.exit(main())
