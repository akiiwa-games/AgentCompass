"""Coverage of saved gateway catalogs by the shipped reference data (a release check; owner 2026-11-18).

`python -X utf8 audit/gateway_coverage.py [--references DIR] [--catalogs DIR] [--known-gaps PATH] [--out-of-scope PATH] [--list]`

A gateway (Cindy, Magpie, ...) lists the (provider, model, efforts) it can launch. A saved catalog
(`audit/gateway-catalogs/<host>-<version>.json`, schema `agent-compass.gateway-catalog/2`) keeps only that: no account,
id or credential. Its `provider_kind` says what `provider` names:

- `harness`: an AgentCompass harness (Cindy's live catalog); a row that lists no efforts (`efforts: null`) is offered at the
  efforts of `dispatch_efforts[provider][row]`, and a row the table does not list for that harness is a gap;
- `magpie_preset`: a Magpie preset or `account:<kind>` (audit/magpie_catalog.py); never a harness, so the launch table does
  not apply: a row that lists no efforts is the model's one configuration, offered at `default`.

The model name resolves the way the router and the Magpie port resolve a candidate (model_resolution.resolve_model: exact,
alias, then family pattern); only an `exact` or `alias` match names a row.

A catalog may declare `gate` (`{providers, statement}`): only its rows whose provider is listed are gated, the others are
reported, never a failure. A row whose model is on the out-of-scope list (`audit/gateway-out-of-scope.json`, each entry
with its maker's notice: retired or deprecated with the date, absent from the maker's own current list on the date read, or a
special-purpose model the maker does not offer for chat) is reported, not gated, in every catalog. The rule asserted, a data-completeness rule and no routing outcome:

- every gated (model, effort) pair of every saved catalog is **covered**: the name resolves to a catalog row whose every
  facet (creativity, coding execution and faithfulness, aesthetics design, tool_operation responsiveness and image
  recognition) and Time/Task has a value at that effort, maintainer scores applied (axis_scores.five_axis_overlay without
  local state); an effort off the scale is read where the data declares (axis_scores.reading_effort: `default` reads High);
- the same for every (row, effort) the shipped launch table offers (catalog id `shipped-launch-table`: each
  `dispatch_efforts[harness][row]` entry, and each account bundle at its effort, an effort-less bundle at `default`);
- or the pair is on the declared known-gap list (`audit/gateway-coverage-known-gaps.json`, each entry `{catalog, model,
  efforts, row, reason}` whose reason names the input that is missing, `Missing input: ...`: no declared method gives the
  value without it);
- and no entry of that list is covered, ungated or names a pair no saved catalog offers (the list only shrinks), and no
  out-of-scope entry names a model no saved catalog offers.

Exit status 0 when the rule holds, 1 with the violations listed. The ungated pairs are printed as a report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'agent-compass' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import axis_scores  # noqa: E402
import model_resolution  # noqa: E402
import user_scores  # noqa: E402

CATALOG_SCHEMA = 'agent-compass.gateway-catalog/2'
GAPS_SCHEMA = 'agent-compass.gateway-coverage-known-gaps/1'
OUT_OF_SCOPE_SCHEMA = 'agent-compass.gateway-out-of-scope/1'
CATALOGS = Path(__file__).resolve().parent / 'gateway-catalogs'
KNOWN_GAPS = Path(__file__).resolve().parent / 'gateway-coverage-known-gaps.json'
OUT_OF_SCOPE = Path(__file__).resolve().parent / 'gateway-out-of-scope.json'
REFERENCES = ROOT / 'skills' / 'agent-compass' / 'references'
LAUNCH_TABLE = 'shipped-launch-table'
PROVIDER_KINDS = ('harness', 'magpie_preset')
# unlisted: absent from the maker's own current list, as read; special_purpose: a model the maker offers for music, speech,
# translation, OCR or role-play, not for chat
OUT_OF_SCOPE_STATUSES = ('retired', 'deprecated', 'unlisted', 'special_purpose')
ROW_KEYS = {'provider', 'model', 'efforts'}
GAP_KEYS = {'catalog', 'model', 'efforts', 'row', 'reason'}
MISSING_INPUT = 'Missing input: '
ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
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
        if doc.get('provider_kind') not in PROVIDER_KINDS:
            raise ValueError(f'{path.name}: provider_kind must be one of {", ".join(PROVIDER_KINDS)}')
        bad = [r for r in doc['rows'] if not isinstance(r, dict) or set(r) != ROW_KEYS]
        if bad:
            raise ValueError(f'{path.name}: every row is {{provider, model, efforts}} ({len(bad)} are not)')
        gate = doc.get('gate')
        if gate is not None and (not isinstance(gate, dict) or not isinstance(gate.get('providers'), list)
                                 or not gate['providers'] or not str(gate.get('statement') or '').strip()):
            raise ValueError(f'{path.name}: gate must be {{providers, statement}} with at least one provider')
        out[path.stem] = doc
    return out


def _cited(entry, kinds):
    return (isinstance(entry, dict) and entry.get('kind') in kinds and str(entry.get('url') or '').startswith('https://')
            and bool(ISO_DATE.match(str(entry.get('as_of') or ''))) and bool(str(entry.get('statement') or '').strip()))


def load_out_of_scope(path=OUT_OF_SCOPE):
    """{offered model name: entry} of the declared out-of-scope list. Each entry carries its maker's notice (`notice`: kind
    vendor, https url, as_of, statement), a status (OUT_OF_SCOPE_STATUSES; `retired` and `deprecated` with the notice's `date`),
    and, when the offered name is another provider's spelling of the maker's model (`model`), the public statement that it
    is that model (`same_as`, e.g. models.dev's canonical id). A list without them is refused."""
    doc = read_json(path)
    if doc.get('schema') != OUT_OF_SCOPE_SCHEMA or not isinstance(doc.get('entries'), dict):
        raise ValueError(f'{path}: not a {OUT_OF_SCOPE_SCHEMA} document with entries')
    for name, e in doc['entries'].items():
        why = None
        if not isinstance(e, dict) or e.get('status') not in OUT_OF_SCOPE_STATUSES:
            why = f'status must be one of {", ".join(OUT_OF_SCOPE_STATUSES)}'
        elif not _cited(e.get('notice'), ('vendor',)):
            why = "needs the maker's notice {kind: vendor, url, as_of, statement}"
        elif e['status'] in ('retired', 'deprecated') and not ISO_DATE.match(str(e.get('date') or '')):
            why = 'a retired or deprecated entry needs the date of its notice'
        elif e.get('model', name) != name and not _cited(e.get('same_as'), ('vendor', 'community')):
            why = 'another spelling of the maker\'s model needs the public statement that it is that model (same_as)'
        if why:
            raise ValueError(f'{path}: out-of-scope entry {name!r}: {why}')
    return doc['entries']


def launch_rows(accounts):
    """The rows of the shipped launch table as catalog rows: every `dispatch_efforts[harness][row]` entry, and every account
    bundle (provider: the account) at its effort, an effort-less bundle at `default`."""
    rows = [{'provider': h, 'model': m, 'efforts': list(e)}
            for h, table in sorted((accounts.get('dispatch_efforts') or {}).items()) if isinstance(table, dict)
            for m, e in sorted(table.items()) if isinstance(e, list)]
    for name, spec in sorted((accounts.get('accounts') or {}).items()):
        bundles = (spec.get('bundles') or {}) if isinstance(spec, dict) else {}
        rows += [{'provider': name, 'model': b['model'], 'efforts': [b.get('effort') or 'default']}
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


def ungated_reason(doc, row, out_of_scope):
    """Why a catalog row is reported rather than gated, or None when it is gated."""
    entry = out_of_scope.get(row['model'])
    if entry is not None:
        return f"{entry['status']} ({entry.get('model', row['model'])})"
    gate = doc.get('gate')
    if gate is not None and row['provider'] not in gate['providers']:
        return 'outside the gate'
    return None


def pairs(accounts, catalogs, five_axis, public_scores, out_of_scope=None):
    """[{catalog, provider, model, effort, resolves_to, source, missing, ungated}] for every offered pair of every catalog;
    `ungated` is None for a gated pair, else why it is only reported."""
    overlay = axis_scores.five_axis_overlay(accounts, data=five_axis, read_local_state=False, public_scores=public_scores)
    rows = {k: v for k, v in (accounts.get('models') or {}).items() if isinstance(v, dict)}
    families = model_resolution.family_patterns(accounts)
    out = []
    for cid, doc in catalogs.items():
        kind = doc['provider_kind']
        for row in doc['rows']:
            found = model_resolution.resolve_model(row['model'], rows, accounts.get('aliases') or {}, families)
            key = found['key'] if found['source'] in model_resolution.DIRECT_SOURCES else None
            efforts = row.get('efforts')
            launch = None
            if efforts is None and kind == 'harness':     # the host lists no efforts: the launch table decides
                launch = ((accounts.get('dispatch_efforts') or {}).get(row['provider']) or {}).get(key)
                efforts = launch or ['(none)']
            elif efforts is None:                          # a Magpie row without efforts: the model's one configuration
                efforts = ['default']
            ungated = None if cid == LAUNCH_TABLE else ungated_reason(doc, row, out_of_scope or {})
            for effort in efforts:
                if key is None:
                    missing = [f"no row ({found['source']})"]
                elif kind == 'harness' and row.get('efforts') is None and launch is None:
                    missing = [f"not in dispatch_efforts[{row['provider']}]"]
                else:
                    missing = missing_at(overlay['models'].get(key), effort)
                out.append({'catalog': cid, 'provider': row['provider'], 'model': row['model'], 'effort': effort,
                            'resolves_to': key, 'source': found['source'], 'missing': missing, 'ungated': ungated})
    return out


def check(found, gaps, out_of_scope=None):
    """(violations, counts): a gated uncovered pair not on the known-gap list, a listed pair that is covered, ungated or
    offered by no catalog, and an out-of-scope entry no catalog offers."""
    listed = {}
    for entry in gaps.get('entries') or ():
        for effort in entry['efforts']:
            listed[(entry['catalog'], entry['model'], effort)] = entry
    offered = {}
    for p in found:
        offered.setdefault((p['catalog'], p['model'], p['effort']), []).append(p)
    violations = []
    for k, ps in sorted(offered.items()):
        gated = [p for p in ps if p.get('ungated') is None]
        uncovered = [p for p in gated if p['missing']]
        if uncovered and k not in listed:
            violations.append(f"uncovered, not on the known-gap list: {k[0]} {k[1]} @ {k[2]} "
                              f"({uncovered[0]['provider']}: {', '.join(uncovered[0]['missing'])})")
        if k in listed and not uncovered:
            violations.append(f"on the known-gap list but {'covered' if gated else 'not gated'}: {k[0]} {k[1]} @ {k[2]}")
    for k in sorted(set(listed) - set(offered)):
        violations.append(f'on the known-gap list but offered by no saved catalog: {k[0]} {k[1]} @ {k[2]}')
    names = {p['model'] for p in found}
    for name in sorted(set(out_of_scope or {}) - names):
        violations.append(f'on the out-of-scope list but offered by no saved catalog: {name}')
    gated = [p for p in found if p.get('ungated') is None]
    counts = {'pairs': len(gated), 'covered': sum(1 for p in gated if not p['missing']),
              'known_gap': sum(1 for p in gated if p['missing'] and (p['catalog'], p['model'], p['effort']) in listed),
              'reported': len(found) - len(gated)}
    return violations, counts


def report(found):
    """{catalog: {pairs, covered, gated, gated_covered, distinct, distinct_covered, ungated: {reason kind: pairs}}}: the
    whole of each catalog, gated or not; `distinct` counts (row or offered name, effort) once across providers."""
    out = {}
    for p in found:
        r = out.setdefault(p['catalog'], {'pairs': 0, 'covered': 0, 'gated': 0, 'gated_covered': 0, 'ungated': {},
                                          '_distinct': {}})
        r['pairs'] += 1
        r['covered'] += not p['missing']
        if p.get('ungated') is None:
            r['gated'] += 1
            r['gated_covered'] += not p['missing']
        else:
            why = p['ungated'].split(' ')[0]
            r['ungated'][why] = r['ungated'].get(why, 0) + 1
        k = (p['resolves_to'] or p['model'].rsplit('/', 1)[-1].lower(), p['effort'])
        r['_distinct'][k] = r['_distinct'].get(k, True) and not p['missing']
    for r in out.values():
        d = r.pop('_distinct')
        r['distinct'], r['distinct_covered'] = len(d), sum(d.values())
    return out


def load_gaps(path=KNOWN_GAPS):
    """The declared known-gap list; every entry is {catalog, model, efforts, row, reason} and its reason names the input that
    is missing (`Missing input: ...`). A list without them is refused."""
    gaps = read_json(path)
    if gaps.get('schema') != GAPS_SCHEMA or not isinstance(gaps.get('entries'), list):
        raise ValueError(f'{path}: not a {GAPS_SCHEMA} document with entries')
    for e in gaps['entries']:
        if (not isinstance(e, dict) or set(e) != GAP_KEYS or not e['efforts']
                or not str(e['reason'] or '').startswith(MISSING_INPUT) or not e['reason'][len(MISSING_INPUT):].strip()):
            raise ValueError(f'{path}: every entry is {{{", ".join(sorted(GAP_KEYS))}}} with efforts and a reason that '
                             f'begins {MISSING_INPUT!r} and names the input: {e!r}'[:400])
    return gaps


def run(references=REFERENCES, catalogs=CATALOGS, known_gaps=KNOWN_GAPS, out_of_scope_path=OUT_OF_SCOPE):
    references = Path(references)
    gaps = load_gaps(known_gaps)
    out_of_scope = load_out_of_scope(out_of_scope_path)
    accounts = read_json(references / 'accounts.json')
    found = pairs(accounts, {**load_catalogs(catalogs), LAUNCH_TABLE: {'provider_kind': 'harness',
                                                                       'rows': launch_rows(accounts)}},
                  read_json(references / 'five-axis-scores.json'),
                  user_scores.load_public(references / 'user-scores.json'), out_of_scope)
    violations, counts = check(found, gaps, out_of_scope)
    return found, violations, counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--references', default=str(REFERENCES))
    ap.add_argument('--catalogs', default=str(CATALOGS))
    ap.add_argument('--known-gaps', default=str(KNOWN_GAPS))
    ap.add_argument('--out-of-scope', default=str(OUT_OF_SCOPE))
    ap.add_argument('--list', action='store_true', help='print every uncovered pair, gated or not')
    args = ap.parse_args(argv)
    found, violations, counts = run(args.references, args.catalogs, args.known_gaps, args.out_of_scope)
    if args.list:
        for p in found:
            if p['missing']:
                print(f"{p['catalog']} {p['provider']} {p['model']} @ {p['effort']}: {', '.join(p['missing'])}"
                      + (f" [reported: {p['ungated']}]" if p.get('ungated') else ''))
    for cid, r in sorted(report(found).items()):
        print(f"{cid}: {r['pairs']} offered pairs, {r['covered']} covered ({r['distinct']} distinct, "
              f"{r['distinct_covered']} covered); gated {r['gated_covered']} of {r['gated']} covered"
              + (f"; reported only: {', '.join(f'{k} {v}' for k, v in sorted(r['ungated'].items()))}" if r['ungated'] else ''))
    print(f"gateway coverage: {counts['covered']} of {counts['pairs']} gated pairs covered, "
          f"{counts['known_gap']} on the known-gap list, {counts['reported']} reported only, {len(violations)} violation(s)")
    for v in violations:
        print(f'  {v}')
    return 1 if violations else 0


if __name__ == '__main__':
    sys.exit(main())
