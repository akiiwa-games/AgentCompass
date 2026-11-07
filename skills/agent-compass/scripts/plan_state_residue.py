"""List plan and reading residue left by the bugs fixed with account_state. Dry run: this script writes nothing.

Everything it lists clears by itself or is history, so there is nothing to apply: discovery rewrites the inventory, a good
reading rewrites its cache row, a rate rolls out of its window, and the ledger is a record of what happened. Credential
files are not opened and digests stored beside the plans are not printed.

    python -X utf8 plan_state_residue.py [--home DIR] [--state-dir DIR] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_inventory import configured_plan
import usage_cache


def _read(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _finding(kind, account, detail, clears):
    return {'kind': kind, 'account': account, 'detail': detail, 'clears': clears}


def inventory_findings(rows):
    found = []
    for name, row in rows.items():
        if not isinstance(row, dict):
            continue
        family = row.get('quota_family') or row.get('family')
        owner = row.get('quota_account')
        owner_plan = (rows.get(owner) or {}).get('plan') if owner and owner != name else None
        if row.get('plan') and owner_plan and configured_plan(family, row['plan']) != configured_plan(family, owner_plan):
            found.append(_finding('plan_differs_from_owner', name,
                                  f"plan {row['plan']}, its subscription {owner} has {owner_plan}",
                                  'the next discovery shares the usage API plan across the subscription'))
        if name == 'chatgpt-web' and row.get('plan'):
            found.append(_finding('plan_not_verified', name, f"plan {row['plan']} is a constant for a ready bridge, not a reading",
                                  'not by itself: check the seat in ChatGPT'))
        home = row.get('home')
        if name in ('claude', 'codex') and home and Path(home).name != f'.{name}':
            found.append(_finding('home_rewritten_by_caller', name, f'inventory points at {Path(home).name}',
                                  'the next discovery; a rewritten process no longer writes this file'))
    return found


def cache_findings(paths):
    labels = {}
    for path in paths:
        for name, row in (_read(path).get('providers') or {}).items():
            if isinstance(row, dict) and isinstance(row.get('plan_label'), str):
                labels.setdefault(name, {})[str(path)] = (row['plan_label'], row.get('fetched_at'))
    found, changed = [], set()
    for name, seen in labels.items():
        if len({label.strip().lower() for label, _ in seen.values()}) > 1:
            changed.add(name)
            detail = ', '.join(f'{Path(path).parent.name}: {label} ({at})' for path, (label, at) in seen.items())
            found.append(_finding('cache_copies_disagree', name, detail,
                                  'nothing reads the older copy: the newest fetched_at wins per account'))
    return found, changed


def rate_findings(capacity, plan_changed):
    found = []
    for name in sorted(plan_changed):
        for window, row in (capacity.get('observed', {}).get(name) or {}).items():
            long_rate = row.get('pct_per_hour_24h') if isinstance(row, dict) else None
            if long_rate and not row.get('pct_per_hour_3h'):
                found.append(_finding('rate_across_plan_change', f'{name}.{window}',
                                      f'24h rate {long_rate} with no rate in the last 3h',
                                      'rolls out 24h after the change; new rates stop at a plan change'))
    return found


def ledger_findings(path, own_rows):
    """Ledger rows where an account with no pool of its own carries a pooled reading: another account's numbers."""
    counts = {}
    try:
        lines = open(path, encoding='utf-8')
    except OSError:
        return []
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            usage = record.get('usage') if isinstance(record, dict) else None
            if not isinstance(usage, dict):
                continue
            for name, row in usage.items():
                windows = row.get('windows') if isinstance(row, dict) else None
                if name in own_rows and isinstance(windows, list) and any(
                        isinstance(w, dict) and (w.get('accounts') or 0) > 1 for w in windows):
                    held = counts.setdefault(name, {'rows': 0, 'first': record.get('at'), 'last': None})
                    held.update(rows=held['rows'] + 1, last=record.get('at'))
    return [_finding('ledger_readings_from_another_account', name,
                     f"{held['rows']} rows, {held['first']} to {held['last']}",
                     'history: leave it, and read routing evaluations of that period with this in mind')
            for name, held in counts.items()]


def scan(home, state_dir):
    home, state_dir = Path(home), Path(state_dir)
    rows = _read(state_dir / 'provider-inventory.json').get('accounts') or {}
    canonical = state_dir / 'usage-cache.json'
    found = inventory_findings(rows)
    cache_found, changed = cache_findings([canonical, home / '.usage-float/usage-cache.json'])
    found += cache_found
    found += rate_findings(_read(state_dir / 'capacity.json'), changed)
    current = _read(canonical).get('providers') or {}
    own = {name for name, row in current.items() if isinstance(row, dict) and not any(
        isinstance(w, dict) and (w.get('accounts') or 0) > 1 for w in row.get('windows') or [])}
    found += ledger_findings(state_dir / 'router-ledger.jsonl', own)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home()))
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    found = scan(args.home, args.state_dir or usage_cache.state_dir(args.home))
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    print(f'dry run, nothing written: {len(found)} finding(s)')
    for row in found:
        print(f"{row['kind']} {row['account']}: {row['detail']} -> clears: {row['clears']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
