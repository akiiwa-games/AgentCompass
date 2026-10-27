"""Fixed-input comparison, isolated from host policy and live capacity/usage by default.

Use --profile with the SAME policy on both hosts for the restricted comparison.
No model calls, local quota refresh, or writes to the live policy/cache.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--compare', type=Path, help='Other host result JSON to compare, including policy.')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.skill/'scripts'))
    import allocator
    import host_policy
    fixture = json.loads(args.fixture.read_text(encoding='utf-8-sig'))
    accounts = allocator.load_accounts(args.skill/'references/accounts.json')
    results = {}
    with tempfile.TemporaryDirectory() as temporary:
        path = (args.profile, True) if args.profile else (Path(temporary)/'absent-default.json', False)
        with patch.object(host_policy, 'policy_path', return_value=path):
            profile = host_policy.load_policy()
            for case in fixture['cases']:
                result = allocator.plan(case['task'], fixture['usage'], now=datetime.fromisoformat(fixture['now']),
                                        accounts=accounts, unavailable=case.get('unavailable', ()))
                encoded = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
                results[case['name']] = {'sha256': hashlib.sha256(encoded).hexdigest(),
                                        'choice': result['choice'], 'action': result['decision'].get('action')}
    baseline_equal = results == fixture.get('expected')
    compared = None
    if args.compare:
        expected = json.loads(args.compare.read_text(encoding='utf-8-sig'))
        compared = results == expected['cases'] and profile == expected['policy']
    passed = compared if args.compare else (baseline_equal if args.profile is None else None)
    output = {'pass': passed, 'mode': 'explicit-profile' if args.profile else 'isolated-default',
              'policy': profile, 'baseline_equal': baseline_equal, 'cross_host_equal': compared, 'cases': results}
    args.out.write_text(json.dumps(output, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'pass': passed, 'mode': output['mode'], 'cases': len(results), 'baseline_equal': baseline_equal}))
    return 1 if passed is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
