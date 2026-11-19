"""The auto-bundle ids v0.7.0 derived for every provider group a saved gateway catalog or the shipped launch table offers.

`python -X utf8 audit/v070_bundle_ids.py [--tag v0.7.0] [--out audit/v070-bundle-ids.json]`

A user's checkbox on an auto bundle is stored under the bundle's id, which holds the model name as the release that
wrote it resolved it, and, for the second advertised name of that row on the account, a `#` tag of the advertised
spelling. test_accounts_override.py reads this file to check that a checkbox stored under the id v0.7.0 derived reaches
the bundle this code derives for the same advertised name and no other (round 8 item 1, round 9 item 3; AGENTS.md, what
a released version wrote for the user is kept). A group is the names one provider advertises, in the order of the saved
catalog (`<catalog>|<provider>`, `launch|<provider>`): an account lists them together, so v0.7.0 tagged the later
names of a row the way an account sees them. The ids come from the tag's own code and data: `skills/agent-compass` of
the tag is exported into a temporary directory and its `auto_bundles.derive` runs there, one group at a time, on the two
account shapes of `SETUPS`. Run it again only when the names change (a new saved catalog or launch-table row); the tag
does not.
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
SCHEMA = 'agent-compass.v070-bundle-ids/2'
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
    for group, names in job['groups'].items():
        cat = {'providers': {job['account']: {'ok': True, 'checked_at': job['now'], 'models': [{'id': n} for n in names]}}}
        got = auto_bundles.derive(acc, cat, job['now'])['bundles'].get(job['account']) or {}
        held = {b['launch_model']: bid for bid, b in got.items()}
        ids[group] = [[name, held.get(name)] for name in names]
    out[setup] = ids
json.dump(out, sys.stdout, sort_keys=True)
'''


def groups(root=ROOT):
    """{group: [model name, in catalog order]}: the names each provider of a saved gateway catalog advertises
    (`<catalog>|<provider>`) and each provider of the shipped launch table (`launch|<provider>`)."""
    sys.path.insert(0, str(root / 'audit'))
    sys.path.insert(0, str(root / 'skills' / 'agent-compass' / 'scripts'))
    import gateway_coverage
    accounts = json.loads((gateway_coverage.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig'))
    rows = [(f'{cid}|{r["provider"]}', r['model']) for cid, doc in gateway_coverage.load_catalogs().items()
            for r in doc['rows']]
    rows += [(f'launch|{r["provider"]}', r['model']) for r in gateway_coverage.launch_rows(accounts)]
    out = {}
    for group, name in rows:
        if name not in out.setdefault(group, []):
            out[group].append(name)
    return out


def derive_at(tag, wanted):
    """{setup: {group: [[name, id or None], in catalog order]}} from the tag's own `auto_bundles.derive` and accounts.json."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / 'tag.tar'
        subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), tag, 'skills/agent-compass'],
                       cwd=ROOT, check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter='data')
        skill = Path(tmp) / 'skills' / 'agent-compass'
        job = {'setups': SETUPS, 'account': ACCOUNT, 'now': NOW, 'groups': wanted}
        run = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', DERIVE, str(skill / 'scripts'),
                              str(skill / 'references' / 'accounts.json')],
                             input=json.dumps(job), capture_output=True, text=True, encoding='utf-8', check=True)
        return json.loads(run.stdout)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tag', default='v0.7.0')
    ap.add_argument('--out', default=str(OUT))
    args = ap.parse_args(argv)
    wanted = groups()
    doc = {'schema': SCHEMA, 'tag': args.tag, 'account': ACCOUNT, 'now': NOW, 'setups': SETUPS,
           'ids': derive_at(args.tag, wanted)}
    with open(args.out, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(doc, handle, indent=1, sort_keys=True, ensure_ascii=False)
        handle.write('\n')
    print(f'{len(wanted)} groups, {sum(map(len, wanted.values()))} names, {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
