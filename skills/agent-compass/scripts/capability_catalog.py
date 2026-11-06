"""Build capability views from the effective accounts catalogue.

accounts.json (shipped, or the newer fetched public copy) is the only source.
Generated JSON/Markdown views are documentation, never another runtime authority.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path


REFERENCES = Path(__file__).resolve().parents[1] / 'references'


def compose(accounts):
    """Return the route policy and model data from one effective configuration."""
    policy = accounts.get('capability_policy')
    if not isinstance(policy, dict):
        # Older full local fleets predate the policy field. They inherit public
        # route definitions until their owner supplies an explicit policy.
        policy = json.loads((REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))['capability_policy']
    result = deepcopy(policy)
    result['mainstream_defaults'] = {
        'models': {name: deepcopy(spec) for name, spec in (accounts.get('models') or {}).items()
                   if isinstance(spec, dict)}}
    return result


def markdown(accounts):
    rows = ['# Model capability catalogue', '',
            'Generated from `accounts.json` by `scripts/capability_catalog.py`; do not edit this view.', '',
            'Public scores are conservative, low-confidence routing priors, not measured benchmarks or a vendor ranking. '
            'Tier and text-capability values are the checked-in calibrated baseline; modality flags describe admission only. '
            'Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.', '',
            'Route order, scoring weights and plan capacity ship as calibrated public defaults; plan capacity '
            'follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal '
            'settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.', '',
            '| Model | Tier prior | R / C / E / A / V / G priors | Responsiveness | Visual admission |',
            '| :--- | ---: | :--- | :--- | :--- |']
    for name, spec in sorted((accounts.get('models') or {}).items()):
        if not isinstance(spec, dict):
            continue
        caps = spec.get('capabilities') or {}
        vector = ' / '.join(f'{caps.get(k, 0):.2f}' for k in
                            ('reasoning', 'coding', 'exploration', 'anti_hallucination', 'visual', 'gui'))
        resp = spec.get('responsiveness')
        if resp == 'not_applicable' or spec.get('responsiveness_status') == 'not_applicable':
            resp_str = 'n/a'
        elif isinstance(resp, (int, float)) and not isinstance(resp, bool):
            resp_str = f'{float(resp):.2f}'
        else:
            resp_str = 'default'
        rows.append(f'| `{name}` | {spec.get("tier", "unknown")} | {vector} | {resp_str} | {bool(spec.get("visual"))} |')
    rows += ['', 'R/C/E/A/V/G = reasoning, coding, exploration, anti-hallucination, visual and GUI.',
             'Responsiveness (`0..1`, per-effort in `effort_curves.<model>.responsiveness`) measures per-turn '
             'live-loop speed (`T_turn = (ttft_s + 400 / output_tps) * harness_overhead`, where AA `ttft_s` is '
             '`headline.timeToFirstAnswerToken` including single-turn thinking time; mapped linearly from `[4.0s -> 1.0, 45.0s -> 0.0]` '
             'by `scoring.responsiveness` and refreshed offline via `scripts/refresh_speed_data.py`). It is active only when '
             'the task profile marks `realtime_loop: true` (`floor = 0.50`, evaluated per `(model, effort)` with automatic step-down '
             'to a faster supported effort when the configured effort is below floor); models without community speed data use the '
             'conservative policy `default` (`0.52`, `responsiveness: default`), and packet/web channels (`dot`, `grok-bot`) are `n/a` (`not_applicable`).', '',
             '## Effective policy', '',
             'Routes describe task scope and modality requirements and ship a calibrated `preferred` order. '
             '`capability_policy` in a local fleet can replace `preferred`, `strict_order` and `restrict_models`. '
             'A restriction is state, not an explanation string. The judge receives this effective policy and its model data.', '',
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
