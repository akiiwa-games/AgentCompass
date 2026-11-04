"""Usage-aware routing distribution simulation and ledger replay.

Usage reaches the allocator the way `roll` feeds it: a collector cache document goes through
route_judge.usage_snapshot (remaining percent before reset, freshness, pools), then allocator.plan,
which merges discovery and quota sources itself. Synthetic usage states are written as cache
documents, never as hand-made allocator input, so the simulation cannot drift from routing.

Modes:
  machine  this machine as routing sees it: accounts-override checkboxes and the host policy apply.
  fresh    a fresh install: public data plus discovered accounts; no personal choices, no host policy.

Read-only: the CLI copies the small state files routing reads (accounts-override, host policy, capacity,
overrides, public data) into a throwaway state dir, so discovery bookkeeping and host-policy enrollment
land there; the live usage cache and the ledger are only read. The usage reading is made with an empty
discovery inventory, so the Claude session-limit overlay (which may persist rate-limit files) is skipped.

  python route_simulation.py grid [--mode machine|fresh|both] [--state NAME ...] [--json out.json]
  python route_simulation.py replay [--last N] [--json out.json]
"""
import argparse
import ast
import copy
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts_override  # noqa: E402
import allocator  # noqa: E402
import host_policy  # noqa: E402
import provider_inventory  # noqa: E402
import route_judge  # noqa: E402
import usage_cache  # noqa: E402

DIFFICULTIES = ('routine', 'hard', 'coupled', 'frontier')
SHORT_HOURS = 24
AMPLE_USED, SCARCE_USED, SHORT_SCARCE_USED = 10.0, 95.0, 98.0
STALE_AGE = timedelta(days=3)

# Scenario data: (family, window ids or None for all, or 'short' for windows under SHORT_HOURS).
STATES = {
    'live': None,
    'ample': [],
    'claude scarce': [('claude', None)],
    'codex scarce': [('codex', None)],
    'antigravity gemini scarce': [('antigravity', ('gemini-weekly', 'gemini-5h'))],
    'antigravity 3p scarce': [('antigravity', ('3p-weekly', '3p-5h'))],
    'grok scarce': [('grok', None)],
    'claude+codex scarce': [('claude', None), ('codex', None)],
    'claude+antigravity scarce': [('claude', None), ('antigravity', None)],
    'claude+codex+antigravity scarce': [('claude', None), ('codex', None), ('antigravity', None)],
    '5h-only scarce': [(None, 'short')],
    'unknown/stale': 'stale',
}


# --- accounts -------------------------------------------------------------------------------------
def machine_accounts():
    """The document routing uses on this machine (allocator.load_accounts)."""
    return allocator.load_accounts()


def fresh_accounts(document=None, inventory=None):
    """Public data plus discovery, as load_accounts builds it, minus personal overrides and machine measurements."""
    doc = document if document is not None else json.loads(allocator.fleet_document_path().read_text(encoding='utf-8-sig'))
    accounts = accounts_override.normalize(copy.deepcopy(doc))
    accounts = provider_inventory.apply(accounts, inventory)
    provider_inventory.apply_shared_quota(accounts)
    return accounts_override.normalize(accounts)


def family_of(name, accounts):
    return provider_inventory.account_family(name, accounts, accounts.get('_discovery') or {})


# --- usage ----------------------------------------------------------------------------------------
def snapshot(document, accounts, now):
    """route_judge.usage_snapshot over one cache document (the roll pipeline)."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / usage_cache.CACHE_FILE
        path.write_text(json.dumps(document), encoding='utf-8')
        return route_judge.usage_snapshot(path, None, now, quota_sources=provider_inventory.quota_sources(accounts),
                                          inventory={'accounts': {}})


def metered_accounts(accounts):
    """Accounts with their own quota windows (pooled followers mirror their source)."""
    followers = provider_inventory.quota_sources(accounts)
    return {name: spec for name, spec in (accounts.get('accounts') or {}).items()
            if isinstance(spec.get('windows'), dict) and spec['windows'] and name not in followers}


def synthetic_document(accounts, now, scenario):
    """Collector cache rows for every windowed account: ample everywhere except the scenario's scarce windows."""
    providers = {}
    stale = scenario == 'stale'
    for name, spec in metered_accounts(accounts).items():
        family = family_of(name, accounts)
        windows = []
        for wid, wspec in spec['windows'].items():
            hours = float(wspec.get('hours') or 168)
            short = hours < SHORT_HOURS
            scarce = not stale and any(
                (fam is None or fam == family) and (ids is None or (ids == 'short' and short) or (isinstance(ids, tuple) and wid in ids))
                for fam, ids in scenario)
            if scarce:
                used, reset = (SHORT_SCARCE_USED, 3.0) if short else (SCARCE_USED, hours * 0.7)
            else:
                used, reset = AMPLE_USED, hours * 0.5
            windows.append({'id': wid, 'used_pct': used, 'resets_at': (now + timedelta(hours=reset)).isoformat()})
        fetched = now - STALE_AGE if stale else now
        providers[name] = {'fetched_at': fetched.isoformat(), 'stale': stale, 'available': True, 'windows': windows}
    return {'providers': providers}


LIVE_PATHS = None    # set by the CLI sandbox to the real cache copies before the state dir moves


def live_document():
    """The collector cache as `roll` reads it (newest provider row across its copies)."""
    documents = []
    for path in LIVE_PATHS or usage_cache.read_paths():
        try:
            documents.append((path, usage_cache.read_document(path)))
        except (OSError, ValueError, UnicodeError):
            continue
    return usage_cache.merge(documents)[0] if documents else {'providers': {}}


def ledger_document(compact):
    """A ledger record's compact usage as the cache document it was read from (fetched_at, stale, windows)."""
    providers = {}
    for name, row in (compact or {}).items():
        if isinstance(row, dict) and row.get('fetched_at'):
            providers[name] = {'fetched_at': row['fetched_at'], 'stale': row.get('status') == 'stale', 'available': True,
                               'windows': [w for w in row.get('windows') or [] if isinstance(w, dict)]}
    return {'providers': providers}


def usage_for(state, accounts, now):
    scenario = STATES[state]
    if scenario is None:
        return snapshot(live_document(), accounts, now)
    return snapshot(synthetic_document(accounts, now, scenario), accounts, now)


# --- grid -----------------------------------------------------------------------------------------
def grid_tasks(accounts):
    kinds = sorted(allocator.load_capabilities(accounts=accounts)['routes'])
    return [{'objective': f'{kind} task', 'work_kind': kind, 'difficulty': difficulty, 'hours': 2,
             'needs': {'local_only': local}}
            for kind in kinds for difficulty in DIFFICULTIES for local in (True, False)]


def label(bundle):
    """A model as the account serves it: the same model on two subscriptions draws on different quota."""
    return f"{bundle['model']}@{bundle['account']}" if bundle.get('model') else 'none'


def run_state(accounts, usage, now, tasks, host_policy_config):
    wins_bundle, wins_model = Counter(), Counter()
    seen = defaultdict(lambda: {'eligible': 0, 'gaps': [], 'excluded': Counter()})
    for task in tasks:
        decision = allocator.plan(task, usage, now, accounts=copy.deepcopy(accounts), overrides=[], records=[],
                                  host_policy_config=host_policy_config)
        selected = decision.get('selected_bundle') or {}
        wins_bundle[decision.get('choice') or 'none'] += 1
        wins_model[label(selected)] += 1
        top = next((c['score'] for c in decision['ranking'] if c['score'] is not None), None)
        for c in decision['ranking']:
            row = seen[c['id']]
            if c['excluded']:
                row['excluded'].update(c['excluded'])
            elif c['score'] is not None and top is not None:
                row['eligible'] += 1
                row['gaps'].append(top - c['score'])
    return wins_bundle, wins_model, seen


def grid(mode, states=None, now=None):
    now = now or datetime.now(timezone.utc)
    accounts = machine_accounts() if mode == 'machine' else fresh_accounts()
    # machine: the host policy routing loads; fresh: no policy file exists yet, so every account is allowed.
    policy = None if mode == 'machine' else {'allowed_accounts': list(accounts.get('accounts') or {})}
    tasks = grid_tasks(accounts)
    flat = allocator.bundles(accounts)
    enabled = {bid: b for bid, b in flat.items() if not b.get('disabled')}
    report = {'mode': mode, 'tasks_per_state': len(tasks), 'states': {}}
    totals = Counter()
    seen_all = defaultdict(lambda: {'eligible': 0, 'gaps': [], 'excluded': Counter()})
    for state in states or STATES:
        usage = usage_for(state, accounts, now)
        bundle_wins, model_wins, seen = run_state(accounts, usage, now, tasks, policy)
        totals.update(model_wins)
        for bid, row in seen.items():
            seen_all[bid]['eligible'] += row['eligible']
            seen_all[bid]['gaps'] += row['gaps']
            seen_all[bid]['excluded'].update(row['excluded'])
        report['states'][state] = {'bundles': dict(bundle_wins.most_common()), 'models': dict(model_wins.most_common())}
    models = sorted({label(b) for b in enabled.values()})
    nowhere = {}
    for model in models:
        if totals.get(model):
            continue
        ids = [bid for bid, b in enabled.items() if label(b) == model]
        why = {}
        for bid in ids:
            row = seen_all.get(bid)
            if row is None:
                why[bid] = 'never a candidate (no route applies)'
                continue
            gaps = sorted(row['gaps'])
            why[bid] = {'eligible_in': row['eligible'], 'median_gap_to_top': round(gaps[len(gaps) // 2], 4) if gaps else None,
                        'top_exclusions': row['excluded'].most_common(3)}
        nowhere[model] = why
    report['wins_nowhere'] = nowhere
    return report


def explain(mode, state, task, bundle_ids=(), top=5, now=None):
    """One task under one usage state: the top of the ranking plus the named bundles, with score components."""
    now = now or datetime.now(timezone.utc)
    accounts = machine_accounts() if mode == 'machine' else fresh_accounts()
    policy = None if mode == 'machine' else {'allowed_accounts': list(accounts.get('accounts') or {})}
    decision = allocator.plan(task, usage_for(state, accounts, now), now, accounts=accounts, overrides=[], records=[],
                              host_policy_config=policy)
    keep = [c for c in decision['ranking'] if c['score'] is not None][:top]
    keep += [c for c in decision['ranking'] if c['id'] in bundle_ids and c not in keep]
    return [{'id': c['id'], 'score': c['score'], 'excluded': c['excluded'], 'components': c['components']} for c in keep]


# --- replay ---------------------------------------------------------------------------------------
def ledger_records(path=None, last=None):
    path = Path(path) if path else route_judge.ledger_path()
    rows = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            snap = row.get('decision_snapshot')
            if (row.get('stage') == 'plan' and isinstance(snap, dict) and isinstance(snap.get('task'), dict)
                    and isinstance(row.get('usage'), dict) and not row.get('constraint')):
                rows.append(row)
    return rows[-last:] if last else rows


def replay_task(row):
    task = dict(row['decision_snapshot']['task'])
    if isinstance(task.get('profile'), str):
        try:
            task['profile'] = ast.literal_eval(task['profile'])
        except (ValueError, SyntaxError):
            task.pop('profile')
    if row.get('hours'):
        task.setdefault('hours', row['hours'])
    return task


def replay(last=None, path=None, accounts=None):
    """Re-plan each ledger decision with the usage it saw, read through the roll pipeline at its own time.

    The ledger keeps fetched_at, stale and each window's used_pct/resets_at; it drops a Magpie pool's account
    count, so a pooled account reads as one subscription's worth.
    """
    accounts = accounts or machine_accounts()
    out = []
    for row in ledger_records(path, last):
        now = datetime.fromisoformat(row['at'])
        usage = snapshot(ledger_document(row['usage']), accounts, now)
        decision = allocator.plan(replay_task(row), usage, now, accounts=copy.deepcopy(accounts), overrides=[], records=[])
        selected = decision.get('selected_bundle') or {}
        out.append({'at': row['at'], 'kind': row.get('kind'), 'difficulty': row.get('difficulty'),
                    'ledger_choice': row.get('choice'), 'choice': decision.get('choice'), 'model': selected.get('model')})
    return out


STATE_COPY_MAX_BYTES = 1 << 20
STATE_COPY_DIRS = ('public-data', 'grok-bot')    # discovery reads the fetched public data and the Grok Bot config


def sandbox_state(folder):
    """Point routing state at a copy of the real state dir's small files; returns (live cache path, ledger path)."""
    source = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass').expanduser()
    live = [str(p) for p in usage_cache.read_paths()]
    ledger = route_judge.ledger_path()
    target = Path(folder) / '.agent-compass'
    target.mkdir(parents=True)
    for path in source.glob('*.json'):
        if path.is_file() and path.stat().st_size <= STATE_COPY_MAX_BYTES:
            shutil.copy2(path, target / path.name)
    for sub in STATE_COPY_DIRS:
        if (source / sub).is_dir():
            shutil.copytree(source / sub, target / sub, ignore=shutil.ignore_patterns('*.lock', 'tasks'))
    policy_path, _ = host_policy.policy_path()
    if policy_path.is_file():
        shutil.copy2(policy_path, target / 'host-policy.json')
    os.environ.update(ROUTER_STATE_DIR=str(target), ROUTER_HOST_POLICY=str(target / 'host-policy.json'),
                      ROUTER_CAPACITY=str(target / 'capacity.json'), ROUTER_OVERRIDES=str(target / 'router-overrides.json'),
                      ROUTER_ACCOUNTS_OVERRIDE=str(target / 'accounts-override.json'))
    return live, ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    g = sub.add_parser('grid')
    g.add_argument('--mode', choices=('machine', 'fresh', 'both'), default='both')
    g.add_argument('--state', action='append', choices=list(STATES))
    g.add_argument('--json')
    e = sub.add_parser('explain')
    e.add_argument('--mode', choices=('machine', 'fresh'), default='fresh')
    e.add_argument('--state', choices=list(STATES), default='ample')
    e.add_argument('--kind', required=True)
    e.add_argument('--difficulty', choices=DIFFICULTIES, default='hard')
    e.add_argument('--local', action='store_true')
    e.add_argument('--bundle', action='append', default=[])
    e.add_argument('--top', type=int, default=5)
    e.add_argument('--json')
    r = sub.add_parser('replay')
    r.add_argument('--last', type=int)
    r.add_argument('--ledger')
    r.add_argument('--json')
    args = parser.parse_args(argv)
    global LIVE_PATHS
    folder = tempfile.mkdtemp(prefix='route-simulation-')
    try:
        LIVE_PATHS, ledger = sandbox_state(folder)
        return run(args, getattr(args, 'ledger', None) or ledger)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def run(args, ledger):
    if args.command == 'grid':
        modes = ('machine', 'fresh') if args.mode == 'both' else (args.mode,)
        result = [grid(mode, args.state) for mode in modes]
        for report in result:
            print(f"== {report['mode']} ({report['tasks_per_state']} tasks per state)")
            for state, row in report['states'].items():
                print(f'  {state:28s} {row["models"]}')
            print(f"  wins nowhere: {sorted(report['wins_nowhere'])}")
    elif args.command == 'explain':
        task = {'objective': f'{args.kind} task', 'work_kind': args.kind, 'difficulty': args.difficulty, 'hours': 2,
                'needs': {'local_only': args.local}}
        result = explain(args.mode, args.state, task, args.bundle, args.top)
        for row in result:
            print(row['id'], row['score'], row['excluded'] or '', {k: v for k, v in row['components'].items() if v})
    else:
        result = replay(args.last, ledger)
        print(len(result), Counter(r['choice'] for r in result).most_common())
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
