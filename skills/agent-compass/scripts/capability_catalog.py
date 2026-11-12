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


def markdown(accounts, five=None, references=None):
    ref_dir = Path(references) if references is not None else REFERENCES
    if five is None:
        five = json.loads((ref_dir / 'five-axis-scores.json').read_text(encoding='utf-8'))
        import axis_scores
        import user_scores
        pub_scores = user_scores.load_public(ref_dir / 'user-scores.json', effective=False)
        overlaid = axis_scores.five_axis_overlay(
            deepcopy(accounts), five, public_scores=pub_scores, local_scores={'scores': {}}, read_local_state=False
        )
        scored_over = {}
        for m, raw_row in (five.get('models') or {}).items():
            st = ((overlaid.get('models') or {}).get(m) or {}).get('five_axis') or {}
            cats = st.get('categories') or raw_row.get('categories') or raw_row.get('axes') or {}
            scored_over[m] = {**raw_row, 'axes': cats, 'categories': cats,
                              'image_input': st.get('image_input') or raw_row.get('image_input') or {}}
        five = dict(five, models=scored_over)
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
            '| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |',
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
            status = cell.get('status')
            mark = ('u' if status in ('user', 'maintainer')
                    else '~' if status == 'estimated'
                    else '!' if cell.get('missing_facets') else '')
            cells.append(f"{cell['value']:.2f}{mark}" if cell.get('value') is not None else f"missing{mark}")
        tier = (row.get('tier') or {}).get('value')
        usd = (row.get('usd_per_hour') or {}).get('value')
        image = (row.get('image_input') or {}).get('status', 'unknown')
        rows.append(f'| `{name}` | {"-" if tier is None else f"{tier:.2f}"} | ' + ' | '.join(cells)
                    + f' | {"-" if usd is None else usd} | {image} |')
    rows += ['', 'Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from '
             'the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score '
             'and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. '
             'A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on '
             'the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing '
             'facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` '
             'stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B '
             'when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` '
             '(`estimate`).', '',
             '## Effective policy', '',
             'Routes describe task scope and modality requirements and ship a calibrated harness order. '
             'A route can fix the harness order or imply a modality flag; it does not rank models. '
             'The judge receives this effective policy.', '',
             '## Regeneration', '',
             'Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. '
             'Use `--check` to verify both generated views without writing them.', '']
    return '\n'.join(rows)


def generated(accounts, references=None):
    policy = compose(accounts)
    return {'model-capabilities.json': json.dumps(policy, ensure_ascii=False, indent=2) + '\n',
            'model-capabilities.md': markdown(accounts, references=references)}


def write_default_files(accounts=None, references=None):
    ref_dir = Path(references) if references is not None else REFERENCES
    if accounts is None:
        accounts = json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig'))
    for name, content in generated(accounts, references=ref_dir).items():
        (ref_dir / name).write_text(content, encoding='utf-8', newline='\n')


def check_files(references=None, accounts=None):
    ref_dir = Path(references) if references is not None else REFERENCES
    if accounts is None:
        accounts = json.loads((ref_dir / 'accounts.json').read_text(encoding='utf-8-sig'))
    stale = []
    for name, content in generated(accounts, references=ref_dir).items():
        target = ref_dir / name
        if not target.exists() or target.read_text(encoding='utf-8') != content:
            stale.append(name)
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    accounts = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
    if args.check:
        stale = check_files(references=REFERENCES, accounts=accounts)
        if stale:
            print('Stale generated views: ' + ', '.join(stale))
            return 1
        print('Capability views are current.')
        return 0
    write_default_files(accounts=accounts, references=REFERENCES)
    print('Generated capability JSON and Markdown.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
