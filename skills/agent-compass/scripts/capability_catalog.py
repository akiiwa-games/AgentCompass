"""Build capability views from the effective accounts catalogue and the public five-axis scores.

accounts.json (shipped, or the newer fetched public copy) holds the routes and the model rows' routing flags;
`five-axis-scores.json` holds the four category scores and the economy's usd (calibration/five_axis.py).
Generated JSON/Markdown views are documentation, never another runtime authority.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path


REFERENCES = Path(__file__).resolve().parents[1] / 'references'


def compose(accounts):
    """Return the route policy from one effective configuration."""
    return deepcopy(accounts['capability_policy'])


def markdown(accounts, five=None):
    if five is None:
        five = json.loads((REFERENCES / 'five-axis-scores.json').read_text(encoding='utf-8'))
    rows = ['# Model capability catalogue', '',
            'Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.', '',
            'Each model is scored on four categories (divergence, coding, aesthetics, tool operation) that gather '
            'the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), '
            'and on the quota economy; nothing else scores a model. Tool operation shows the weaker of responsiveness and '
            'image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model '
            'availability.', '',
            'Route order and plan capacity ship as calibrated public defaults; plan capacity '
            'follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal '
            'settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.', '',
            '| Model | Tier | Divergence | Coding | Aesthetics | Tool operation | USD per hour | Image input |',
            '| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |']
    scored = five.get('models') or {}
    for name, spec in sorted((accounts.get('models') or {}).items()):
        if not isinstance(spec, dict):
            continue
        row = scored.get(name) or {}
        axes = row.get('axes') or {}
        cells = []
        for axis in ('divergence', 'coding', 'aesthetics', 'tool_operation'):
            cell = axes.get(axis) or {}
            mark = '~' if cell.get('status') == 'estimated' else ('!' if cell.get('missing_facets') else '')
            cells.append(f"{cell['value']:.2f}{mark}" if cell.get('value') is not None else 'missing')
        tier = (row.get('tier') or {}).get('value')
        usd = (row.get('usd_per_hour') or {}).get('value')
        image = (row.get('image_input') or {}).get('status', 'unknown')
        rows.append(f'| `{name}` | {"-" if tier is None else f"{tier:.2f}"} | ' + ' | '.join(cells)
                    + f' | {"-" if usd is None else usd} | {image} |')
    rows += ['', 'Tier is the arena.ai Overall position on the user scale (or its interpolation from another overall board, or '
             'its same-series sibling tier); it is not a score and only estimates facets a model lacks. A value marked `~` '
             'holds such an estimate (a same-series sibling, else the fit of that facet on the tier); `!` marks a category '
             'computed from only the facets that have values; `missing` is a category with no value at all (mode B when the '
             'task demands it). The record of each estimate is in `five-axis-scores.json` (`estimate`).', '',
             '## Effective policy', '',
             'Routes describe task scope and modality requirements and ship a calibrated harness order. '
             'A route can fix the harness order or imply a modality flag; it does not rank models. '
             'The judge receives this effective policy.', '',
             '## Regeneration', '',
             'Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. '
             'Use `--check` to verify both generated views without writing them.', '']
    return '\n'.join(rows)


def generated(accounts):
    policy = compose(accounts)
    return {'model-capabilities.json': json.dumps(policy, ensure_ascii=False, indent=2) + '\n',
            'model-capabilities.md': markdown(accounts)}


def write_default_files(accounts=None):
    if accounts is None:
        accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
    for name, content in generated(accounts).items():
        (REFERENCES / name).write_text(content, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
    stale = []
    for name, content in generated(accounts).items():
        target = REFERENCES / name
        if args.check:
            if not target.exists() or target.read_text(encoding='utf-8') != content:
                stale.append(name)
        else:
            target.write_text(content, encoding='utf-8')
    if stale:
        print('Stale generated views: ' + ', '.join(stale))
        return 1
    print('Capability views are current.' if args.check else 'Generated capability JSON and Markdown.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
