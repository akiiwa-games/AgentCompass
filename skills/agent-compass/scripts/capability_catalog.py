"""Build capability views from the effective accounts catalogue.

accounts.json is the public source; accounts.local.json may replace that document.
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
            'Personal scores, route order, effort calibration and account capacity belong in the local fleet. '
            'Unknown capacity and hourly cost remain unknown; neither means unlimited. '
            'No plan capacity is assumed for a generic subscription slot.', '',
            '| Model | Tier prior | R / C / E / A / V / G priors | Visual admission |',
            '| :--- | ---: | :--- | :--- |']
    for name, spec in sorted((accounts.get('models') or {}).items()):
        if not isinstance(spec, dict):
            continue
        caps = spec.get('capabilities') or {}
        vector = ' / '.join(f'{caps.get(k, 0):.2f}' for k in
                            ('reasoning', 'coding', 'exploration', 'anti_hallucination', 'visual', 'gui'))
        rows.append(f'| `{name}` | {spec.get("tier", "unknown")} | {vector} | {bool(spec.get("visual"))} |')
    rows += ['', 'R/C/E/A/V/G = reasoning, coding, exploration, anti-hallucination, visual and GUI.', '',
             '## Effective policy', '',
             'Routes describe task scope and modality requirements. Public routes have no preferred model ordering. '
             '`capability_policy` in a local fleet can set `preferred`, `strict_order` and `restrict_models`. '
             'A restriction is state, not an explanation string. The judge receives this effective policy and its model data.', '',
             '## Regeneration', '',
             'Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. '
             'Use `--check` to verify both generated views without writing them.', '']
    return '\n'.join(rows)


def generated(accounts):
    policy = compose(accounts)
    return {'model-capabilities.json': json.dumps(policy, ensure_ascii=False, indent=2) + '\n',
            'model-capabilities.md': markdown(accounts)}


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
