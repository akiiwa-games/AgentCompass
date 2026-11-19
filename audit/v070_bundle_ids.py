"""The auto-bundle id v0.7.0 derived for every model name a saved gateway catalog or the shipped launch table offers.

`python -X utf8 audit/v070_bundle_ids.py [--tag v0.7.0] [--out audit/v070-bundle-ids.json]`

A user's checkbox on an auto bundle is stored under the bundle's id, which holds the model name as the release that
wrote it resolved it. test_accounts_override.py reads this file to check that a checkbox stored under the id v0.7.0
derived reaches the bundle this code derives for the same advertised name (round 8 item 1; AGENTS.md, what a released
version wrote for the user is kept). The ids come from the tag's own code and data: `skills/agent-compass` of the tag is
exported into a temporary directory and its `auto_bundles.derive` runs there, one name at a time, on the two account
shapes of `SETUPS`. Run it again only when the names change (a new saved catalog or launch-table row); the tag does not.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / 'v070-bundle-ids.json'
SCHEMA = 'agent-compass.v070-bundle-ids/1'
NOW = 2_000_000_000
# The account shapes a derived id depends on: a metered proxy without bundles of its own takes the harness from the model
# (`auto_bundles._account_harnesses`); a subscription account with a bundle of its own launches every model through
# that bundle's harness.
SETUPS = {
    'harness_by_model': {'billing': 'metered', 'backend_kind': 'metered_usd', 'family': 'codex',
                         'windows': {'balance': {}}, 'bundles': {}},
    'fixed_harness': {'billing': 'subscription', 'family': 'codex', 'windows': {},
                      'bundles': {'own': {'harness': 'codex', 'model': 'placeholder-own-model', 'effort': 'high'}}},
}
ACCOUNT = 'acct'

DERIVE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import auto_bundles
ref = json.load(open(sys.argv[2], encoding='utf-8-sig'))
job = json.load(sys.stdin)
out = {}
for setup, spec in job['setups'].items():
    acc = {'models': ref['models'], 'aliases': ref.get('aliases', {}), 'superseded': ref.get('superseded', {}),
           'families': ref.get('families'), 'accounts': {job['account']: spec}}
    ids = {}
    for name in job['names']:
        cat = {'providers': {job['account']: {'ok': True, 'checked_at': job['now'], 'models': [{'id': name}]}}}
        got = auto_bundles.derive(acc, cat, job['now'])['bundles'].get(job['account']) or {}
        ids[name] = next(iter(got), None)
    out[setup] = ids
json.dump(out, sys.stdout, sort_keys=True)
'''


def names(root=ROOT):
    """Every model name of the saved gateway catalogs and of the shipped launch table, sorted."""
    sys.path.insert(0, str(root / 'audit'))
    sys.path.insert(0, str(root / 'skills' / 'agent-compass' / 'scripts'))
    import gateway_coverage
    accounts = json.loads((gateway_coverage.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
    rows = [r for doc in gateway_coverage.load_catalogs().values() for r in doc['rows']]
    rows += gateway_coverage.launch_rows(accounts)
    return sorted({r['model'] for r in rows})


def derive_at(tag, wanted):
    """{setup: {name: id or None}} from the tag's own `auto_bundles.derive` and accounts.json."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / 'tag.tar'
        subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), tag, 'skills/agent-compass'],
                       cwd=ROOT, check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter='data')
        skill = Path(tmp) / 'skills' / 'agent-compass'
        job = {'setups': SETUPS, 'account': ACCOUNT, 'now': NOW, 'names': wanted}
        run = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', DERIVE, str(skill / 'scripts'),
                              str(skill / 'references' / 'accounts.json')],
                             input=json.dumps(job), capture_output=True, text=True, encoding='utf-8', check=True)
        return json.loads(run.stdout)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tag', default='v0.7.0')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args(argv)
    wanted = names()
    doc = {'schema': SCHEMA, 'tag': args.tag, 'account': ACCOUNT, 'now': NOW, 'setups': SETUPS,
           'ids': derive_at(args.tag, wanted)}
    with open(args.out, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(doc, handle, indent=1, sort_keys=True, ensure_ascii=False)
        handle.write('\n')
    print(f'{len(wanted)} names, {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
