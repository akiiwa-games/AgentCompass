"""Same-series siblings and the base a converted model takes (round 058, user 2026-11-10: "后续同系列新出，缺少实测数据的情况下，
先按照同系列的全分数进行折算，不要留下任何一个默认值或者fallback"; five_axis.py estimates through the tier on top of it).

Series membership is the router's own resolution of each row's name with the row itself left out
(model_resolution.resolve_model): a `family` match joins the row to that family's row (auto_bundles.series_target), and
an `alias` match joins it to the row it is a release of (a row `x-preview` joins row `x`: the resolver drops `-preview`
and other vendor tags); plus the public data's `superseded` pairs and `same_model` rows, joined transitively. A
small-size row is never in the series of a non-small one (`size`): the size of a row is the declared `size` of the
`families` row that decides its name (model_resolution.family_row); a join between a `small` row and any other row is
refused, whatever relation proposed it, except a `same_model` declaration (the very model). No name list here.

The downward relation (`tier_up`, owner 2026-11-18) is a second relation, not series membership: a small row listed in
its `families` row's `models` (a current member of that family) whose `families` row declares `tier_up` (the pattern of
the same line's next larger family row) has, as its larger model, the first model of that larger row the catalog has;
`tier_up_version` declares whether that model is the same version as the row's current members (`same`) or the nearest
version of the line (`nearest`). five_axis.py uses it only for a facet the small row's own same-size series and the tier
fit leave without any value: the small row's own gap only for `same`, the fleet's size step for both.

A `same_model` row (accounts.json) declares that one row is the very model another row names (a vendor statement with its
url): the declared model is that row's base before any other sibling, and the row carries the type scores of the
categories the row lists under `carries`; a category it does not list is the channel's own and stays missing (a
property of the channel, not of the model behind it).

The base for a score is the sibling that carries a direct value for it, nearest on a public reading both rows actually
have: the AA Intelligence Index when both have it; else the cited board readings both have on the five-axis component
dimensions (public-measurements `axes`; every such value is on the one anchor-relative scale, `conversion`), by the
mean absolute difference over those shared boards; only when the two share no reading at all, nearest on the tier
(tiers.py, the arena.ai Overall position). The key breaks ties.
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


def groups(acc):
    """{row key: sorted same-series sibling keys} over the score rows of `acc`."""
    models = acc.get('models') or {}
    rows = {k for k, v in models.items() if isinstance(v, dict)}
    aliases = acc.get('aliases') or {}
    fams = model_resolution.family_patterns(acc)
    small = small_rows(acc)
    parent = {k: k for k in rows}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def join(a, b):
        if a in rows and b in rows and (a in small) == (b in small):
            parent[find(a)] = find(b)

    for k in sorted(rows):
        rest = {j: v for j, v in models.items() if j != k and isinstance(v, dict)}
        found = model_resolution.resolve_model(k, rest, {a: t for a, t in aliases.items() if t != k}, fams)
        if found['source'] in ('family', 'alias') and found['key']:
            join(k, found['key'])
    for old, entry in sorted((acc.get('superseded') or {}).items()):
        if isinstance(entry, dict):
            join(old, entry.get('by'))
    for row, entry in sorted(same_model(acc).items()):
        if row in rows and entry.get('model') in rows:
            parent[find(row)] = find(entry['model'])
    return {k: sorted(j for j in rows if j != k and find(j) == find(k)) for k in rows}


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
    return out


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


def nearest(target, candidates, index, board, tier, same=None):
    """`candidates` in base order (see the module docstring). `index`/`tier`: {key: value}; `board`: `boards(meas)`;
    `same`: `same_model(acc)`, whose declared model for `target` comes first."""
    own = board.get(target) or {}
    declared = ((same or {}).get(target) or {}).get('model')

    def key(c):
        if c == declared:
            return -1, 0.0, c
        if index.get(target) is not None and index.get(c) is not None:
            return 0, abs(index[target] - index[c]), c
        theirs = board.get(c) or {}
        shared = sorted(set(own) & set(theirs))
        if shared:
            return 1, sum(abs(own[s] - theirs[s]) for s in shared) / len(shared), c
        if isinstance(tier.get(target), (int, float)) and isinstance(tier.get(c), (int, float)):
            return 2, abs(tier[target] - tier[c]), c
        return 3, 0.0, c
    return sorted(candidates, key=key)
