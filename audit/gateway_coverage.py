"""Coverage of saved gateway catalogs by the shipped reference data (a release check; owner 2026-11-18).

`python -X utf8 audit/gateway_coverage.py [--references DIR] [--catalogs DIR] [--known-gaps PATH] [--list]`

A gateway (Cindy, Magpie, ...) lists the (harness, model, efforts) it can launch. A saved catalog
(`audit/gateway-catalogs/<host>-<version>.json`, schema `agent-compass.gateway-catalog/1`) keeps only that: no account,
id or credential. The rule asserted, a data-completeness rule and no routing outcome:

- every offered (model, effort) pair of every saved catalog is **covered**: the name resolves to a catalog row (`exact`
  or `alias`, model_resolution.resolve_model) whose every facet (creativity, coding execution and faithfulness, aesthetics
  design, tool_operation responsiveness and image recognition) and Time/Task has a value at that effort, maintainer scores
  applied (axis_scores.five_axis_overlay without local state); for a catalog row that lists no efforts (`efforts: null`),
  the efforts are those of `dispatch_efforts[harness][row]`, and a row the table does not list for that harness is a gap;
- the same for every (row, effort) the shipped launch table offers (catalog id `shipped-launch-table`: each
  `dispatch_efforts[harness][row]` entry, and each account bundle at its effort, an effort-less bundle at `default`); an
  effort off the scale is read where the data declares (axis_scores.reading_effort: `default` reads High);
- or the pair is on the declared known-gap list (`audit/gateway-coverage-known-gaps.json`, each entry with its reason);
- and no entry of that list is covered or names a pair no saved catalog offers (the list only shrinks).

Exit status 0 when the rule holds, 1 with the violations listed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'agent-compass' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import axis_scores  # noqa: E402
import model_resolution  # noqa: E402
import user_scores  # noqa: E402

CATALOG_SCHEMA = 'agent-compass.gateway-catalog/1'
GAPS_SCHEMA = 'agent-compass.gateway-coverage-known-gaps/1'
CATALOGS = Path(__file__).resolve().parent / 'gateway-catalogs'
KNOWN_GAPS = Path(__file__).resolve().parent / 'gateway-coverage-known-gaps.json'
REFERENCES = ROOT / 'skills' / 'agent-compass' / 'references'
LAUNCH_TABLE = 'shipped-launch-table'
FACETS = (('creativity', 'creative_writing'), ('coding', 'execution'), ('coding', 'faithfulness'),
          ('aesthetics', 'design'), ('tool_operation', 'responsiveness'), ('tool_operation', 'image_recognition'))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def load_catalogs(directory=CATALOGS):
    """{catalog id: document} of every saved catalog in `directory`; the id is the file's stem."""
    out = {}
    for path in sorted(Path(directory).glob('*.json')):
        doc = read_json(path)
        if doc.get('schema') != CATALOG_SCHEMA or not isinstance(doc.get('rows'), list):
            raise ValueError(f'{path.name}: not a {CATALOG_SCHEMA} document with rows')
        out[path.stem] = doc
    return out


def launch_rows(accounts):
    """The rows of the shipped launch table as catalog rows: every `dispatch_efforts[harness][row]` entry, and every account
    bundle (harness: the account) at its effort, an effort-less bundle at `default`."""
    rows = [{'harness': h, 'model': m, 'efforts': list(e)}
            for h, table in sorted((accounts.get('dispatch_efforts') or {}).items()) if isinstance(table, dict)
            for m, e in sorted(table.items()) if isinstance(e, list)]
    for name, spec in sorted((accounts.get('accounts') or {}).items()):
        bundles = (spec.get('bundles') or {}) if isinstance(spec, dict) else {}
        rows += [{'harness': name, 'model': b['model'], 'efforts': [b.get('effort') or 'default']}
                 for _, b in sorted(bundles.items()) if isinstance(b, dict) and b.get('model')]
    return rows


def missing_at(info, effort):
    """The facets and Time/Task the overlaid model row `info` has no value for at `effort` (empty: covered); an effort off
    the scale is read where the data declares (axis_scores.reading_effort)."""
    five = (info or {}).get('five_axis') or {}
    effort = axis_scores.reading_effort(five, effort)
    cats = five.get('categories') or {}
    miss = [f'{c}.{f}' for c, f in FACETS
            if ((((cats.get(c) or {}).get('facets') or {}).get(f) or {}).get('by_effort') or {}).get(effort) is None]
    if ((five.get('duration') or {}).get('by_effort') or {}).get(effort) is None:
        miss.append('time_per_task')
    return miss


def pairs(accounts, catalogs, five_axis, public_scores):
    """[{catalog, harness, model, effort, resolves_to, source, missing}] for every offered pair of every catalog."""
    overlay = axis_scores.five_axis_overlay(accounts, data=five_axis, read_local_state=False, public_scores=public_scores)
    rows = {k: v for k, v in (accounts.get('models') or {}).items() if isinstance(v, dict)}
    families = model_resolution.family_patterns(accounts)
    out = []
    for cid, doc in catalogs.items():
        for row in doc['rows']:
            found = model_resolution.resolve_model(row['model'], rows, accounts.get('aliases') or {}, families)
            key = found['key'] if found['source'] in model_resolution.DIRECT_SOURCES else None
            efforts = row.get('efforts')
            launch = None
            if efforts is None:     # the host lists no efforts: the launch table decides
                launch = ((accounts.get('dispatch_efforts') or {}).get(row['harness']) or {}).get(key)
                efforts = launch or ['(none)']
            for effort in efforts:
                if key is None:
                    missing = [f"no row ({found['source']})"]
                elif row.get('efforts') is None and launch is None:
                    missing = [f"not in dispatch_efforts[{row['harness']}]"]
                else:
                    missing = missing_at(overlay['models'].get(key), effort)
                out.append({'catalog': cid, 'harness': row['harness'], 'model': row['model'], 'effort': effort,
                            'resolves_to': key, 'source': found['source'], 'missing': missing})
    return out


def check(found, gaps):
    """(violations, counts): an uncovered pair not on the known-gap list, a listed pair that is covered, a listed pair no
    catalog offers."""
    listed = {}
    for entry in gaps.get('entries') or ():
        for effort in entry['efforts']:
            listed[(entry['catalog'], entry['model'], effort)] = entry
    offered = {}
    for p in found:
        offered.setdefault((p['catalog'], p['model'], p['effort']), []).append(p)
    violations = []
    for k, ps in sorted(offered.items()):
        uncovered = [p for p in ps if p['missing']]
        if uncovered and k not in listed:
            violations.append(f"uncovered, not on the known-gap list: {k[0]} {k[1]} @ {k[2]} "
                              f"({uncovered[0]['harness']}: {', '.join(uncovered[0]['missing'])})")
        if not uncovered and k in listed:
            violations.append(f'on the known-gap list but covered: {k[0]} {k[1]} @ {k[2]}')
    for k in sorted(set(listed) - set(offered)):
        violations.append(f'on the known-gap list but offered by no saved catalog: {k[0]} {k[1]} @ {k[2]}')
    counts = {'pairs': len(found), 'covered': sum(1 for p in found if not p['missing']),
              'known_gap': sum(1 for p in found if p['missing'] and (p['catalog'], p['model'], p['effort']) in listed)}
    return violations, counts


def run(references=REFERENCES, catalogs=CATALOGS, known_gaps=KNOWN_GAPS):
    references = Path(references)
    gaps = read_json(known_gaps)
    if gaps.get('schema') != GAPS_SCHEMA:
        raise ValueError(f'{known_gaps}: not a {GAPS_SCHEMA} document')
    accounts = read_json(references / 'accounts.json')
    found = pairs(accounts, {**load_catalogs(catalogs), LAUNCH_TABLE: {'rows': launch_rows(accounts)}},
                  read_json(references / 'five-axis-scores.json'),
                  user_scores.load_public(references / 'user-scores.json'))
    violations, counts = check(found, gaps)
    return found, violations, counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--references', default=str(REFERENCES))
    ap.add_argument('--catalogs', default=str(CATALOGS))
    ap.add_argument('--known-gaps', default=str(KNOWN_GAPS))
    ap.add_argument('--list', action='store_true', help='print every uncovered pair')
    args = ap.parse_args(argv)
    found, violations, counts = run(args.references, args.catalogs, args.known_gaps)
    if args.list:
        for p in found:
            if p['missing']:
                print(f"{p['catalog']} {p['harness']} {p['model']} @ {p['effort']}: {', '.join(p['missing'])}")
    print(f"gateway coverage: {counts['covered']} of {counts['pairs']} offered pairs covered, "
          f"{counts['known_gap']} on the known-gap list, {len(violations)} violation(s)")
    for v in violations:
        print(f'  {v}')
    return 1 if violations else 0


if __name__ == '__main__':
    sys.exit(main())
