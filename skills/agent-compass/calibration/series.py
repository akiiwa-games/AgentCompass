"""Same-series siblings and the base a converted model takes (round 058, user 2026-11-10: "后续同系列新出，缺少实测数据的情况下，
先按照同系列的全分数进行折算，不要留下任何一个默认值或者fallback"; five_axis.py estimates through the tier on top of it).

Series membership is the router's own family resolution (scripts/auto_bundles.series_target over
model_resolution.resolve_model, with the row itself left out so it can resolve to another row) plus the public data's
`superseded` pairs and `same_model` rows, joined transitively. A small-size name is never in the series of a non-small
one. No name list here.

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
import auto_bundles  # noqa: E402


def groups(acc):
    """{row key: sorted same-series sibling keys} over the score rows of `acc`."""
    models = acc.get('models') or {}
    rows = {k for k, v in models.items() if isinstance(v, dict)}
    aliases = acc.get('aliases') or {}
    parent = {k: k for k in rows}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def join(a, b):
        if a in rows and b in rows:
            parent[find(a)] = find(b)

    for k in sorted(rows):
        rest = dict(acc, models={j: v for j, v in models.items() if j != k},
                    aliases={a: t for a, t in aliases.items() if t != k})
        join(k, auto_bundles.series_target(k, rest))
    for old, entry in sorted((acc.get('superseded') or {}).items()):
        if isinstance(entry, dict):
            join(old, entry.get('by'))
    for row, entry in sorted(same_model(acc).items()):
        if row in rows and entry.get('model') in rows:
            parent[find(row)] = find(entry['model'])
    return {k: sorted(j for j in rows if j != k and find(j) == find(k)) for k in rows}


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
