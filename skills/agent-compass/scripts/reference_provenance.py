"""Provenance checks for the reference data under references/ and for every set published from it.

No code path reads the boards at run time; between releases only what the maintainer publishes moves
(../references/router-internals.md, Reference data). These checks hold the reference files to the data provenance
rule: every declaration in accounts.json and plan-templates.json is a dated public citation or a dated maintainer
declaration, and references/user-scores.json holds maintainer scores only. `agentcompass data refresh` runs them
before it writes and after it rebuilds, `data publish` and every install run them before accepting a set
(public_data.accept); axis_scores.py applies the declaration checks to the entries it reads.
"""
import json
from pathlib import Path
import re

import model_resolution
import user_scores

_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def valid_public_declaration(entry):
    if not isinstance(entry, dict) or entry.get('kind') == 'user':
        return False
    url = str(entry.get('url') or '').strip()
    if url.startswith('https://'):
        return True
    as_of = str(entry.get('as_of') or '').strip()
    return entry.get('kind') == 'maintainer' and bool(_ISO_DATE.match(as_of))


def valid_capacity_rule(entry):
    if not valid_public_declaration(entry):
        return False
    for key in ('target_family', 'target_window', 'reference_family', 'reference_window'):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            return False
    forbidden = {'plan', 'base_plan', 'plan_price_usd_per_month', 'base_plan_price_usd_per_month', 'capacity_usd'}
    if forbidden & set(entry) or any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in entry.values()):
        return False
    return True


def valid_vendor_price_source(entry):
    if not isinstance(entry, dict) or entry.get('kind') != 'vendor':
        return False
    url = str(entry.get('url') or '').strip()
    as_of = str(entry.get('as_of') or '').strip()
    return url.startswith('https://') and bool(_ISO_DATE.match(as_of))


def validate_plan_templates_provenance(doc):
    """Ensure every `price_source` declared in plan-templates.json is a dated vendor https citation."""
    if not isinstance(doc, dict):
        raise ValueError('plan-templates.json must be a JSON object')
    for family, plans in doc.items():
        if not isinstance(plans, dict):
            continue
        for plan, tpl in plans.items():
            if not isinstance(tpl, dict):
                continue
            if 'price_source' in tpl and not valid_vendor_price_source(tpl.get('price_source')):
                raise ValueError(f'plan-templates[{family!r}][{plan!r}].price_source requires kind="vendor", https url and ISO as_of')


def validate_accounts_provenance(doc):
    """Ensure every capability/equivalence declaration in accounts.json has a public https URL or dated maintainer kind."""
    if not isinstance(doc, dict):
        raise ValueError('accounts.json must be a JSON object')
    for row, entry in (doc.get('same_model') or {}).items():
        if not valid_public_declaration(entry):
            raise ValueError(f'same_model[{row!r}] requires an https url or dated kind="maintainer" (with as_of)')
        if 'capacity' in entry:
            raise ValueError(f'same_model[{row!r}].capacity may not hard-code a plan or price; use rule-only capacity_rule')
        if 'capacity_rule' in entry and not valid_capacity_rule(entry.get('capacity_rule')):
            raise ValueError(
                f'same_model[{row!r}].capacity_rule requires dated kind="maintainer" (or https url) and family/window rule fields without plan or price literals'
            )
    for model, spec in (doc.get('models') or {}).items():
        if not isinstance(spec, dict):
            continue
        rt = spec.get('response_time_source')
        if rt is not None and not (isinstance(rt, dict) and valid_public_declaration(rt)
                                   and str(rt.get('statement') or '').strip()
                                   and isinstance(rt.get('min_seconds'), (int, float)) and not isinstance(rt.get('min_seconds'), bool)
                                   and rt['min_seconds'] > 0):
            raise ValueError(f'models[{model!r}].response_time_source requires a statement, `min_seconds` above 0 and an '
                             'https url or dated kind="maintainer"')
        if spec.get('text_only') and not valid_public_declaration(spec.get('text_only_source')):
            raise ValueError(f'models[{model!r}].text_only requires a cited or dated maintainer text_only_source (with as_of)')
        if 'image_input' in spec and not valid_public_declaration(spec.get('image_input_source')):
            raise ValueError(f'models[{model!r}].image_input requires a cited or dated maintainer image_input_source (with as_of)')
        for field in ('text_only_source', 'image_input_source', 'model_source'):
            if field in spec and not valid_public_declaration(spec.get(field)):
                raise ValueError(f'models[{model!r}].{field} requires an https url or dated kind="maintainer" (with as_of)')
        _validate_model_data(model, spec)
    _validate_catalog_tables(doc)
    _validate_cited_tables(doc)


def _validate_cited_tables(doc):
    """`alias_sources` and `dispatch_efforts_sources`: each cited row is a public declaration of an alias or a launch row
    the catalog has, and every alias is cited with its date and statement (round 4)."""
    def rows(name):
        table = doc.get(name, {'rows': {}})
        if not isinstance(table, dict) or not isinstance(table.get('rows'), dict):
            raise ValueError(f'accounts.json {name} needs {{statement, rows}}')
        return table['rows']
    launch = doc.get('dispatch_efforts') or {}
    checks = {
        'alias_sources': {k: (e, k in (doc.get('aliases') or {})) for k, e in rows('alias_sources').items()},
        'dispatch_efforts_sources': {f'{h}/{m}': (e, m in (launch.get(h) or {}))
                                     for h, per in rows('dispatch_efforts_sources').items() for m, e in per.items()},
    }
    # A dated maintainer declaration is no statement of identity: it stands only for a shorthand, the maintainer's short
    # name for a row (`shorthand: true`, "short name for <row>"); an identity alias cites a public page (round 5 review A).
    aliases = doc.get('aliases') or {}
    wrong = sorted(k for k, e in rows('alias_sources').items() if isinstance(e, dict) and k in aliases and (
        (e.get('kind') == 'maintainer') != (e.get('shorthand') is True)
        or (e.get('shorthand') is True and f'short name for {aliases[k]}' not in str(e.get('statement') or ''))
        or (e.get('shorthand') is not True and not str(e.get('url') or '').startswith('https://'))))
    if wrong:
        raise ValueError('accounts.json alias_sources: a maintainer row is a shorthand (shorthand: true, "short name for '
                         f'<row>") and every other alias cites a public https page: {", ".join(wrong)}')
    uncited = sorted(set(doc.get('aliases') or {}) - set(rows('alias_sources')))
    if uncited:
        raise ValueError('accounts.json aliases need their public statement in alias_sources (a vendor statement, a cited '
                         f'catalog such as models.dev, or a dated maintainer declaration): {", ".join(uncited)}')
    for name, cited in checks.items():
        missing = [k for k, (_, known) in cited.items() if not known]
        if missing:
            raise ValueError(f'accounts.json {name} cites rows the catalog does not have: {", ".join(missing)}')
        bad = [k for k, (e, _) in cited.items() if not valid_public_declaration(e)]
        if bad:
            raise ValueError(f'accounts.json {name} rows need an https url or a dated maintainer declaration: {", ".join(bad)}')
    undated = [k for k, (e, _) in checks['alias_sources'].items()
               if not _ISO_DATE.match(str(e.get('as_of') or '')) or not str(e.get('statement') or '').strip()]
    if undated:
        raise ValueError(f'accounts.json alias_sources rows need the date they were read (as_of) and the statement: '
                         f'{", ".join(undated)}')


LISTING_BOARDS = ('arena', 'aa')        # calibration/tiers.py LISTING_BOARDS: `listed_as` keys
LIST_PRICE_FIELDS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _price_table(value, fields):
    return isinstance(value, dict) and set(value) == set(fields) and all(_number(value[f]) for f in fields)


def _validate_model_data(model, spec):
    """Shape of the per-model reference fields (`listed_as`, `list_price`, `vendor_fast_mode`, `fast_capable`)."""
    if 'listed_as' in spec:
        names = spec['listed_as']
        if (not isinstance(names, dict) or not names or not set(names) <= set(LISTING_BOARDS)
                or not all(isinstance(v, str) and v.strip() for v in names.values())):
            raise ValueError(f'models[{model!r}].listed_as maps {"/".join(LISTING_BOARDS)} to the name each board lists the model under')
    if 'list_price' in spec:
        if not _price_table(spec['list_price'], LIST_PRICE_FIELDS):
            raise ValueError(f'models[{model!r}].list_price needs numeric {", ".join(LIST_PRICE_FIELDS)} (USD per million tokens)')
        if not valid_vendor_price_source(spec.get('list_price_source')):
            raise ValueError(f'models[{model!r}].list_price requires list_price_source with kind="vendor", https url and ISO as_of')
    if 'vendor_fast_mode' in spec:
        fast = spec['vendor_fast_mode']
        ok = (isinstance(fast, dict) and _number(fast.get('price_multiplier')) and _number(fast.get('speed_multiplier'))
              and all(_price_table(fast.get(k), ('input', 'output', 'cache_read')) for k in ('fast_price', 'standard_price'))
              and all(str(fast.get(k) or '').startswith('https://') for k in ('url', 'pricing_url'))
              and bool(_ISO_DATE.match(str(fast.get('as_of') or ''))) and isinstance(fast.get('statement'), str))
        if not ok:
            raise ValueError(f'models[{model!r}].vendor_fast_mode needs price_multiplier, speed_multiplier, fast_price, '
                             'standard_price, https url and pricing_url, ISO as_of and the vendor statement')
    if 'fast_capable' in spec and not isinstance(spec['fast_capable'], bool):
        raise ValueError(f'models[{model!r}].fast_capable must be true or false')


def _validate_catalog_tables(doc):
    """The catalog-wide model tables: `families` (model_resolution) and `calibration_panel` (calibration/measurements.py)."""
    models = {k for k, v in (doc.get('models') or {}).items() if isinstance(v, dict)}
    families = doc.get('families')
    if not valid_public_declaration(families) or not isinstance(families.get('rows'), list):
        raise ValueError('accounts.json needs families {rows: [{pattern, models}]} as a dated maintainer declaration')
    for i, entry in enumerate(families['rows']):
        if not isinstance(entry, dict) or not isinstance(entry.get('pattern'), str) or not isinstance(entry.get('models'), list):
            raise ValueError(f'families.rows[{i}] needs a pattern and a models list')
        try:
            re.compile(entry['pattern'])
        except re.error as exc:
            raise ValueError(f'families.rows[{i}].pattern {entry["pattern"]!r} is not a regular expression: {exc}') from None
        unknown = [m for m in entry['models'] if m not in models]
        if unknown:
            raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) names models without a row: {", ".join(map(str, unknown))}')
        if entry.get('size') not in model_resolution.FAMILY_SIZES:
            raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) needs a size, one of '
                             f'{", ".join(model_resolution.FAMILY_SIZES)} (calibration/series.py)')
        if not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ('maker', 'line')):
            raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) needs a maker and a product line '
                             '(calibration/series.py: the relatives of a row)')
    by_pattern = {e['pattern']: e for e in families['rows']}
    for i, entry in enumerate(families['rows']):
        if 'tier_up' in entry and (entry['size'] != 'small' or (by_pattern.get(entry['tier_up']) or {}).get('size') != 'full'):
            raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) tier_up must be on a small row and name the pattern '
                             f'of a full row: {entry["tier_up"]!r}')
        if ('tier_up' in entry) != ('tier_up_version' in entry) or entry.get('tier_up_version', 'same') not in ('same', 'nearest'):
            raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) tier_up needs tier_up_version `same` or `nearest` '
                             'and tier_up_version needs tier_up')
        older = entry.get('tier_up_older')
        if older is not None:
            larger = (by_pattern.get(entry.get('tier_up')) or {}).get('pattern')
            ok = isinstance(older, dict) and bool(older) and larger is not None and all(
                k in models and k not in entry['models'] and re.search(entry['pattern'], k)
                and isinstance(rel, dict) and set(rel) == {'model', 'version'} and rel['model'] in models
                and re.search(larger, rel['model']) and rel['version'] in ('same', 'nearest') for k, rel in older.items())
            if not ok:
                raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) tier_up_older needs tier_up and maps older model '
                                 'rows of this pattern (not current members) to {model: a row of the tier_up pattern, '
                                 'version: same | nearest}')
    panel = doc.get('calibration_panel')
    if not valid_public_declaration(panel) or not isinstance(panel.get('anchor'), str) or not isinstance(panel.get('models'), list):
        raise ValueError('accounts.json needs calibration_panel {anchor, models} as a dated maintainer declaration')
    members = panel.get('members')
    if not isinstance(members, list) or not members:
        raise ValueError('calibration_panel needs `members`, the declared panel (calibration/measurements.py)')
    readings = doc.get('effort_readings')
    reads = (readings or {}).get('reads') if isinstance(readings, dict) else None
    if (not valid_public_declaration(readings) or not isinstance(reads, dict) or not reads
            or not all(isinstance(k, str) and k not in user_scores.EFFORTS and v in user_scores.MEASURED_EFFORTS
                       for k, v in reads.items())):
        raise ValueError('accounts.json needs effort_readings {reads: {effort name off the scale: the measured effort it '
                         'reads}} as a dated maintainer declaration (calibration/five_axis.py effort_readings)')
    unknown = [m for m in [panel['anchor'], *panel['models'], *members] if m not in models]
    if unknown or panel['anchor'] not in panel['models'] or not set(panel['models']) <= set(members):
        raise ValueError(f'calibration_panel anchor, models and members must be model rows, the anchor among the models '
                         f'and the models among the members' + (f' (no row: {", ".join(map(str, unknown))})' if unknown else ''))


REFERENCES = Path(__file__).resolve().parents[1] / 'references'


DRIFT_KEYS = {'drift', 'n', 'members', 'spread', 'common_shift', 'snapshot_as_of', 'fetched_as_of'}


def _drift_problem(where, d, scaled):
    """Why a `drift_correction` record is malformed, or None (calibration/refresh.py board_drift)."""
    date = lambda v: isinstance(v, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', v)
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if not isinstance(d, dict) or set(d) != DRIFT_KEYS:
        return f'{where}: drift_correction has exactly {", ".join(sorted(DRIFT_KEYS))}'
    if not num(d['drift']) or not isinstance(d['n'], int) or d['n'] < 2 or not isinstance(d['members'], list) \
            or len(d['members']) != d['n']:
        return f'{where}: drift_correction needs a numeric drift and n >= 2 panel members named in members'
    spread = d['spread']
    if not (isinstance(spread, dict) and set(spread) == {'min', 'max', 'mad'} and all(num(v) for v in spread.values())
            and spread['min'] <= d['drift'] <= spread['max'] and spread['mad'] >= 0):
        return f'{where}: drift_correction.spread is {{min, max, mad}} around the drift'
    if d['common_shift'] is not (abs(d['drift']) > spread['mad']):
        return f'{where}: drift_correction.common_shift is whether |drift| is above the median absolute deviation'
    if not (date(d['snapshot_as_of']) and date(d['fetched_as_of'])):
        return f'{where}: drift_correction names both dates (snapshot_as_of, fetched_as_of)'
    if not scaled:
        return f'{where}: drift_correction on a source without a scale'
    return None


def validate_measurements_provenance(doc):
    """A measurement corrected by its board's drift states the correction in that shape, on a scaled source."""
    bad = []
    for model, row in sorted((doc.get('models') or {}).items()):
        for field, items in sorted(row.items()):
            for e in items:
                if 'drift_correction' in e:
                    scaled = bool(((doc.get('sources') or {}).get(e.get('source')) or {}).get('scale'))
                    bad.append(_drift_problem(f"models.{model}.{field} {e.get('source')}", e['drift_correction'], scaled))
    bad = [b for b in bad if b]
    if bad:
        raise ValueError('; '.join(bad))


def validate_five_axis_provenance(doc, measurements):
    """A cell source carrying drift_correction carries the one of its measurement entry, unchanged."""
    bad = []
    for model, row in sorted((doc.get('models') or {}).items()):
        entries = {e.get('source'): e for items in ((measurements.get('models') or {}).get(model) or {}).values()
                   for e in items}
        for axis, ax in sorted((row.get('axes') or {}).items()):
            for facet, cell in sorted((ax.get('facets') or {}).items()):
                for src, rec in sorted((cell.get('sources') or {}).items()):
                    if 'drift_correction' in rec and rec['drift_correction'] != (entries.get(src) or {}).get('drift_correction'):
                        bad.append(f'models.{model}.{axis}.{facet} {src}: drift_correction differs from its measurement')
    if bad:
        raise ValueError('; '.join(bad))


def validate_references(directory=REFERENCES):
    """Raise ValueError naming the file and the field when a shipped reference file breaks the provenance rule."""
    directory = Path(directory)

    def read(name):
        return json.loads((directory / name).read_text(encoding='utf-8-sig'))

    for name, check in (('accounts.json', validate_accounts_provenance),
                        ('plan-templates.json', validate_plan_templates_provenance),
                        ('user-scores.json', lambda doc: user_scores.validate_document(doc, expected_kind='maintainer'))):
        try:
            check(read(name))
        except ValueError as exc:
            raise ValueError(f'{directory / name} is invalid: {exc}') from None
    if (directory / 'public-measurements.json').is_file():
        meas = read('public-measurements.json')
        try:
            validate_measurements_provenance(meas)
            if (directory / 'five-axis-scores.json').is_file():
                validate_five_axis_provenance(read('five-axis-scores.json'), meas)
        except ValueError as exc:
            raise ValueError(f'{directory} measurements or five-axis scores are invalid: {exc}') from None
