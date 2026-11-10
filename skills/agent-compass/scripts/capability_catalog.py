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
            'Each model is scored on four categories (divergence, global rigor, module precision, vision and tools) that '
            'gather the type scores of the public boards, and on the quota economy; nothing else scores a model. '
            'Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.', '',
            'Route order and plan capacity ship as calibrated public defaults; plan capacity '
            'follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal '
            'settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.', '',
            '| Model | Tier | Divergence | Global rigor | Module precision | Vision and tools | USD per hour | Responsiveness | Image input |',
            '| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- |']
    scored = five.get('models') or {}
    for name, spec in sorted((accounts.get('models') or {}).items()):
        if not isinstance(spec, dict):
            continue
        row = scored.get(name) or {}
        axes = row.get('axes') or {}
        cells = []
        for axis in ('divergence', 'global_rigor', 'module_precision', 'vision_tools'):
            cell = axes.get(axis) or {}
            mark = {'estimated': '~', 'partial': '!', 'missing': '!'}.get(cell.get('status'), '')
            cells.append(f"{cell['value']:.2f}{mark}" if cell.get('value') is not None and cell.get('status') != 'missing' else 'missing')
        tier = (row.get('tier') or {}).get('value')
        usd = (row.get('usd_per_hour') or {}).get('value')
        resp = spec.get('responsiveness')
        if resp == 'not_applicable' or spec.get('responsiveness_status') == 'not_applicable':
            resp_str = 'n/a'
        elif isinstance(resp, (int, float)) and not isinstance(resp, bool):
            resp_str = f'{float(resp):.2f}'
        else:
            resp_str = 'default'
        image = (row.get('image_input') or {}).get('status', 'unknown')
        rows.append(f'| `{name}` | {"-" if tier is None else f"{tier:.2f}"} | ' + ' | '.join(cells)
                    + f' | {"-" if usd is None else usd} | {resp_str} | {image} |')
    rows += ['', 'Tier is the arena.ai Overall position on the user scale (or its interpolation from another overall board, or '
             'its same-series sibling tier); it is not a score and only estimates type scores a model lacks. A value marked `~` is '
             'such an estimate (through the tier, from a same-series sibling shifted by the tier difference or from the fit '
             'of that type score on tier); `!` marks a category with a missing type score counted as 0 (mode B when the task '
             'demands it). The record of each estimate is in `five-axis-scores.json` (`estimate`).',
             'Responsiveness (`0..1`, per-effort in `effort_curves.<model>.responsiveness`) measures per-turn '
             'live-loop speed (`T_turn = (ttft_s + 400 / output_tps) * harness_overhead`, where AA `ttft_s` is '
             '`headline.timeToFirstAnswerToken` including single-turn thinking time; mapped linearly from `[4.0s -> 1.0, 45.0s -> 0.0]` '
             'by `scoring.responsiveness` and refreshed offline via `scripts/refresh_speed_data.py`). It is active only when '
             'the task profile marks `realtime_loop: true` (`floor = 0.50`, evaluated per `(model, effort)` with automatic step-down '
             'to a faster effort when the configured effort is below floor, within the range in '
             '[Model names, efforts and pins](router-internals.md#model-names-efforts-and-pins)); models without community speed data use the '
             'conservative policy `default` (`0.52`, `responsiveness: default`), and packet/web channels (`dot`, `grok-bot`) are `n/a` (`not_applicable`).', '',
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
