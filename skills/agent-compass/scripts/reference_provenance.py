"""Provenance checks for the shipped reference data under references/.

Reference data ships with the installed version and is fixed until the next release; no code path reads a newer copy
at run time (../references/router-internals.md, Reference data). These checks hold the shipped files to the data
provenance rule: every declaration in accounts.json and plan-templates.json is a dated public citation or a dated
maintainer declaration, and references/user-scores.json holds maintainer scores only. `agentcompass data refresh`
runs them before it writes and after it rebuilds; axis_scores.py applies the declaration checks to the entries it reads.
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
    the catalog has."""
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
    for name, cited in checks.items():
        missing = [k for k, (_, known) in cited.items() if not known]
        if missing:
            raise ValueError(f'accounts.json {name} cites rows the catalog does not have: {", ".join(missing)}')
        bad = [k for k, (e, _) in cited.items() if not valid_public_declaration(e)]
        if bad:
            raise ValueError(f'accounts.json {name} rows need an https url or a dated maintainer declaration: {", ".join(bad)}')


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
    by_pattern = {e['pattern']: e for e in families['rows']}
    for i, entry in enumerate(families['rows']):
        if 'tier_up' in entry and (entry['size'] != 'small' or (by_pattern.get(entry['tier_up']) or {}).get('size') != 'full'):
            raise ValueError(f'families.rows[{i}] ({entry["pattern"]}) tier_up must be on a small row and name the pattern '
                             f'of a full row: {entry["tier_up"]!r}')
    panel = doc.get('calibration_panel')
    if not valid_public_declaration(panel) or not isinstance(panel.get('anchor'), str) or not isinstance(panel.get('models'), list):
        raise ValueError('accounts.json needs calibration_panel {anchor, models} as a dated maintainer declaration')
    members = panel.get('members')
    if not isinstance(members, list) or not members:
        raise ValueError('calibration_panel needs `members`, the declared panel (calibration/measurements.py)')
    unknown = [m for m in [panel['anchor'], *panel['models'], *members] if m not in models]
    if unknown or panel['anchor'] not in panel['models'] or not set(panel['models']) <= set(members):
        raise ValueError(f'calibration_panel anchor, models and members must be model rows, the anchor among the models '
                         f'and the models among the members' + (f' (no row: {", ".join(map(str, unknown))})' if unknown else ''))


REFERENCES = Path(__file__).resolve().parents[1] / 'references'


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
