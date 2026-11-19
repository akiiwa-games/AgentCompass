"""Same-series siblings and the base a converted model takes (round 058, user 2026-11-10: "后续同系列新出，缺少实测数据的情况下，
先按照同系列的全分数进行折算，不要留下任何一个默认值或者fallback"; five_axis.py estimates through the tier on top of it).

Relatives are declared (owner 2026-11-19, round 6 item 4), never read off a name or a member list: every `families` row
declares its `maker`, its product `line` and its `size` (`small` or `full`), and a row takes those of the `families` row
that decides its name (model_resolution.family_row; `identity`); a `same_model` row takes its declared model's (it is
that model). A row's relatives for an **ability** facet (`groups(acc, 'ability')`) are the rows of the same maker and the
same size, every product line, a row of another line only up to the declared version distance
(`estimation_order.sibling_first_up_to_distance`); for a **speed or price** facet (`groups(acc, 'line')`: responsiveness, Time/Task, cost per
hour) only those of its own line (a Pro model does not have Astra's speed). A row whose deciding row declares no maker has
no relatives.

The downward relation (`tier_up`, owner 2026-11-18) is a second relation, not series membership: a small row listed in
its `families` row's `models` (a current member of that family) whose `families` row declares `tier_up` (the pattern of
the same line's next larger family row) has, as its larger model, the first model of that larger row the catalog has;
`tier_up_version` declares whether that model is the same version as the row's current members (`same`) or the nearest
version of the line (`nearest`). An older member of the line that is no current member gets its larger model only from
the row's `tier_up_older` declaration ({model: {model: larger, version}}). five_axis.py uses it only for a facet the small
row's own same-size series and the tier fit leave without any value: the small row's own gap only for `same`, the fleet's
size step for both.

A `same_model` row (accounts.json) declares that one row is the very model another row names (a vendor statement with its
url): the declared model is that row's base before any other sibling, and the row carries the type scores of the
categories the row lists under `carries`; a category it does not list is the channel's own and stays missing (a
property of the channel, not of the model behind it).

The base for a score is the relative that carries a direct value for it, nearest by the declared version distance
(accounts.json `series_versions`: the rank difference among the distinct declared versions of the row and its relatives,
0 the same version; a row without a declared version ranks after every declared distance), then the same line before
another line at equal distance; relatives still tied form one group whose estimates the caller takes the median of. A
name never decides (round 5 review D: an alphabetical tie-break moved estimates by whole levels when a row was added).
"""
import sys

import sources

sys.path.insert(0, str(sources.REPO / 'scripts'))
import model_resolution  # noqa: E402


def small_rows(acc):
    """The rows of `acc` whose deciding `families` row declares `size: small`."""
    fams = model_resolution.family_patterns(acc)
    return {k for k, v in (acc.get('models') or {}).items()
            if isinstance(v, dict) and (model_resolution.family_row(k, fams) or {}).get('size') == 'small'}


FAMILY_KEYS = ('maker', 'line', 'size')


def identity(acc):
    """{row: {'maker', 'line', 'size'}} (module docstring): the declaration of the `families` row that decides the row's
    name, or, for a `same_model` row, its declared model's; a row no `families` row decides is left out. A `families` row
    without a nonempty `maker` and `line` and a `size` of `small` or `full` stops the build."""
    models = {k: v for k, v in (acc.get('models') or {}).items() if isinstance(v, dict)}
    fams = model_resolution.family_patterns(acc)
    for f in fams:
        if not (isinstance(f.get('maker'), str) and f['maker'].strip() and isinstance(f.get('line'), str)
                and f['line'].strip() and f.get('size') in model_resolution.FAMILY_SIZES):
            raise ValueError(f"accounts.json families row {f.get('pattern')!r} must declare maker, line and size "
                             '(small or full)')
    out = {}
    for k in models:
        row = model_resolution.family_row(k, fams)
        if row is not None:
            out[k] = {key: row[key] for key in FAMILY_KEYS}
    for row, entry in same_model(acc).items():
        if row in models and entry.get('model') in out:
            out[row] = dict(out[entry['model']])
    return out


def groups(acc, scope='ability'):
    """{row key: sorted relative keys} over the score rows of `acc` (module docstring): `ability` the same maker and size,
    `line` also the same line."""
    if scope not in ('ability', 'line'):
        raise ValueError(f'scope {scope!r}: ability or line')
    ident = identity(acc)
    rows = [k for k, v in (acc.get('models') or {}).items() if isinstance(v, dict)]

    def key(k):
        i = ident[k]
        return (i['maker'], i['size']) + ((i['line'],) if scope == 'line' else ())
    return {k: sorted(j for j in ident if j != k and key(j) == key(k)) if k in ident else [] for k in rows}


def lines(acc):
    """{row: its declared line} (`identity`)."""
    return {k: v['line'] for k, v in identity(acc).items()}


def tier_up(acc):
    """{small row: {'model': its larger model, 'version': 'same' | 'nearest'}} (module docstring, the downward relation)."""
    models = {k: v for k, v in (acc.get('models') or {}).items() if isinstance(v, dict)}
    fams = model_resolution.family_patterns(acc)
    by_pattern = {f['pattern']: f for f in fams}
    out = {}
    for k in sorted(models):
        row = model_resolution.family_row(k, fams) or {}
        if row.get('size') != 'small' or not row.get('tier_up') or k not in row['models']:
            continue
        larger = next((t for t in by_pattern[row['tier_up']]['models'] if t in models), None)
        if larger:
            out[k] = {'model': larger, 'version': row['tier_up_version']}
    for row in fams:
        for k, rel in (row.get('tier_up_older') or {}).items():
            if k in models and rel['model'] in models:
                out[k] = {'model': rel['model'], 'version': rel['version']}
    return dict(sorted(out.items()))


def same_model(acc):
    """{row key: {'model', 'carries', 'statement', 'url', 'as_of', ...}}: the `same_model` rows of accounts.json."""
    return {k: v for k, v in (acc.get('same_model') or {}).items() if isinstance(v, dict)}


def boards(meas):
    """{model: {source: value}}: the numeric cited readings on the five-axis component dimensions of
    public-measurements.json (first entry per source)."""
    dims = (meas or {}).get('axes') or {}
    out = {}
    for m, fields in ((meas or {}).get('models') or {}).items():
        got = out.setdefault(m, {})
        for dim in dims:
            for e in fields.get(dim) or ():
                v = e.get('value')
                if e.get('source') and e['source'] not in got and isinstance(v, (int, float)) and not isinstance(v, bool):
                    got[e['source']] = v
    return out


def versions(acc):
    """{row: declared version number} (accounts.json `series_versions`)."""
    return dict(((acc.get('series_versions') or {}).get('versions') or {}))


def distance(target, other, series_rows, declared):
    """The version distance of `other` from `target` within the series `series_rows` (target's siblings): the steps
    between their ranks among the series' distinct declared versions; None when either has no declared version."""
    if target not in declared or other not in declared:
        return None
    ladder = sorted({declared[x] for x in (target, *series_rows) if x in declared})
    return abs(ladder.index(declared[target]) - ladder.index(declared[other]))


def nearest(target, candidates, series_rows, declared, small, same=None, line_of=None, other_line_up_to=None):
    """[(version distance, [tied candidates])], nearest first (see the module docstring). `series_rows`: the target's
    relatives; `declared`: `versions(acc)`; `small`: `small_rows(acc)`; `same`: `same_model(acc)`, whose declared model
    for `target` comes first, alone; `line_of`: `lines(acc)`, the same line first at equal distance;
    `other_line_up_to`: a candidate of another line is used only up to that version distance (an ability facet's
    relatives of every line, round 6 item 4). Candidates with one key form one group, in no order a name decides."""
    model = ((same or {}).get(target) or {}).get('model')
    line_of = line_of or {}
    groups = {}
    for c in candidates:
        d = distance(target, c, series_rows, declared)
        other_line = int(line_of.get(c) != line_of.get(target))
        if other_line and c != model and other_line_up_to is not None and (d is None or d > other_line_up_to):
            continue
        k = ((-1, 0, 0, 0) if c == model else
             (0 if d is not None else 1, d or 0, other_line, int((c in small) != (target in small))))
        groups.setdefault(k, []).append(c)
    return [(None if k[0] > 0 else 0 if k[0] < 0 else k[1], sorted(v)) for k, v in sorted(groups.items())]
