"""Per-stage help for `agentcompass <stage> -h`: only that stage's options, the few that matter first.

route_judge.py keeps one flat option set (existing automation passes any of them to any stage). This module only
decides what each stage's help shows; `--help-all` still prints every option.
"""
import argparse

PROG = 'agentcompass'

# stage -> (summary, usage line, options the caller normally gives, options that are optional extras)
STAGES = {
    'roll': ('Hand a task to the router: it picks owner, model and effort, launches the worker and says what to do next.',
             'roll (--task TEXT | --task - | --brief FILE)',
             ('task', 'brief'),
             ('hours', 'local_only', 'keep_whole', 'file', 'apply_to', 'fast', 'verbose', 'wait', 'title', 'workspace', 'task_id', 'dry_run',
              'provider', 'account', 'model', 'effort', 'basis', 'parent_host', 'parent_id', 'parent_address', 'usage_cache')),
    'plan': ('Rank the signed-in model x effort bundles for a task file without launching anything (write no ledger row with --dry-run).',
             'plan --input TASK.json [--dry-run]   |   plan --demo --dry-run',
             ('input',),
             ('dry_run', 'demo', 'format', 'hours', 'fast', 'keep_whole', 'score', 'no_score', 'score_timeout', 'usage_cache')),
    'providers': ('Refresh and print the provider inventory: which accounts, CLIs and models this machine can launch.',
                  'providers', (), ()),
    'usage': ('Print the merged provider usage windows the router would route on (read only).',
              'usage --show [--accounts KEY ...]', ('show',), ('accounts', 'usage_cache')),
    'score': ('Run only the System-One classifier on a task file.', 'score --input TASK.json', ('input',), ('dry_run', 'score_timeout')),
    'scorer': ('Show, set or test the System-One scorer.', 'scorer [--action show|set|test]', ('action',),
               ('scorer_type', 'api_key', 'base_url', 'model', 'fallback_to_agy', 'objective', 'score_timeout')),
    'delegate': ('Ask two low-effort judges whether to do the work locally or delegate it.', 'delegate --input TASK.json', ('input',),
                 ('dry_run', 'judge', 'timeout', 'usage_cache')),
    'select': ('Run plan, then low-effort judges only when the top two bundles tie.', 'select --input TASK.json', ('input',),
               ('dry_run', 'judge', 'timeout', 'usage_cache')),
    'record': ('Record a route the user or a hard constraint forced, so the spawn gate accepts it.',
               'record --constraint user_override --provider P --account A --model M --effort E --basis "<user quote>"',
               ('constraint', 'provider', 'account', 'model', 'effort', 'basis'),
               ('reason_category', 'specialty', 'hours', 'fast', 'dry_run', 'usage_cache')),
    'override': ('Add, list or clear a temporary routing boost (reset or subscription end coming soon).',
                 'override --list   |   override --provider P --account A --basis "<user quote>" [--resets-in H | --expires-in H]',
                 ('list', 'clear', 'provider', 'account', 'model', 'basis'),
                 ('intent', 'window', 'resets_in', 'resets_at', 'expires_in', 'expires_at', 'boost', 'until', 'stop_on_reset',
                  'reset_confirmation', 'rebaseline', 'specialty', 'reason_category', 'usage_cache')),
    'dispatch': ('Drive one dispatch through its lifecycle (prepare, start, status, reconcile, release).',
                 'dispatch --dispatch-action status --dispatch-id ID', ('dispatch_action', 'dispatch_id'),
                 ('input', 'parent_id', 'watch_seconds', 'poll_seconds', 'basis', 'usage_cache')),
    'launch': ('Prepare and start one dispatch from a plan file.', 'launch --input PLAN.json', ('input',),
               ('parent_id', 'watch_seconds', 'poll_seconds', 'basis', 'usage_cache')),
}


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
