"""Per-stage help for `agentcompass <stage> -h`: only that stage's options, the few that matter first.

route_judge.py keeps one flat option set (existing automation passes any of them to any stage). A stage's row of `STAGES`
is what its help shows (`--help-all` still prints every option). What a stage reads is `reads(stage)`: its row and the
options of `UNLISTED`, which it reads and its help leaves to `--help-all` so that the help stays the few a caller sets.
`route_judge.unread_flags` names a given flag outside `reads(stage)` in a note of the stage's result.
"""
import argparse

PROG = 'agentcompass'

# stage -> (summary, usage line, options the caller normally gives, options that are optional extras)
STAGES = {
    'roll': ('Hand a task to the router: it picks owner, model and effort, launches the worker and says what to do next.',
             'roll (--task TEXT | --task - | --brief FILE)',
             ('task', 'brief'),
             ('hours', 'local_only', 'can_wait_hours', 'keep_whole', 'file', 'apply_to', 'fast', 'resume', 'new_worker', 'workspace',
              'provider', 'account', 'model', 'effort', 'basis')),
    'plan': ('Rank the signed-in model x effort bundles for a task file without launching anything (write no ledger row with --dry-run).',
             'plan --input TASK.json [--dry-run]   |   plan --demo --dry-run',
             ('input',),
             ('dry_run', 'demo', 'format', 'hours', 'can_wait_hours', 'fast', 'keep_whole', 'score', 'no_score', 'score_timeout', 'usage_cache')),
    'providers': ('Refresh and print the provider inventory: which accounts, CLIs and models this machine can launch.',
                  'providers', (), ()),
    'usage': ('Print the merged provider usage windows the router would route on (read only).',
              'usage --show [--accounts KEY ...]', ('show',), ('accounts', 'usage_cache')),
    'score': ('Set, list, clear or review manual capability scores (demand-level scale: 1 = panel weakest, 3 = panel best), or run the System-One classifier on a task file.',
              'score (set <model> <category>[.<facet>] <level> [--public] | list [<model>] | suggest [<model>] [--threshold N] | clear (<model> [target] | --all) [--public])   |   score --input TASK.json',
              ('input',), ('dry_run', 'score_timeout')),
    'scorer': ('Show, set or test the System-One scorer.', 'scorer [--action show|set|test]', ('action',),
               ('scorer_type', 'api_key', 'base_url', 'model', 'fallback_to_agy', 'objective', 'score_timeout')),
    'record': ('Record a route the user or a hard constraint forced, so the spawn gate accepts it.',
               'record --constraint user_override --provider P --account A --model M --effort E --basis "<user quote>"',
               ('constraint', 'provider', 'account', 'model', 'effort', 'basis'),
               ('input', 'trigger', 'reason_category', 'specialty', 'fast', 'dry_run', 'parent_id', 'usage_cache', 'accounts')),
    'override': ('Add, list or clear a temporary routing boost (reset or subscription end coming soon).',
                 'override --list   |   override --account A --basis "<user quote>" [--resets-in H | --expires-in H]',
                 ('list', 'clear', 'account', 'model', 'basis'),
                 ('intent', 'window', 'resets_in', 'resets_at', 'expires_in', 'expires_at', 'boost', 'until', 'stop_on_reset',
                  'reset_confirmation', 'rebaseline', 'usage_cache')),
    'dispatch': ('Drive one dispatch through its lifecycle (prepare, start, status, reconcile, release, fallback, settle).',
                 'dispatch --dispatch-action status --dispatch-id ID', ('dispatch_action', 'dispatch_id'),
                 ('input', 'parent_id', 'basis', 'usage_cache')),
    'launch': ('Start a prepared dispatch, or observe it (the continuation a roll hands back), watching for its answer.',
               'launch --dispatch-id ID --parent-id ID', ('dispatch_id', 'parent_id'),
               ('watch_seconds', 'poll_seconds')),
}

# Options a stage reads that its help does not show: placement, addressing and tooling a delegating caller rarely sets.
UNLISTED = {
    'roll': ('access', 'title', 'task_id', 'verbose', 'dry_run', 'no_score', 'score', 'score_timeout', 'usage_cache',
             'reason_category', 'specialty', 'trigger', 'accounts', 'parent_id', 'parent_host', 'parent_address'),
}


def reads(stage):
    """The options `stage` reads: the ones its help shows and the ones it leaves to `--help-all`."""
    summary, usage, basic, extra = STAGES[stage]
    return (*basic, *extra, *UNLISTED.get(stage, ()))


def overview():
    rows = [f'  {stage:<10} {STAGES[stage][0]}' for stage in STAGES]
    return (f'usage: {PROG} <stage> [options]\n\n'
            f'To delegate a task:  {PROG} roll --task "<what the worker should do>"\n'
            f'Options for one stage: {PROG} <stage> -h   (every option: {PROG} <stage> --help-all)\n\n'
            'stages:\n' + '\n'.join(rows) + '\n')


def stage_parser(full, stage):
    """A parser holding only `stage`'s options, required-looking ones first; the actions are the full parser's own."""
    summary, usage, basic, extra = STAGES[stage]
    parser = argparse.ArgumentParser(prog=f'{PROG} {stage}', usage=f'{PROG} {usage}', description=summary, add_help=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    by_dest = {action.dest: action for action in full._actions}
    for title, dests in ((f'what {stage} needs', basic), ('optional, only when it applies', extra)):
        group = parser.add_argument_group(title) if dests else None
        for dest in dests:
            if dest in by_dest:
                group._add_action(by_dest[dest])
    return parser


def stage_help(full, stage):
    return stage_parser(full, stage).format_help() + f'\n(`{PROG} {stage} --help-all` lists every option the script accepts.)\n'
