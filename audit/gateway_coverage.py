"""Coverage of saved gateway catalogs by the shipped reference data (a release check; owner 2026-11-18).

`python -X utf8 audit/gateway_coverage.py [--references DIR] [--catalogs DIR] [--known-gaps PATH] [--out-of-scope PATH] [--list]`

A gateway (Cindy, Magpie, ...) lists the (provider, model, efforts) it can launch. A saved catalog
(`audit/gateway-catalogs/<host>-<version>.json`, schema `agent-compass.gateway-catalog/3`) keeps only that: no account,
id or credential. Its `provider_kind` says what `provider` names:

- `harness`: an AgentCompass harness (Cindy's live catalog); a row that lists no efforts (`efforts: null`) is offered at the
  efforts of `dispatch_efforts[provider][row]`, and a row the table does not list for that harness is a gap;
- `magpie_preset`: a Magpie preset or `account:<kind>` (audit/magpie_catalog.py); never a harness, so the launch table does
  not apply: a row that lists no efforts is the model's one configuration, offered at `default`.

The model name resolves the way the router and the Magpie port resolve a candidate (model_resolution.resolve_in: the
serving providers' `provider_names` declarations first, then exact, alias and family pattern); only an `exact`, `alias` or
`provider` match names a row, and a name its serving provider declares `retired` names none.

A catalog may declare `gate` (`{makers, statement}`): its rows then carry `maker`, the model's maker, and only a row whose
maker is its provider's (`gate.makers[provider]`) is gated; the others are reported (`not first-party`, or `maker unknown`
when the row has none), never a failure. A row whose model is on the out-of-scope list (`audit/gateway-out-of-scope.json`, each entry
with its maker's notice: retired, deprecated with the vendor's shutdown date, absent from a maker page that enumerates the
callable model ids, a moving `-latest` pointer with no dated vendor statement of its target, or a special-purpose model the
maker does not offer for chat) is reported, not gated, in every catalog. The rule asserted, a data-completeness rule and no routing outcome:

- every gated (model, effort) pair of every saved catalog is **covered**: the name resolves to a catalog row whose every
  facet (creativity, coding execution and faithfulness, aesthetics design, tool_operation responsiveness and image
  recognition) and Time/Task has a value at that effort, maintainer scores applied (axis_scores.five_axis_overlay without
  local state); an effort off the scale is read where the data declares (axis_scores.reading_effort: `default` reads High);
  and the name is that row's weights on every provider that serves it: a pair is not covered by a row a serving
  provider's own listing identifies as other weights (owner, 2026-11-19), the serving providers being the ones
  model_resolution.serving_providers gives for the row's provider and the account's provider of a Magpie `account:<kind>`
  (and the provider part of a `provider/name` id), where the models.dev names file gives (provider, name) a canonical id
  and the row has one (a canonical id the file lists whose last segment is the row's id, or the `canonical_id` the
  declaration of the pair gives) and they differ, unless the name is an alias citing the maker's own statement that it is
  that row (as for the alias check below). The run-time rule closes such a pair by a `provider_names` declaration; one the
  data does not declare yet is a known gap of kind `other_weights_on_provider`, its entry carrying both canonical ids
  (`identity`);
- every `provider_names` declaration agrees with the names file (declaration_violations): the name is in the file, and
  where the file lists it on the declaring provider the canonical ids include the declared row's own (or its
  `canonical_id`); a `retired` declaration cites the maker. A declaration the file contradicts (the maker moved the name,
  models.dev followed) fails the check until the maintainer corrects and publishes it;
- the same for every (row, effort) the shipped launch table offers (catalog id `shipped-launch-table`: each
  `dispatch_efforts[harness][row]` entry, and each account bundle at its effort, an effort-less bundle at `default`);
- every alias of the shipped `accounts.json` names one model (round 5 review A): where models.dev (the snapshot Magpie
  bundles, saved by audit/magpie_catalog.py as `audit/models-dev-names.json`) lists both names, they share a canonical id
  (unless the alias cites the maker's own statement that they are one model) and, at a provider listing both, the list
  prices; every alias name and target is in that file (else it is rebuilt);
  and a shorthand (`alias_sources` `shorthand: true`) is not a name a saved catalog offers or a shipped row;
- or the pair is on the declared known-gap list (`audit/gateway-coverage-known-gaps.json`, each entry `{catalog, model,
  efforts, row, kind, detail}`: `kind` is one of the closed set `GAP_KINDS`, the input that is missing, which the file's
  `kinds` table states with what would close the gap; `detail` says it for this model: no declared method gives the
  value without it), and the kind matches the cells the pair misses (`gap_kinds`, round 7);
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

CATALOG_SCHEMA = 'agent-compass.gateway-catalog/3'
GAPS_SCHEMA = 'agent-compass.gateway-coverage-known-gaps/2'
OUT_OF_SCOPE_SCHEMA = 'agent-compass.gateway-out-of-scope/2'
CATALOGS = Path(__file__).resolve().parent / 'gateway-catalogs'
KNOWN_GAPS = Path(__file__).resolve().parent / 'gateway-coverage-known-gaps.json'
NAMES = Path(__file__).resolve().parent / 'models-dev-names.json'
NAMES_SCHEMA = 'agent-compass.models-dev-names/2'
OUT_OF_SCOPE = Path(__file__).resolve().parent / 'gateway-out-of-scope.json'
REFERENCES = ROOT / 'skills' / 'agent-compass' / 'references'
LAUNCH_TABLE = 'shipped-launch-table'
PROVIDER_KINDS = ('harness', 'magpie_preset')
# unlisted: absent from a maker page that enumerates the callable model ids (the file's `id_lists`), as read; moving_alias:
# a `-latest` pointer with no dated vendor statement of its target; special_purpose: a model the maker offers for music,
# speech, translation, OCR or role-play, not for chat
OUT_OF_SCOPE_STATUSES = ('retired', 'deprecated', 'unlisted', 'moving_alias', 'special_purpose')
ROW_KEYS = {'provider', 'model', 'efforts'}
GAP_KEYS = {'catalog', 'model', 'efforts', 'row', 'kind', 'detail'}
# The closed set of missing inputs a known gap may name (round 5 review G); the gaps file states for each what closes it.
GAP_KINDS = ('no_public_measurement', 'no_fleet_reading_at_effort', 'no_time_reading_in_series', 'no_relative_statement',
             'anchor_reading_moved', 'other_weights_on_provider')
# An entry of kind other_weights_on_provider also carries `identity`: {provider, listed (the provider's canonical id for
# the name), row (the row's canonical ids)}.
IDENTITY_KIND = 'other_weights_on_provider'
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
        gate = doc.get('gate')
        keys = ROW_KEYS | ({'maker'} if gate is not None else set())
        bad = [r for r in doc['rows'] if not isinstance(r, dict) or set(r) != keys]
        if bad:
            raise ValueError(f'{path.name}: every row is {{{", ".join(sorted(keys))}}} ({len(bad)} are not)')
        if gate is not None and (not isinstance(gate, dict) or not isinstance(gate.get('makers'), dict)
                                 or not any(gate['makers'].values()) or not str(gate.get('statement') or '').strip()):
            raise ValueError(f'{path.name}: gate must be {{makers, statement}} with at least one provider maker')
        out[path.stem] = doc
    return out


def _cited(entry, kinds):
    return (isinstance(entry, dict) and entry.get('kind') in kinds and str(entry.get('url') or '').startswith('https://')
            and bool(ISO_DATE.match(str(entry.get('as_of') or ''))) and bool(str(entry.get('statement') or '').strip()))


def load_out_of_scope(path=OUT_OF_SCOPE):
    """{offered model name: entry} of the declared out-of-scope list. Each entry carries its maker's notice (`notice`: kind
    vendor, https url, as_of, statement), a status (OUT_OF_SCOPE_STATUSES), the notice's `date` when the page gives one, and,
    when the offered name is another provider's spelling of the maker's model (`model`), the public statement that it is
    that model (`same_as`, e.g. models.dev's canonical id). A `deprecated` entry carries the vendor's `shutdown` date (null
    when the page states none); an `unlisted` entry's notice is a page of the file's `id_lists`, the maker pages that
    enumerate the callable model ids (absence from a page that does not is no evidence). A list without them is refused."""
    doc = read_json(path)
    if doc.get('schema') != OUT_OF_SCOPE_SCHEMA or not isinstance(doc.get('entries'), dict):
        raise ValueError(f'{path}: not a {OUT_OF_SCOPE_SCHEMA} document with entries')
    id_lists = doc.get('id_lists')
    if not isinstance(id_lists, dict) or not all(
            str(url).startswith('https://') and isinstance(v, dict) and ISO_DATE.match(str(v.get('as_of') or ''))
            and str(v.get('statement') or '').strip() for url, v in id_lists.items()):
        raise ValueError(f'{path}: id_lists must map each https page to {{as_of, statement}} of how it enumerates model ids')
    for name, e in doc['entries'].items():
        why = None
        if not isinstance(e, dict) or e.get('status') not in OUT_OF_SCOPE_STATUSES:
            why = f'status must be one of {", ".join(OUT_OF_SCOPE_STATUSES)}'
        elif not _cited(e.get('notice'), ('vendor',)):
            why = "needs the maker's notice {kind: vendor, url, as_of, statement}"
        elif 'date' in e and not ISO_DATE.match(str(e['date'] or '')):
            why = 'the date of the notice is an ISO date'
        elif e['status'] == 'deprecated' and ('shutdown' not in e or (
                e['shutdown'] is not None and not ISO_DATE.match(str(e['shutdown'])))):
            why = "a deprecated entry carries the vendor's shutdown date (null when the page states none)"
        elif e['status'] != 'deprecated' and 'shutdown' in e:
            why = 'only a deprecated entry carries a shutdown date'
        elif e['status'] == 'unlisted' and e['notice']['url'] not in id_lists:
            why = 'unlisted needs a notice page that enumerates the callable model ids (id_lists)'
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
    if gate is None:
        return None
    if not row.get('maker'):
        return 'maker unknown'
    if gate['makers'].get(row['provider']) != row['maker']:
        return f"not first-party ({row['maker']} on {row['provider']})"
    return None


def row_providers(doc, row):
    """The providers a gateway's own listing says serve the catalog row's name (owner, 2026-11-19, round 7): the row's
    provider as the catalog names it, and the account's provider for a Magpie `account:<kind>` (its `gate.makers` entry).
    model_resolution.serving_providers adds the provider part of a `provider/name` id."""
    account = ((doc.get('gate') or {}).get('makers') or {}).get(row['provider']) if row['provider'].startswith('account:') else None
    return tuple(dict.fromkeys(p for p in (row['provider'], account) if p))


def own_ids(listings, row):
    """The canonical ids the names file gives the row itself (a listed id whose last segment is the row's id)."""
    return {x[1] for x in listings.get(row) or () if x[1] and x[1].rsplit('/', 1)[-1] == row}


def identity_conflict(names, accounts, served, model, row):
    """{provider, listed, row} for the first (provider, name) of `served` (model_resolution.serving_providers) the
    models.dev names file identifies as other weights than `row` (both have a canonical id and they differ, the pair's
    `provider_names` declaration giving no `canonical_id` the file lists, and `model` is not an alias citing the maker's
    statement), else None: every provider the file knows for the pair is checked, not one of them."""
    listings = (names or {}).get('names') or {}
    own = own_ids(listings, row)
    source = (((accounts.get('alias_sources') or {}).get('rows') or {}).get(model) or {})
    if not own or ((accounts.get('aliases') or {}).get(model) == row and source.get('kind') == 'vendor'):
        return None
    rows = model_resolution.provider_name_table(accounts)['rows']
    for provider, name in served:
        listed = sorted({x[1] for x in listings.get(name) or () if x[0] == provider and x[1]})
        declared = ((rows.get(provider) or {}).get(name.lower()) or {})
        accepted = own | ({declared['canonical_id']} if declared.get('model') == row and declared.get('canonical_id') else set())
        if listed and not set(listed) & accepted:
            return {'provider': provider, 'listed': listed, 'row': sorted(accepted)}
    return None


def declaration_violations(accounts, listed):
    """(violation lines, counts): where a `provider_names` declaration of `accounts` disagrees with the models.dev names
    file (`listed`). A declared name the file does not hold (rebuild it); a provider's declaration whose row's canonical
    ids (or its `canonical_id`) are not among those the file lists for the name on that provider; a `retired` declaration
    that does not cite the maker; a listing of a default's name, on a provider that declares nothing for it, that is not
    the default's row (the provider's listing beats the maker's default, so it needs its own declaration). `counts`: the
    (provider, name) listings of the file under a default, under a provider's declaration, and under neither."""
    names = (listed or {}).get('names') or {}
    table = model_resolution.provider_name_table(accounts)
    out = []

    def accepted(d):
        return own_ids(names, d['model']) | ({d['canonical_id']} if d.get('canonical_id') else set())

    for name in sorted(set(table['defaults']) | {n for r in table['rows'].values() for n in r}):
        if name not in names:
            out.append(f'provider_names {name}: not in the models.dev names file (rebuild it with audit/magpie_catalog.py)')
    for provider, rows in sorted(table['rows'].items()):
        for name, d in sorted(rows.items()):
            where = f'provider_names rows[{provider}][{name}]'
            ids = {x[1] for x in names.get(name) or () if x[0] == provider and x[1]}
            if d.get('model') is None:
                if d.get('status') == 'retired' and d.get('kind') != 'vendor':
                    out.append(f"{where}: a retired declaration cites the maker's notice (kind vendor)")
            elif ids and not ids & accepted(d):
                out.append(f"{where}: declares row {d['model']} ({', '.join(sorted(accepted(d))) or 'no canonical id'}), "
                           f"the names file lists {', '.join(sorted(ids))} on {provider}: correct the declaration and "
                           'publish it (agentcompass data publish)')
    counts = {'default': 0, 'provider': 0, 'neither': 0}
    for name, listings in names.items():
        for provider, canonical, *_ in listings:
            if name.lower() in (table['rows'].get(provider) or {}):
                counts['provider'] += 1
            elif name.lower() in table['defaults']:
                counts['default'] += 1
                d = table['defaults'][name.lower()]
                if canonical and canonical not in accepted(d):
                    out.append(f"provider_names defaults[{name}] is {d['model']}, and {provider} lists the name as "
                               f'{canonical} with no declaration of its own: declare the row it serves there '
                               '(provider_names rows) and publish it')
            else:
                counts['neither'] += 1
    return out, counts


def pairs(accounts, catalogs, five_axis, public_scores, out_of_scope=None, names=None):
    """[{catalog, provider, model, effort, resolves_to, source, missing, ungated, identity}] for every offered pair of every
    catalog; `ungated` is None for a gated pair, else why it is only reported; `identity` the conflict of identity_conflict
    (`names`: the models.dev names file), which leaves the pair uncovered."""
    overlay = axis_scores.five_axis_overlay(accounts, data=five_axis, read_local_state=False, public_scores=public_scores)
    out = []
    for cid, doc in catalogs.items():
        kind = doc['provider_kind']
        for row in doc['rows']:
            # A Magpie preset is an endpoint (its ids are in its own namespace); a harness row is a launcher's catalog.
            providers = row_providers(doc, row) if kind == 'magpie_preset' else ()
            found = model_resolution.resolve_in(row['model'], accounts, providers)
            key = found['key'] if found['source'] in model_resolution.DIRECT_SOURCES else None
            efforts = row.get('efforts')
            launch = None
            if efforts is None and kind == 'harness':     # the host lists no efforts: the launch table decides
                launch = ((accounts.get('dispatch_efforts') or {}).get(row['provider']) or {}).get(key)
                efforts = launch or ['(none)']
            elif efforts is None:                          # a Magpie row without efforts: the model's one configuration
                efforts = ['default']
            ungated = None if cid == LAUNCH_TABLE else ungated_reason(doc, row, out_of_scope or {})
            identity = (identity_conflict(names, accounts, model_resolution.serving_providers(row['model'], providers),
                                          row['model'], key) if key else None)
            for effort in efforts:
                if key is None:
                    missing = [f"no row ({found['source']})"]
                elif kind == 'harness' and row.get('efforts') is None and launch is None:
                    missing = [f"not in dispatch_efforts[{row['provider']}]"]
                else:
                    missing = missing_at(overlay['models'].get(key), effort)
                if identity:
                    missing = [f"other weights on {identity['provider']}: {', '.join(identity['listed'])} "
                               f"(row {key}: {', '.join(identity['row'])})", *missing]
                out.append({'catalog': cid, 'provider': row['provider'], 'model': row['model'], 'effort': effort,
                            'resolves_to': key, 'source': found['source'], 'missing': missing, 'ungated': ungated,
                            'identity': identity})
    return out


SPEED_CELLS = {'time_per_task', 'tool_operation.responsiveness'}


def gap_kinds(missing):
    """The known-gap kinds the missing cells of one pair (`pairs` `missing`, the provider identity left out) can be
    (round 7): no row for the name -> `no_relative_statement`; every facet and the time -> `no_public_measurement` or
    `anchor_reading_moved`; only Time/Task and responsiveness, the readings of speed -> `no_time_reading_in_series`; some
    facets -> `no_fleet_reading_at_effort` or `anchor_reading_moved`, with `no_time_reading_in_series` when the time is
    missing too. A cell the audit does not know matches no kind."""
    cells = set(missing)
    if any(c.startswith(('no row (', 'not in dispatch_efforts[')) for c in cells):
        return {'no_relative_statement'}
    known = {f'{c}.{f}' for c, f in FACETS} | {'time_per_task'}
    if not cells or not cells <= known:
        return set()
    if cells == known:
        return {'no_public_measurement', 'anchor_reading_moved'}
    if cells <= SPEED_CELLS:
        return {'no_time_reading_in_series'}
    return {'no_fleet_reading_at_effort', 'anchor_reading_moved'} | (
        {'no_time_reading_in_series'} if 'time_per_task' in cells else set())


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
        conflicts = [p['identity'] for p in gated if p.get('identity')]
        if k in listed and conflicts and (listed[k]['kind'] != IDENTITY_KIND or listed[k].get('identity') not in conflicts):
            violations.append(f"other weights on a provider, not listed as {IDENTITY_KIND} with that identity: "
                              f"{k[0]} {k[1]} @ {k[2]} ({conflicts[0]})")
        elif k in listed and listed[k]['kind'] == IDENTITY_KIND and not conflicts:
            violations.append(f"listed as {IDENTITY_KIND} but no provider's listing identifies other weights: "
                              f"{k[0]} {k[1]} @ {k[2]}")
        if k in listed and listed[k]['kind'] != IDENTITY_KIND:
            for p in uncovered:
                cells = [c for c in p['missing'] if not c.startswith('other weights on ')]
                if listed[k]['kind'] not in gap_kinds(cells):
                    violations.append(f"listed as {listed[k]['kind']} but what is missing is {', '.join(cells)}: "
                                      f"{k[0]} {k[1]} @ {k[2]} (kinds that match: "
                                      f"{', '.join(sorted(gap_kinds(cells))) or 'none'})")
                    break
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
            why = p['ungated'].split(' (')[0]
            r['ungated'][why] = r['ungated'].get(why, 0) + 1
        k = (p['resolves_to'] or p['model'].rsplit('/', 1)[-1].lower(), p['effort'])
        r['_distinct'][k] = r['_distinct'].get(k, True) and not p['missing']
    for r in out.values():
        d = r.pop('_distinct')
        r['distinct'], r['distinct_covered'] = len(d), sum(d.values())
    return out


def alias_violations(accounts, listed, catalogs):
    """Why an alias of `accounts` does not name one model, as violation lines: `listed` is the models.dev names document
    (models-dev-names.json); `catalogs` {id: document} the saved catalogs."""
    if listed.get('schema') != NAMES_SCHEMA or not isinstance(listed.get('names'), dict):
        return [f'the models.dev names file is not a {NAMES_SCHEMA} document']
    names = listed['names']
    aliases = accounts.get('aliases') or {}
    sources = (accounts.get('alias_sources') or {}).get('rows') or {}
    out = []
    for name, target in sorted(aliases.items()):
        lacking = [n for n in (name, target) if n not in names]
        if lacking:
            out.append(f'alias {name} -> {target}: {", ".join(lacking)} not in the models.dev names file '
                       f'(rebuild it with audit/magpie_catalog.py)')
            continue
        a, b = names[name], names[target]
        ca, cb = {x[1] for x in a if x[1]}, {x[1] for x in b if x[1]}
        # the maker's own statement that the two names are one model outweighs the aggregator's separate entries
        maker_says = (sources.get(name) or {}).get('kind') == 'vendor'
        if ca and cb and not ca & cb and not maker_says:
            out.append(f'alias {name} -> {target}: models.dev gives different canonical ids ({", ".join(sorted(ca))} '
                       f'against {", ".join(sorted(cb))})')
        pa, pb = {x[0]: tuple(x[2:]) for x in a}, {x[0]: tuple(x[2:]) for x in b}
        differ = [f'{p}: {pa[p]} against {pb[p]}' for p in sorted(set(pa) & set(pb)) if pa[p] != pb[p]]
        if differ:
            out.append(f'alias {name} -> {target}: models.dev lists different prices ({"; ".join(differ[:3])})')
    shorthands = [k for k, e in sources.items() if isinstance(e, dict) and e.get('shorthand') is True]
    offered = {}
    for cid, doc in catalogs.items():
        for r in doc.get('rows') or ():
            offered.setdefault(r['model'], cid)
            offered.setdefault(r['model'].rsplit('/', 1)[-1], cid)
    for s in sorted(shorthands):
        if s in (accounts.get('models') or {}):
            out.append(f'shorthand {s} is a shipped row')
        if s in offered:
            out.append(f'shorthand {s} is a name the saved catalog {offered[s]} offers')
    return out


def load_gaps(path=KNOWN_GAPS):
    """The declared known-gap list: a `kinds` table stating, for exactly the kinds of `GAP_KINDS`, the missing input and
    what would close the gap (`{missing, closes_when}`), and entries {catalog, model, efforts, row, kind, detail} whose kind
    is one of them and whose detail says it for the model. A list without them is refused."""
    gaps = read_json(path)
    if gaps.get('schema') != GAPS_SCHEMA or not isinstance(gaps.get('entries'), list):
        raise ValueError(f'{path}: not a {GAPS_SCHEMA} document with entries')
    kinds = gaps.get('kinds')
    if (not isinstance(kinds, dict) or set(kinds) != set(GAP_KINDS)
            or not all(isinstance(v, dict) and set(v) == {'missing', 'closes_when'} and all(str(x).strip() for x in v.values())
                       for v in kinds.values())):
        raise ValueError(f'{path}: kinds must state {{missing, closes_when}} for exactly {", ".join(GAP_KINDS)}')
    for e in gaps['entries']:
        keys = GAP_KEYS | ({'identity'} if isinstance(e, dict) and e.get('kind') == IDENTITY_KIND else set())
        if (not isinstance(e, dict) or set(e) != keys or not e['efforts'] or e['kind'] not in GAP_KINDS
                or not str(e['detail'] or '').strip()):
            raise ValueError(f'{path}: every entry is {{{", ".join(sorted(GAP_KEYS))}}} with efforts, a kind of '
                             f'{", ".join(GAP_KINDS)} and its detail: {e!r}'[:400])
    return gaps


def run(references=REFERENCES, catalogs=CATALOGS, known_gaps=KNOWN_GAPS, out_of_scope_path=OUT_OF_SCOPE, names=NAMES):
    references = Path(references)
    gaps = load_gaps(known_gaps)
    out_of_scope = load_out_of_scope(out_of_scope_path)
    accounts = read_json(references / 'accounts.json')
    saved = load_catalogs(catalogs)
    listed_names = read_json(names)
    found = pairs(accounts, {**saved, LAUNCH_TABLE: {'provider_kind': 'harness', 'rows': launch_rows(accounts)}},
                  read_json(references / 'five-axis-scores.json'),
                  user_scores.load_public(references / 'user-scores.json'), out_of_scope, listed_names)
    violations, counts = check(found, gaps, out_of_scope)
    violations += alias_violations(accounts, listed_names, saved)
    declared, counts['names_file'] = declaration_violations(accounts, listed_names)
    violations += declared
    return found, violations, counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--references', default=str(REFERENCES))
    ap.add_argument('--catalogs', default=str(CATALOGS))
    ap.add_argument('--known-gaps', default=str(KNOWN_GAPS))
    ap.add_argument('--out-of-scope', default=str(OUT_OF_SCOPE))
    ap.add_argument('--names', default=str(NAMES), help='the models.dev names file audit/magpie_catalog.py writes')
    ap.add_argument('--list', action='store_true', help='print every uncovered pair, gated or not')
    args = ap.parse_args(argv)
    found, violations, counts = run(args.references, args.catalogs, args.known_gaps, args.out_of_scope, args.names)
    if args.list:
        for p in found:
            if p['missing']:
                print(f"{p['catalog']} {p['provider']} {p['model']} @ {p['effort']}: {', '.join(p['missing'])}"
                      + (f" [reported: {p['ungated']}]" if p.get('ungated') else ''))
    for cid, r in sorted(report(found).items()):
        print(f"{cid}: {r['pairs']} offered pairs, {r['covered']} covered ({r['distinct']} distinct, "
              f"{r['distinct_covered']} covered); gated {r['gated_covered']} of {r['gated']} covered"
              + (f"; reported only: {', '.join(f'{k} {v}' for k, v in sorted(r['ungated'].items()))}" if r['ungated'] else ''))
    nf = counts['names_file']
    print(f"models.dev names file: {nf['default']} (provider, name) listings under a maker's default, {nf['provider']} "
          f"under a provider's declaration, {nf['neither']} under neither (provider_names)")
    print(f"gateway coverage: {counts['covered']} of {counts['pairs']} gated pairs covered, "
          f"{counts['known_gap']} on the known-gap list, {counts['reported']} reported only, {len(violations)} violation(s)")
    for v in violations:
        print(f'  {v}')
    return 1 if violations else 0


if __name__ == '__main__':
    sys.exit(main())
