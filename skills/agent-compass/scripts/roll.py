"""roll: one command from a written task brief to a routed, launched worker.

The classifier reads the original brief; the worker receives it unchanged plus the communication contract.
Classification, allocation, the ledger record, dispatch preparation and launch happen here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import dispatch_fallback
import dispatch_flow
import entry
import dispatch_registry as registry
import host_placement
import provider_inventory
import worker_context
import worker_sessions
from worker_context import NATIVE_HOSTS, harness_session, require_parent_id, resolve_parent

# Launcher families a configured Paseo provider may use: the dispatch adapter's own table, so a ready Grok/ACP
# definition is launched by Paseo instead of falling through to the CLI.
PASEO_LAUNCHER_FAMILIES = tuple(registry.PASEO_LAUNCHER_MODES)
SKILLS = Path(__file__).resolve().parents[2]
DOT_RUNNER = SKILLS / 'dot-subagent' / 'scripts' / 'run_dot_task.py'
CONSULT_BRIDGE = SKILLS / 'chatgpt-pro-consult' / 'scripts' / 'consult_bridge.py'
STATE_DIR = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass').expanduser()
CLI_RUNS_DIR = STATE_DIR / 'cli-workers'
WORKER_PROMPTS_DIR = STATE_DIR / 'worker-prompts'
RESULTS_DIR = STATE_DIR / 'roll-results'
WORKER_COMMUNICATION = SKILLS / 'agent-compass' / 'references' / 'worker-communication.md'
# Without Paseo, each CLI family runs one routed bundle unattended with full tool access, as Paseo would launch it:
# executable and flags for (model, effort, cwd). agy takes its prompt only as the argument after --print; the
# others read it from stdin; grok reads neither (headless ignores stdin) and takes --prompt-file.
CLI_RUNS = {
    'claude': ('claude', lambda model, effort, cwd, fast=False: ['-p', '--model', model, *(['--effort', effort] if effort else []),
                                                                 '--permission-mode', 'bypassPermissions',
                                                                 '--settings', json.dumps({'fastMode': bool(fast)})]),
    'codex': ('codex', lambda model, effort, cwd, fast=False: ['exec', '-m', model,
                                                               *(['-c', f'model_reasoning_effort="{effort}"'] if effort else []),
                                                               '-c', 'features.fast_mode=' + str(bool(fast)).lower(),
                                                               '-c', 'service_tier="priority"' if fast else 'service_tier="default"',
                                                               '--dangerously-bypass-approvals-and-sandbox',
                                                               '--skip-git-repo-check', '-C', cwd, '-']),
    'pi': ('pi', lambda model, effort, cwd, fast=False: ['-p', '--model', model if '/' in model else f'openai-codex/{model}', *(['--thinking', effort] if effort else [])]),
    'antigravity-resilient': ('agy', lambda model, effort, cwd, fast=False: ['--dangerously-skip-permissions', '--model', model,
                                                                             '--add-dir', cwd, '--print']),
    'grok': ('grok', lambda model, effort, cwd, fast=False: ['--always-approve', '-m', model,
                                                           *(['--reasoning-effort', effort] if effort else []), '--cwd', cwd]),
    'opencode': ('opencode', lambda model, effort, cwd, fast=False: [
        'run', '-m', model if '/' in model else f'opencode/{model}',
        *(['--variant', effort] if effort else []), '--dir', cwd]),
}
PROMPT_ARGUMENT = ('antigravity-resilient', 'opencode')
PROMPT_FILE = ('grok',)
# The caller's own session and subscription; a CLI worker gets its provider's, never these.
# The parent's session identity never reaches a CLI worker: the worker's own host sets its own.
HOST_SESSION_ENV = ('CLAUDE', 'CODEX_', 'PASEO_AGENT', *worker_context.OWN_SESSION_ID_VARS)


def title_of(text):
    for line in text.splitlines():
        line = line.strip().lstrip('#').strip()
        if line:
            return line[:60]
    return 'task'


def read_task_text(args):
    """The assignment exactly as given: --brief file, --task text, or --task - for stdin (no command-line length limit)."""
    if args.brief is not None:
        return args.brief.read_bytes().decode('utf-8-sig')  # bytes, so CRLF is not rewritten
    if args.task == '-':
        return sys.stdin.buffer.read().decode('utf-8-sig')
    return args.task


def build_task(args, text, parent):
    # Same brief, same parent -> same task_id, so a repeated roll cannot open a second live dispatch.
    hours = getattr(args, 'hours', None)
    if hours is not None and not (isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0 and hours != float('inf')):
        raise ValueError('--hours must be a positive number of expected active hours.')
    task = {'objective': text,  # the classifier and the worker both get the whole text
            'task_id': args.task_id or 'roll-' + hashlib.sha256(text.encode()).hexdigest()[:16]}
    if hours is None:
        # No extra model call: the allocator budgets the classifier's own expected_hours.
        task['hours_source'] = 'classifier_estimate'
    else:
        task['expected_hours'] = float(hours)
    if parent:
        task['parent_agent_id'] = parent
    if getattr(args, 'brief', None) is not None:
        try:
            task['brief_path'] = str(Path(args.brief).resolve())
        except OSError:
            task['brief_path'] = str(args.brief)
    if getattr(args, 'local_only', None) is not None:
        local_only_reason = str(args.local_only).strip()
        if not local_only_reason:
            raise ValueError('--local-only requires a non-empty reason.')
        task['needs'] = {'local_only': True, 'local_only_reason': local_only_reason}
    elif getattr(args, 'workspace', None):
        ws = host_placement.canonical_dir(args.workspace)
        if ws:
            task['needs'] = {'local_only': True, 'local_only_reason': f'inferred from --workspace {ws}'}
    if getattr(args, 'file', None) or getattr(args, 'apply_to', None):
        task.setdefault('needs', {})['file_transfer'] = True
    if getattr(args, 'fast', False):
        task.setdefault('needs', {})['fast'] = True
    can_wait = getattr(args, 'can_wait_hours', None)
    if can_wait is not None:
        if not (isinstance(can_wait, (int, float)) and not isinstance(can_wait, bool) and can_wait >= 0
                and can_wait != float('inf') and can_wait == can_wait):
            raise ValueError('--can-wait-hours must be a non-negative number of hours.')
        # One flag, two readers: the allocator's limit on a later start (five-axis wait logic), and how long a transient
        # provider failure may be waited out before AgentCompass relaunches the brief on the next candidate
        # (dispatch_fallback).
        task['can_wait_hours'] = float(can_wait)
    return task


def hours_assumption(args, decision):
    """Where the budgeted hours came from, in words that are true for every case; never a figure it does not have.
    None when the caller gave --hours: their own figure is not an assumption."""
    if getattr(args, 'hours', None) is not None:
        return None
    est = decision.get('hours_estimate') or {}
    origin = {'classifier_estimate': 'the classifier estimate',
              'caller_profile': "the caller's task profile (its expected_hours)"}.get(est.get('source'))
    if origin and isinstance(est.get('hours'), (int, float)) and not isinstance(est.get('hours'), bool):
        confidence = est.get('confidence') if est.get('confidence') not in (None, '') else 'not stated'
        return (f"--hours not given: budgeted {est['hours']:g}h from {origin}, confidence {confidence}; "
                'an estimate, not a measurement. Pass --hours for a large or unusually small task.')
    return ('--hours not given and no classifier estimate: no hours were budgeted, and none were invented. '
            'Pass --hours, or roll again when the classifier answers.')


def is_readonly_review_profile(profile):
    """True when the classified profile describes a read-only review or audit that writes no repo code."""
    if not isinstance(profile, dict):
        return False
    if 'read_only_review' in profile:
        return bool(profile.get('read_only_review'))
    return bool(profile.get('work_kind') == 'debug_review' and profile.get('code_complexity') == 0)


def independent_reviewers(workers):
    """Workers that may take a review: only those that have never written code in their history. A worker that
    implemented or fixed the work would be reviewing itself, whatever brief overlap or chain points at it."""
    out = []
    for w in workers:
        profiles = w.get('profiles') or ([w['profile']] if isinstance(w.get('profile'), dict) else [])
        if profiles and not all(is_readonly_review_profile(p) for p in profiles):
            continue
        if isinstance(w.get('read_only_review'), bool):
            if w['read_only_review'] is True:
                out.append(w)
        elif profiles:
            out.append(w)
    return out


def match_reusable_worker(args, task, text=None, parent=None, workers=None):
    """Select an idle Paseo worker of `parent` only for structured continuation:
    (1) explicit `--resume <agent_id>`, (2) exact `task_id` or `brief_path` equality, or (3) explicit `task['chain']`."""
    del text
    explicit_id = (getattr(args, 'resume', None) or '').strip()
    if explicit_id and getattr(args, 'new_worker', False):
        raise ValueError('Cannot combine --resume and --new-worker.')
    if not parent or getattr(args, 'new_worker', False):
        return None
    excluded = set(getattr(args, '_exclude_agents', None) or ())
    workers = [w for w in (registry.reusable_workers(parent) if workers is None else workers)
               if w.get('agent_id') not in excluded]
    if explicit_id:
        matched = next((w for w in workers if w.get('agent_id') == explicit_id), None)
        if matched is None:
            raise ValueError(f'Worker {explicit_id!r} is not an idle, reusable worker of parent {parent!r}.')
        return matched
    if not workers or not isinstance(task, dict):
        return None
    raw_chain = task.get('chain')
    chain = raw_chain if isinstance(raw_chain, dict) else None
    chain_agent_id = ''
    chain_id = (task.get('chain_id') or (raw_chain if isinstance(raw_chain, str) else '') or '').strip()
    if chain:
        chain_worker = chain.get('worker') if isinstance(chain.get('worker'), dict) else {}
        chain_agent_id = (chain_worker.get('agent_id') or '').strip()
        chain_id = (chain.get('id') or chain_id or '').strip()
    if chain_agent_id:
        matched = next((w for w in workers if w.get('agent_id') == chain_agent_id), None)
        if matched is not None:
            return matched
    if chain_id:
        eligible = [((not w.get('worn', False), w.get('updated_at') or ''), w)
                    for w in workers
                    if w.get('chain_id') == chain_id or chain_id in (w.get('chain_ids') or ())]
        if eligible:
            return max(eligible, key=lambda item: item[0])[1]
    task_id = task.get('task_id')
    brief_path = task.get('brief_path')
    if not brief_path and not getattr(args, 'task_id', None) and not task_id:
        return None
    eligible = []
    for w in workers:
        if (task_id and task_id in (w.get('task_ids') or ())) or (brief_path and brief_path in (w.get('brief_paths') or ())):
            eligible.append(((not w.get('worn', False), w.get('updated_at') or ''), w))
    if not eligible:
        return None
    return max(eligible, key=lambda item: item[0])[1]


def _bundle_matches_worker(bundle, worker, same_model=None):
    """`same_model` compares resolved keys, so one model under two spellings is still this worker's."""
    if not isinstance(bundle, dict) or not isinstance(worker, dict) or worker.get('worn'):
        return False
    w_bundle = worker.get('bundle') or {}
    if bool(bundle.get('fast')) != bool(w_bundle.get('fast')):
        return False
    b_id = bundle.get('bundle_id') or bundle.get('id')
    if b_id and worker.get('bundle_id') == b_id and not str(b_id).startswith('user_override_'):
        return True
    if not all(bundle.get(k) and bundle.get(k) == w_bundle.get(k) for k in ('provider', 'account', 'effort')):
        return False
    theirs, ours = w_bundle.get('model'), bundle.get('model')
    return bool(ours) and (ours == theirs or bool(same_model and theirs and same_model(ours, theirs)))


def model_comparison(fleet):
    """Do two names run the same model? A spelling does, a family guess at its target does not."""
    import account_fleet
    return lambda a, b: account_fleet.row_key(a, fleet) == account_fleet.row_key(b, fleet)


def summary(decision):
    bundle = decision.get('selected_bundle') or {}
    out = {'choice': decision.get('choice'),
           'bundle': {k: bundle[k] for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model', 'fast', 'fast_billing') if bundle.get(k)},
           'reason': ((decision.get('host_policy_violation') or {}).get('reason') or
                      (decision.get('decision') or {}).get('reason') or '')[:300] or None,
           'alerts': [a['message'] for a in decision.get('alerts') or [] if a.get('severity') == 'warn'],
           **({'warnings': warnings} if (warnings := decision.get('warnings') or (decision.get('decision') or {}).get('warnings')) else {})}
    return out


def python(script, *argv):
    return [sys.executable, '-X', 'utf8', str(script), *argv]


_INJECTED_FOOTER_RE = re.compile(
    r'\n+---\n+(?:# Worker(?:-to-parent communication contract| 通信与汇报契约)\b[\s\S]*|'
    r'Parent session agent ID: `[^`\n]+`\.\s*(?:Parent host:[^\n]*\n?)?)\Z'
)


def strip_injected_contract(text):
    """Strip any previously injected worker communication contract/footer so re-rolling or pre-pasted briefs do not duplicate it."""
    if not isinstance(text, str):
        return ''
    cleaned = _INJECTED_FOOTER_RE.sub('', text)
    cleaned = re.sub(
        r'(?:\r?\n)+(?:---\r?\n+)?Parent session agent ID: `[^`\n]+`\.(?:\r?\nParent host: `[^`\n]+`\. Parent message address: `[^`\n]+`\.)?\s*\Z',
        '',
        cleaned,
    )
    return cleaned


def recipient_footer(parent, host='paseo', address=None):
    parent = require_parent_id(parent)
    return (f'Parent session agent ID: `{parent}`.\n'
            f'Parent host: `{host}`. Parent message address: `{address or parent}`.')


def prepare_worker_prompt(text, parent, host='paseo', address=None):
    """One contract for every launch surface; retain the source brief and a durable native-fallback prompt."""
    base = strip_injected_contract(text)
    recipient = recipient_footer(parent, host, address)
    prompt = base + '\n\n---\n\n' + WORKER_COMMUNICATION.read_text(encoding='utf-8') + '\n' + recipient + '\n'
    if host == 'herdr':
        prompt += ('\nHerdr core return channel: send questions and [Worker final report] with the argument vector '
                   f'`hc send {parent} <message>`; check the send result. Finish this turn after reporting. '
                   'Your final reply alone is not a parent message. Do not use Paseo or user-dialog tools.\n')
    WORKER_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKER_PROMPTS_DIR / f'{uuid.uuid4()}.md'
    path.write_bytes(prompt.encode('utf-8'))  # no newline translation of the caller's text
    return prompt, path


def prepare_resume_prompt(text, parent, host='paseo', address=None):
    """Resumed turns already received worker-communication.md in turn 1; send only the brief and parent address footer."""
    base = strip_injected_contract(text)
    recipient = recipient_footer(parent, host, address)
    return base + '\n\n---\n\n' + recipient + '\n'


def dot(args, out, prompt_file, *, task=None, decision=None):
    files = [str(f) for f in args.file]
    target_cwd = str(args.apply_to or Path.cwd()) if files else None
    apply_to = ['--apply-to', target_cwd] if target_cwd else []
    command = python(DOT_RUNNER, 'run', '--prompt-file', str(prompt_file),
                     *[x for f in files for x in ('--file', f)], *apply_to, '--no-wait')
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=180)
    found = re.search(r'task_id=(\S+)', proc.stderr)
    launched = bool(found) or proc.returncode == 0
    if launched and found and isinstance(decision, dict) and decision.get('decision_id'):
        try:
            registry.record_external_launch(
                'dot',
                found.group(1),
                decision['decision_id'],
                decision.get('selected_bundle') or {'provider': 'dot', 'account': 'dot', 'model': 'dot'},
                parent_id=out.get('parent_id'),
                task_id=(task or {}).get('task_id'),
                profile=decision.get('task_profile') or (task or {}).get('profile'),
                brief_path=(task or {}).get('brief_path'),
                cwd=target_cwd,
            )
        except OSError:
            pass
    result = dict(out, launched=launched, dispatch_mode='dot',
                  caller_action='already_launched_do_not_respawn' if launched else 'check_error',
                  worker={'provider': 'dot', 'task_id': found.group(1) if found else None, 'exit': proc.returncode,
                          'reply': None, 'stderr': proc.stderr[-1500:] or None})
    if launched:
        result['next'] = ('Worker is ALREADY launched on Dot by roll. Do NOT call create_agent, Agent, '
                          'spawn_agent, or invoke_subagent; use continuation_argv or read worker.reply.')
    if found:
        result['continuation_argv'] = entry.argv('dot', 'status', '--task-id', found.group(1), '--wait', '120', *apply_to)
    return result, 0 if proc.returncode in (0, 3) else 1


def consult(args, out, prompt_file):
    base = args.brief if args.brief is not None else prompt_file
    target = base.with_name(f'{base.stem}-consult-{time.strftime("%Y%m%d-%H%M%S")}')
    given = ['--prompt', str(prompt_file), '--out', str(target)]
    command = entry.argv('chatgpt-web', *given)
    # A browser consultation blocks for up to 30 minutes; hand over the one command instead of backgrounding it.
    return dict(out, launched=False, dispatch_mode='chatgpt-web', caller_action='run_next_argv', next_argv=command,
                next='Run next_argv (blocks until the answer is in <out>/response.md).'), 0


def grok_bot(args, out, prompt_file, task, bundle, *, decision=None):
    """Same external-runner receipt/continuation surface as Dot; no Paseo adapter."""
    import run_grok_bot as runner
    if args.file or args.apply_to or args.local_only or getattr(args, 'workspace', None):
        raise ValueError('grok-bot runs remotely and accepts text only; local-only/file-transfer options are unsupported.')
    if bundle.get('model') != 'grok-bot' or bundle.get('account') != 'grok-bot' or bundle.get('effort'):
        raise ValueError('Use --provider grok-bot --account grok-bot --model grok-bot without an effort; upstream owns model selection.')
    try:
        receipt = runner.submit(prompt_file.read_text(encoding='utf-8'), out['parent_id'], task['task_id'])
    except (runner.TransportError, OSError, ValueError) as exc:
        return dict(out, launched=False, dispatch_mode='grok-bot', caller_action='check_error', error=str(exc)), 1
    delivery = receipt['delivery']
    completed = receipt['state'] == 'completed'
    if receipt.get('task_id') and receipt.get('state') != 'failed' and isinstance(decision, dict) and decision.get('decision_id'):
        try:
            registry.record_external_launch(
                'grok-bot',
                receipt['task_id'],
                decision['decision_id'],
                bundle,
                parent_id=out.get('parent_id'),
                task_id=task.get('task_id'),
                profile=decision.get('task_profile') or task.get('profile'),
                brief_path=task.get('brief_path'),
                cwd=None,
                completed=completed,
            )
        except OSError:
            pass
    result = dict(out, launched=True if completed or delivery == 'accepted' else False if delivery == 'rejected' else None,
                  dispatch_mode='grok-bot', completed=completed, worker=receipt,
                  caller_action='completed' if completed else 'already_launched_do_not_respawn' if delivery == 'accepted' else
                  'check_delivery_do_not_resend' if delivery == 'unknown' else 'check_error')
    if receipt['state'] not in ('completed', 'failed'):
        result['continuation_argv'] = entry.argv('grok-bot', 'status', '--task-id', receipt['task_id'], '--wait', '30')
        result['next'] = 'Read continuation_argv; never repeat an uncertain send. Relay needs_input to the parent; resolve cards in the official app.'
    return result, 1 if receipt['state'] == 'failed' else 0


def task_access(args, read_only_review=None):
    """--access wins, then the classifier's access / read_only_review, and anything else writes: a wrong block is loud
    and has a way out, a wrong exemption silently lets a second writer in. interaction_style says nothing about writing."""
    explicit = getattr(args, 'access', None)
    if explicit:
        return 'read_only' if explicit == 'read-only' else 'write'
    if isinstance(read_only_review, dict):
        classified = read_only_review.get('access')
        if classified in ('read-only', 'read_only'):
            return 'read_only'
        if classified == 'write':
            return 'write'
        read_only_review = read_only_review.get('read_only_review')
    if read_only_review in ('read-only', 'read_only'):
        return 'read_only'
    if read_only_review == 'write':
        return 'write'
    return 'read_only' if read_only_review is True else 'write'


def parent_cwd():
    # Paseo starts a worker without --workspace in its parent's cwd, which Paseo gives its agents here.
    return os.environ.get('PASEO_AGENT_CWD') or os.getcwd()


def same_cwd_warning(out, cwd, parent):
    """Without `--workspace`, parallel workers commonly start in one checkout and create their own worktrees, so
    other unsettled writers in `cwd` are reported as a warning and block nothing."""
    others = registry.writers_at(cwd, caller=parent)
    if not others:
        return out
    named = '; '.join(f"{h.get('state')} dispatch {h.get('dispatch_id')} ({h.get('title')})" for h in others)
    note = f'{cwd} already has unsettled writer dispatches and this worker will also write there: {named}.'
    return dict(out, assumptions=[*out.get('assumptions', []), note], warnings=[*out.get('warnings', []), note])


def workspace_blocked(out, workspace, holders, dispatch_mode='paseo'):
    named = '; '.join(f"{h.get('state')} dispatch {h.get('dispatch_id')} (agent {h.get('agent_id') or 'not created yet'})" for h in holders)
    return dict(out, launched=False, dispatch_mode=dispatch_mode, caller_action='resolve_workspace_writer',
                error='workspace_has_active_writer', blocked_by=holders,
                next=f'Nothing was launched: workspace {workspace} already has a writer: {named}. Wait for it to finish, then roll '
                     'this brief again, or create a separate worktree for parallel work. If this brief only reads, roll it with --access read-only.'), 2


def placement_refused(out, dispatch_mode, placed):
    return dict(out, launched=False, dispatch_mode=dispatch_mode, caller_action=placed['caller_action'],
                error=placed['error'], next=placed['next']), 1


def paseo(args, out, decision, task, text, parent, usage, judge=None):
    dispatch_flow.reconcile()
    decision_profile = (
        decision.get('task_profile')
        or ((decision.get('decision') or {}).get('task') or {}).get('profile')
        or task.get('profile')
    )
    is_ro_review = is_readonly_review_profile(decision_profile)
    access = task_access(args, decision_profile if isinstance(decision_profile, dict) else None)
    workspace = host_placement.canonical_dir(getattr(args, 'workspace', None))
    probe = None
    if workspace:
        if access == 'write':
            holders, unavailable, probe = dispatch_flow.workspace_holders(workspace, parent)
            if probe and probe.get('placeable') is False:
                return placement_refused(out, 'paseo', host_placement.place('paseo', workspace, paseo_probe=probe))
            if unavailable:
                out = dict(out, assumptions=[*out.get('assumptions', []),
                                             f'Running agents in workspace {workspace} could not be read ({unavailable}); only recorded dispatches were checked.'])
            if holders:
                return workspace_blocked(out, workspace, holders, dispatch_mode='paseo')
        else:
            probe = dispatch_flow.paseo_placement_probe(workspace, parent)
            if probe and probe.get('placeable') is False:
                return placement_refused(out, 'paseo', host_placement.place('paseo', workspace, paseo_probe=probe))
    resume_agent_id = None
    explicit_id = (getattr(args, 'resume', None) or '').strip()
    if explicit_id and getattr(args, 'new_worker', False):
        raise ValueError('Cannot combine --resume and --new-worker.')
    excluded = set(getattr(args, '_exclude_agents', None) or ())
    all_reusable = [w for w in registry.reusable_workers(parent) if w.get('agent_id') not in excluded]
    import account_fleet
    same_model = model_comparison(account_fleet.load_accounts())
    if explicit_id:
        explicit_worker = next((w for w in all_reusable if w.get('agent_id') == explicit_id), None)
        if explicit_worker is None:
            raise ValueError(f'Worker {explicit_id!r} is not an idle, reusable worker of parent {parent!r}.')
        if is_ro_review and not independent_reviewers([explicit_worker]):
            raise ValueError(
                f'Worker {explicit_id!r} previously wrote or modified code and cannot be resumed for an independent '
                'read-only review; omit --resume to start a fresh reviewer.'
            )
        if explicit_worker.get('worn') or (decision.get('decision') or {}).get('action') == 'rotate':
            raise ValueError(
                f'Worker {explicit_id!r} reached the context rotation limit and cannot be resumed; '
                'omit --resume to start a fresh worker.'
            )
        selected_bundle = decision.get('selected_bundle') or {}
        if not _bundle_matches_worker(selected_bundle, explicit_worker, same_model):
            raise ValueError(
                f'Worker {explicit_id!r} uses bundle {explicit_worker.get("bundle_id")!r}, which does not match '
                f'routed bundle {(selected_bundle.get("bundle_id") or selected_bundle.get("id"))!r}; cannot resume it.'
            )
        resume_agent_id = explicit_id
    elif not getattr(args, 'new_worker', False) and (decision.get('decision') or {}).get('action') != 'rotate':
        selected_bundle = decision.get('selected_bundle') or {}
        compatible = [w for w in all_reusable if _bundle_matches_worker(selected_bundle, w, same_model)]
        if is_ro_review:
            compatible = independent_reviewers(compatible)
        chain_worker_id = (((task.get('chain') or {}).get('worker') or {}).get('agent_id')
                           if isinstance(task.get('chain'), dict) else None)
        if chain_worker_id and any(w['agent_id'] == chain_worker_id for w in compatible):
            resume_agent_id = chain_worker_id
        elif compatible:
            matched = match_reusable_worker(args, task, text, parent, workers=compatible)
            if matched:
                resume_agent_id = matched['agent_id']
    # A resumed worker without --workspace stays where it lives, whatever the parent's directory.
    default_cwd = (resume_agent_id and (registry.paseo_agent_record(resume_agent_id) or {}).get('cwd')) or parent_cwd()
    placed = host_placement.place('paseo', workspace, default_cwd=default_cwd, paseo_probe=probe)
    if not placed['ok']:
        return placement_refused(out, 'paseo', placed)
    landing = placed['cwd']
    if not workspace and landing and access == 'write':
        out = same_cwd_warning(out, landing, parent)
    prompt_for_launch = (prepare_resume_prompt(text, parent, out.get('parent_host') or 'paseo', out.get('parent_address'))
                         if resume_agent_id else text)
    launch = {'title': args.title or title_of(text), 'initialPrompt': prompt_for_launch, **placed['launch']}
    try:
        row = registry.prepare(decision['decision_id'], task, launch, usage,
                               resume_agent_id=resume_agent_id, fallback_prompt=text if resume_agent_id else None,
                               access=access,
                               new_worker=bool(getattr(args, 'new_worker', False)),
                               workspace=workspace, cwd=landing)
    except registry.WorkspaceBusy as busy:
        return workspace_blocked(out, busy.workspace, busy.holders, dispatch_mode='paseo')
    except registry.CapacityChanged as refused:
        if not dispatch_fallback.enabled() or judge is None:
            raise
        # The routed bundle could no longer start (e.g. its live model list dropped it): the next candidate does.
        fresh = dict(launch, initialPrompt=text)
        outcome = dispatch_fallback.after_refusal(decision['decision_id'], fresh, refused, access=access,
                                                  workspace=workspace, cwd=landing, judge=judge)
        return fallback_result(out, outcome, decision, parent, args)
    receipt = dispatch_flow.launch(row['dispatch_id'], parent, watch_seconds=0)
    if (receipt.get('state') == 'create_failed' or (receipt.get('state') == 'failed' and receipt.get('agent_id'))) \
            and dispatch_fallback.enabled() and judge is not None:
        # Paseo confirms no worker runs this brief any more (none was created, or it ended in error while roll watched):
        # AgentCompass retries or relaunches it on the next candidate instead of asking the parent. The bridge's watcher
        # may see the same failure; whichever claims it first relaunches and the other reads that outcome.
        return fallback_result(out, dispatch_fallback.handle(row['dispatch_id'], judge=judge), decision, parent, args)
    if (
        receipt.get('state') == 'failed'
        and receipt.get('error') == 'parent_agent_not_found'
        and not os.environ.get('PASEO_AGENT_ID', '').strip()
        and judge is not None
    ):
        try:
            claim_row = registry.record_host_claim(
                'cli', decision, task, decision['selected_bundle'],
                label=args.title or title_of(text),
                usage=usage, access=access, workspace=workspace, cwd=landing, launched=True,
            )
        except registry.WorkspaceBusy as busy:
            return workspace_blocked(out, busy.workspace, busy.holders, dispatch_mode='cli')
        res, code = cli(args, out, decision['selected_bundle'], text, judge)
        if not res.get('launched'):
            registry.settle_host_claim(claim_row['dispatch_id'], 'canceled', 'cli_not_launched')
        return res, code
    failed = receipt.get('state') in ('failed', 'create_failed')
    # launched_at records the single-use claim, before create is sent. Only a
    # validated host observation binds agent_id/observed_at to this dispatch.
    confirmed = bool(receipt.get('agent_id') and receipt.get('observed_at'))
    ended_before_send = (receipt.get('state') in ('canceled', 'expired') and not receipt.get('launched_at'))
    removed = receipt.get('state') == 'canceled' and bool(receipt.get('next_action'))
    uncertain = not failed and not confirmed and not ended_before_send and not removed
    if uncertain:
        receipt = dict(receipt)
        receipt.setdefault('continuation_argv', entry.argv(
            'launch', '--dispatch-id', row['dispatch_id'], '--parent-id', parent, '--watch-seconds', '120'))
        return dict(out, launched=None, dispatch_mode='paseo', caller_action='check_delivery_do_not_resend',
                    next=receipt.get('next_action') or
                         'Paseo delivery is unconfirmed. Run worker.continuation_argv to observe this same dispatch; '
                         'do not roll again or issue another create request.',
                    worker=dict(receipt, provider=decision['selected_bundle']['provider'])), 0
    if ended_before_send:
        return dict(out, launched=False, dispatch_mode='paseo', caller_action='check_error',
                    next=f"Dispatch {receipt['state']} before a launch was sent; inspect its receipt before scheduling new work.",
                    worker=dict(receipt, provider=decision['selected_bundle']['provider'])), 2
    if removed:
        return dict(out, launched=False, dispatch_mode='paseo', caller_action='check_error', next=receipt['next_action'],
                    worker=dict(receipt, provider=decision['selected_bundle']['provider'])), 2
    if receipt.get('state') == 'create_failed':
        action = 'check_error' if receipt.get('next_action') == dispatch_flow.REQUEST_CREATE_FAILED_NEXT else 'roll_again_safe'
        return dict(out, launched=False, dispatch_mode='paseo', caller_action=action, next=receipt['next_action'],
                    worker=dict(receipt, provider=decision['selected_bundle']['provider'])), 1
    notification = receipt.get('completion_notification') or {}
    notice = ('A completion notification will be sent to the parent on finish/error/permission; '
              'the bounded watcher PID, deadline and durable receipt are in worker.completion_notification. '
              'Use worker.continuation_argv to recover observation if that watcher stops.') \
        if notification.get('status') == 'watching' else \
        ('The completion notification has been delivered to the parent.' if notification.get('status') == 'delivered' else
         'No active completion notification is confirmed. The caller must collect the result using '
         'worker.continuation_argv or get_agent_activity; do not wait for an unsolicited callback.')
    return dict(out, launched=not failed, dispatch_mode='paseo',
                caller_action='already_launched_do_not_respawn' if not failed else 'check_error',
                next=('Worker is ALREADY launched in Paseo by roll. Do NOT call create_agent, Agent, '
                      'spawn_agent, or invoke_subagent again. ' + notice) if not failed else
                     ('Paseo launch failed: ' + (receipt.get('error') or 'worker failed') + '. ' +
                      ('Repair the local notifier dependencies/state directory, then roll the brief again.'
                       if receipt.get('error') == 'notification_start_failed' else receipt.get('next_action') or notice)),
                worker=dict(receipt, provider=decision['selected_bundle']['provider'])), \
        1 if failed else 0


def fallback_result(out, outcome, decision, parent, args):
    """roll's answer once dispatch_fallback has relaunched the brief or found nothing left to start it on."""
    attempts = outcome.get('lines') or []
    if outcome.get('action') == 'relaunched':
        dispatch_id = outcome['dispatch_id']
        receipt = dispatch_flow.public_chain(registry.inspect(dispatch_id))
        receipt.update({k: v for k, v in (outcome.get('receipt') or {}).items()
                        if k in ('completion_notification', 'error', 'cause', 'observe_error')})
        bundle = registry.inspect(dispatch_id)['bundle']
        route = dict(out.get('route') or {}, bundle={k: bundle[k] for k in (
            'provider', 'harness', 'account', 'model', 'effort', 'modeId', 'launch_model', 'fast') if bundle.get(k)})
        out = dict(out, route=route, attempts=attempts)
        worker = dict(receipt, provider=bundle['provider'], attempts=attempts)
        if receipt.get('state') in ('launching', 'expired') and not receipt.get('agent_id'):
            worker.setdefault('continuation_argv', entry.argv(
                'launch', '--dispatch-id', dispatch_id, '--parent-id', parent, '--watch-seconds', '120'))
            return dict(out, launched=None, dispatch_mode='paseo', caller_action='check_delivery_do_not_resend',
                        next=receipt.get('next_action') or 'Paseo delivery of the relaunch is unconfirmed. Run '
                             'worker.continuation_argv to observe it; do not roll again.', worker=worker), 0
        notification = receipt.get('completion_notification') or {}
        return dict(out, launched=True, dispatch_mode='paseo', caller_action='already_launched_do_not_respawn',
                    next=('The routed model failed to start, so AgentCompass relaunched the brief on the next candidate '
                          f'({bundle.get("model")} on {bundle.get("account")}). Do NOT call create_agent, Agent, '
                          'spawn_agent, or invoke_subagent again. ' +
                          ('A completion notification will be sent to the parent.' if notification.get('status') == 'watching'
                           else 'No completion notification is confirmed: run worker.continuation_argv to collect the result.')),
                    worker=worker), 0
    if outcome.get('action') == 'final' and outcome.get('result') in ('blocked', 'owner_named'):
        return dict(out, launched=False, dispatch_mode='paseo', caller_action='tell_user', error='no_candidate_left'
                    if outcome['result'] == 'blocked' else 'owner_named_model_failed', attempts=attempts,
                    next=outcome.get('text'),
                    worker={'provider': (decision.get('selected_bundle') or {}).get('provider'),
                            'dispatch_id': outcome.get('root_dispatch_id'), 'attempts': attempts,
                            'next_action': outcome.get('text')}), 2
    if outcome.get('action') in ('pending', 'scheduled'):
        # pending: the bridge's watcher claimed this failure first and is relaunching the brief. scheduled: a transient
        # failure whose same-model retry starts at retry_at, from the watcher or the next observation.
        root = outcome.get('root_dispatch_id')
        scheduled = outcome['action'] == 'scheduled'
        text = (dispatch_flow.FALLBACK_SCHEDULED_NEXT.format(retry_at=outcome.get('retry_at')) if scheduled
                else dispatch_flow.FALLBACK_RUNNING_NEXT.format(where='its watcher is starting the next attempt'))
        return dict(out, launched=None, dispatch_mode='paseo', caller_action='check_delivery_do_not_resend',
                    attempts=attempts, **({'retry_at': outcome.get('retry_at')} if scheduled else {}), next=text,
                    worker={'dispatch_id': root, 'attempts': attempts, 'next_action': text,
                            **({'retry_at': outcome.get('retry_at')} if scheduled else {}),
                            'continuation_argv': entry.argv(
                                'launch', '--dispatch-id', root, '--parent-id', parent, '--watch-seconds', '120')}), 0
    # Declined (not a failure AgentCompass relaunches) or unsettled: the last attempt's own receipt says what to do.
    last = next((a.get('dispatch_id') for a in reversed(outcome.get('attempts') or []) if a.get('dispatch_id')), None)
    receipt = dispatch_flow.public_chain(registry.inspect(last)) if last else {}
    return dict(out, launched=False, dispatch_mode='paseo', caller_action='check_error', attempts=attempts,
                error=receipt.get('state') or outcome.get('action'),
                next=receipt.get('next_action') or outcome.get('text') or 'The relaunch could not be prepared; tell the user.',
                worker=dict(receipt, provider=(decision.get('selected_bundle') or {}).get('provider'),
                            attempts=attempts)), 1


def _router_override_proxy_spec(provider):
    state_dir = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass')
    override_env = os.environ.get('AGENTCOMPASS_ACCOUNTS_OVERRIDE')
    override_path = Path(override_env).expanduser() if override_env else state_dir / 'accounts-override.json'
    import accounts_override
    doc = accounts_override.load_override(override_path) or {}
    for section in ('litellm_proxies', 'accounts'):
        block = doc.get(section) if isinstance(doc, dict) else None
        if isinstance(block, dict) and isinstance(block.get(provider), dict):
            spec = block[provider]
            bkind = str(spec.get('backend_kind') or spec.get('type') or '').strip().lower()
            sub = spec.get('litellm') if isinstance(spec.get('litellm'), dict) else spec
            ep = str(sub.get('endpoint') or sub.get('base_url') or spec.get('endpoint') or spec.get('base_url') or '').strip()
            key = str(sub.get('api_key') or spec.get('api_key') or sub.get('api_key_env') or spec.get('api_key_env') or '').strip()
            if section == 'litellm_proxies' or bkind in ('litellm', 'proxy', 'api-proxy') or (ep and key):
                return spec
    return None


native_next = host_placement.native_next


def can_launch_via_paseo(provider):
    family = provider_inventory.launcher_family(provider)
    providers = (dispatch_flow.paseo_config().get('agents') or {}).get('providers')
    if provider == family or (isinstance(providers, dict) and provider in providers):
        return family in PASEO_LAUNCHER_FAMILIES
    if _router_override_proxy_spec(provider) is not None:
        return False
    return family in ('claude', 'codex')


def account_home(provider):
    """The home discovery resolved for an extra Claude/Codex account, else the conventional ~/.<provider>."""
    row = (provider_inventory.current().get('accounts') or {}).get(provider) or {}
    return str(row.get('home') or Path.home() / f'.{provider}')


def provider_definition(provider, bundle=None):
    """Paseo's definition of a provider: the CLI family it extends and the env it sets, e.g. a second Claude account
    -> claude with CLAUDE_CONFIG_DIR. An account without a Paseo entry uses its discovered family/home."""
    orig_provider = str(provider or '')
    providers = (dispatch_flow.paseo_config().get('agents') or {}).get('providers')
    providers = providers if isinstance(providers, dict) else {}
    env = {}
    for _ in range(len(providers) + 1):
        spec = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
        env = {**(spec.get('env') or {}), **env}
        if not spec.get('extends'):
            break
        provider = spec['extends']
    if provider not in CLI_RUNS:
        proxy_spec = _router_override_proxy_spec(orig_provider)
        if isinstance(proxy_spec, dict):
            sub = proxy_spec.get('litellm') if isinstance(proxy_spec.get('litellm'), dict) else proxy_spec
            harness = str((bundle or {}).get('harness') or '').strip().lower()
            family = harness if harness in CLI_RUNS else ''
            if not family and (bundle or {}).get('model'):
                import account_fleet
                accounts_doc = account_fleet.load_accounts()
                canon = account_fleet.canonical((bundle or {}).get('model'), accounts_doc)
                api_family = str(account_fleet.model_spec(accounts_doc, canon).get('api_family') or '').strip().lower()
                if api_family in CLI_RUNS:
                    family = api_family
            if not family:
                family = str(proxy_spec.get('family') or proxy_spec.get('extends') or 'codex').strip().lower()
            ep = str(sub.get('endpoint') or sub.get('base_url') or proxy_spec.get('endpoint') or proxy_spec.get('base_url') or '').strip().rstrip('/')
            key_env = str(sub.get('api_key_env') or proxy_spec.get('api_key_env') or '').strip()
            key = str(sub.get('api_key') or proxy_spec.get('api_key') or (os.environ.get(key_env) if key_env else '') or '').strip()
            if ep and key:
                if family == 'claude':
                    provider = 'claude'
                    env.setdefault('ANTHROPIC_BASE_URL', ep)
                    env.setdefault('ANTHROPIC_API_KEY', key)
                else:
                    provider = family if family in CLI_RUNS else 'codex'
                    base = ep if ep.endswith('/v1') else f'{ep}/v1'
                    env.setdefault('OPENAI_BASE_URL', base)
                    env.setdefault('OPENAI_API_KEY', key)
        if provider not in CLI_RUNS:
            family = provider_inventory.account_family(orig_provider)
            if family in CLI_RUNS:
                provider = family
        if provider not in CLI_RUNS and provider_inventory.launcher_family(orig_provider) == 'grok':
            provider = 'grok'  # a ready Grok ACP definition extends acp, but its CLI is Grok Build
    if provider == 'claude' and orig_provider != 'claude' and 'CLAUDE_CONFIG_DIR' not in env and 'ANTHROPIC_BASE_URL' not in env:
        env['CLAUDE_CONFIG_DIR'] = account_home(orig_provider)
    elif provider == 'codex' and orig_provider != 'codex' and 'CODEX_HOME' not in env and 'OPENAI_BASE_URL' not in env:
        env['CODEX_HOME'] = account_home(orig_provider)
    return provider, env


def cli_command(family, provider, judge):
    if family == 'grok':
        # The executable the provider's Paseo definition names (its sibling grok when it names agent) is the one to run;
        # a configured command is never swapped for another install. Discovery applies only when none is configured.
        providers = (dispatch_flow.paseo_config().get('agents') or {}).get('providers')
        providers = providers if isinstance(providers, dict) else {}
        for _ in range(len(providers) + 1):
            spec = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
            command = spec.get('command')
            if isinstance(command, list) and command and isinstance(command[0], str):
                name = command[0]
                configured = Path(name)
                if configured.stem.lower() not in ('grok', 'agent'):
                    raise RuntimeError(f'The command configured for {provider!r} is not a Grok executable: {name}')
                if configured.name == name:  # bare name: Paseo resolves it through PATH, never the worker's cwd
                    resolved = shutil.which(name)
                    configured = Path(resolved) if resolved else None
                found = configured and next((c for c in (configured.with_name('grok' + configured.suffix), configured)
                                             if c.is_file()), None)
                if found is None:
                    raise RuntimeError(f'The Grok executable configured for {provider!r} is missing: {name}')
                return [str(found)]
            if not spec.get('extends'):
                break
            provider = spec['extends']
    return judge.cli_prefix(CLI_RUNS[family][0])


def cli(args, out, bundle, text, judge):
    """No Paseo here: run the routed bundle through its own CLI, on the account its provider definition selects."""
    workspace = host_placement.canonical_dir(getattr(args, 'workspace', None))
    placed = host_placement.place('cli', workspace, default_cwd=str(Path.cwd()))
    cwd = placed['cwd']
    family, provider_env = provider_definition(bundle['provider'], bundle=bundle)
    ws_note = f' in directory `{workspace}`' if workspace else ''
    native = f'; start it natively{ws_note} with exactly route.bundle and the contents of worker_prompt_file as its prompt.'
    if family not in CLI_RUNS:
        return dict(out, launched=False, dispatch_mode='native', caller_action='launch_with_native_host_tool',
                    next=f'No Paseo daemon and no CLI here for {bundle["provider"]!r}' + native), 0
    if bundle.get('fast'):
        from fast_modes import validate
        import account_fleet
        accounts = account_fleet.load_accounts()
        model = account_fleet.canonical(bundle['model'], accounts)
        validate(family, model, account_fleet.model_spec(accounts, model))
    executable, flags = CLI_RUNS[family]
    env = {k: v for k, v in judge.subscription_env().items() if not k.startswith(HOST_SESSION_ENV)}
    env.update(provider_env)
    env.update(AGENTCOMPASS_WORKER='1', ROUTER_PARENT_AGENT_ID=out['parent_id'],
               ROUTER_PARENT_HOST=out['parent_host'], ROUTER_PARENT_ADDRESS=out['parent_address'])
    run = CLI_RUNS_DIR / f'{time.strftime("%Y%m%d-%H%M%S")}-{bundle["provider"]}-{os.getpid()}'
    run.mkdir(parents=True, exist_ok=True)
    (run / 'brief.md').write_bytes(text.encode('utf-8'))
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} \
        if os.name == 'nt' else {'start_new_session': True}
    prompt_argument = family in PROMPT_ARGUMENT
    try:
        argv = [*cli_command(family, bundle['provider'], judge),
                *flags(bundle.get('launch_model') or bundle['model'], bundle.get('effort'), cwd, bool(bundle.get('fast')))]
        if family in PROMPT_FILE:
            argv += ['--prompt-file', str(run / 'brief.md')]
        with open(run / 'brief.md', 'rb') as brief, open(run / 'output.txt', 'wb') as stdout, \
                open(run / 'stderr.txt', 'wb') as stderr:
            process = subprocess.Popen(argv + [text] if prompt_argument else argv, cwd=cwd, env=env,
                                       stdin=subprocess.DEVNULL if prompt_argument or family in PROMPT_FILE else brief,
                                       stdout=stdout, stderr=stderr, **options)
    except (RuntimeError, OSError) as exc:
        return dict(out, launched=False, dispatch_mode='native', caller_action='launch_with_native_host_tool',
                    next=f'The {executable} CLI could not start ({exc})' + native), 1
    worker = {'provider': bundle['provider'], 'cli': executable, 'pid': process.pid,
              'output': str(run / 'output.txt'), 'stderr': str(run / 'stderr.txt'), 'exit': None}
    return dict(out, launched=True, dispatch_mode='cli', caller_action='already_launched_do_not_respawn',
                worker=worker,
                next='No automatic completion notification is installed for this CLI worker. '
                     f'The worker runs as PID {process.pid}; the caller must watch worker.output and worker.stderr. You own this process: '
                     'wait for it, or stop it before you finish.'), 0


def status_of(result, code):
    """One plain state from the existing launch fields. Uncertain delivery is pending, never not_launched."""
    mode, action = result.get('dispatch_mode'), result.get('caller_action')
    if result.get('launched') is True:
        return 'launched'
    if result.get('launched') is None or action in ('check_delivery_do_not_resend', 'run_next_argv'):
        return 'pending'
    if mode == 'scorer_unavailable' or result.get('error') == 'scorer_unavailable':
        return 'blocked'
    if mode in ('self', 'split'):
        return mode
    if mode == 'native' and code == 0:
        return 'native'
    if mode == 'dry_run':
        return 'dry_run'
    return 'error' if code == 1 else 'blocked'


def launched_next(result, worker, argv):
    """The one accurate sentence for a started worker: who tells the caller it finished depends on the launch surface."""
    mode, exit_code = result.get('dispatch_mode'), worker.get('exit')
    if mode == 'herdr' and worker.get('state') in ('completed', 'failed', 'needs_input', 'cleaned'):
        return f"Herdr operation is {worker['state']}; run next_argv to inspect the existing receipt. Nothing new was launched."
    if mode == 'cli':
        if exit_code is None:
            return (f"Worker is running as PID {worker.get('pid')} and nothing calls back: watch worker_output until it exits, "
                    'or stop it before you finish.')
        return 'Worker finished; read worker_output.' if exit_code == 0 else f'Worker exited with code {exit_code}; read worker_output and worker_stderr.'
    if (worker.get('completion_notification') or {}).get('status') in ('watching', 'delivered'):
        return 'Worker launched; do not launch another. Its completion arrives as a notification.'
    if argv:
        return 'Worker launched; do not launch another. No completion notification is confirmed: run next_argv to collect the result.'
    return result.get('next') or 'Worker launched; do not launch another.'


def compact(result, status, detail_path):
    """The default --task answer: state, worker, reason and the single next action; everything else is on disk."""
    worker = result.get('worker') or {}
    bundle = (result.get('route') or {}).get('bundle') or {}
    argv = worker.get('continuation_argv') or result.get('continuation_argv') or result.get('next_argv')
    nxt = {'self': 'Do this task yourself in the current session; do not spawn a subagent.',
           'dry_run': 'Nothing was recorded or launched.'}.get(status) or result.get('next')
    if status == 'launched':
        nxt = launched_next(result, worker, argv)
    elif status == 'pending' and argv:
        nxt = worker.get('next_action') or 'Run next_argv to observe the same operation; do not roll again.'
    elif status == 'native':
        ws_clause = f" in directory `{result['workspace']}`" if result.get('workspace') else ''
        nxt = (f'Launch one native worker{ws_clause} with exactly route_bundle (its modeId is the permission mode) and the full contents of '
               'worker_prompt_file as its prompt; if the host cannot reproduce that provider/account/model/effort, tell the '
               'user instead of substituting.')
    worker_summary = {k: bundle.get(k) for k in ('provider', 'account', 'model', 'effort') if bundle.get(k)}
    if worker.get('agent_id'):
        worker_summary['agent_id'] = worker['agent_id']
    if worker_summary and worker.get('resumed'):
        worker_summary['resumed'] = True
    is_scorer_unavail = result.get('error') == 'scorer_unavailable' or result.get('dispatch_mode') == 'scorer_unavailable'
    reason_val = 'scorer_unavailable' if is_scorer_unavail else (((result.get('route') or {}).get('reason') or '')[:160] or None)
    out = {'status': status, 'worker': worker_summary or None,
           'reason': reason_val, 'next_action': nxt}
    if is_scorer_unavail:
        out['error'] = 'scorer_unavailable'
        cause = result.get('cause') or ((result.get('route') or {}).get('reason')) or result.get('reason') or 'scorer_unavailable'
    else:
        cause = result.get('error') or worker.get('cause') or worker.get('error') or worker.get('read_error')
    if cause and status in ('error', 'blocked', 'pending'):
        out['cause'] = str(cause)[:300]
    if (result.get('route') or {}).get('warnings'):
        out['warnings'] = result['route']['warnings'][:4]
    if argv and (status == 'pending' or (status == 'launched' and 'next_argv' in (nxt or ''))):
        out['next_argv'] = argv
    if status == 'launched' and result.get('dispatch_mode') == 'cli':
        out['worker_output'], out['worker_stderr'] = worker.get('output'), worker.get('stderr')
    if status == 'native':
        out['route_bundle'], out['worker_prompt_file'] = bundle, result.get('worker_prompt_file')
    if status == 'split':
        out['task_count'] = result.get('task_count')
    if result.get('start_after_hours') is not None:
        out['start_after_hours'] = result['start_after_hours']
        if result.get('caller_action') == 'wait_for_reset':
            out['caller_action'] = 'wait_for_reset'
    if result.get('retry_at'):
        out['retry_at'] = result['retry_at']
    if result.get('workspace'):
        out['workspace'] = result['workspace']
    if result.get('blocked_by'):
        out['blocked_by'] = result['blocked_by']
    if result.get('attempts'):
        out['attempts'] = result['attempts']
    if result.get('assumptions'):
        out['assumptions'] = result['assumptions']
    if result.get('warnings'):
        out['warnings'] = [*out.get('warnings', []), *result['warnings']]
    if result.get('continuation'):
        out['continuation'] = result['continuation']
    out['details'] = detail_path
    return {k: v for k, v in out.items() if v is not None}


# Flags that describe one brief; a batch takes only the shared ones and each brief is rolled as written.
BATCH_ONLY_ALONE = ('task_id', 'title', 'hours', 'can_wait_hours', 'local_only', 'keep_whole', 'file', 'apply_to', 'provider', 'account',
                    'model', 'effort', 'basis', 'resume')


def run_batch(args, judge):
    """Several one-line briefs: classified at the same time, then routed and launched one after another so each
    allocation sees the reservations of the ones before it."""
    briefs = list(args.brief)
    if len({str(Path(b).resolve()) for b in briefs}) != len(briefs):
        raise ValueError('The same --brief was given twice.')
    alone = [f"--{name.replace('_', '-')}" for name in BATCH_ONLY_ALONE if getattr(args, name, None) not in (None, False, [], 0)]
    if alone:
        raise ValueError('Several --brief files take only shared flags; roll a brief on its own for ' + ', '.join(alone) + '.')
    try:
        host_placement.validate_workspace(getattr(args, 'workspace', None))
    except host_placement.InvalidWorkspace as exc:
        return ({'status': 'error', 'error': 'invalid_workspace_directory', 'cause': 'invalid_workspace_directory',
                 'workspace': getattr(args, 'workspace', None),
                 'next_action': f'Nothing was launched: {exc}'}, 1)
    each = [argparse.Namespace(**{**vars(args), 'brief': Path(b)}) for b in briefs]
    if not args.dry_run:
        judge.prefetch_scores([build_task(one, read_task_text(one), None) for one in each], args.score_timeout)
    results, codes = [], []
    batch_used_agents = set()
    for one in each:
        one._exclude_agents = set(batch_used_agents)
        try:
            result, code = run(one, judge)
        except (OSError, ValueError, RuntimeError) as exc:
            result, code = {'status': 'error', 'cause': str(exc)[:300],
                            'next_action': 'Fix the cause, then roll this brief again on its own.'}, 1
        used_id = (result.get('worker') or {}).get('agent_id') if isinstance(result, dict) else None
        if isinstance(used_id, str) and used_id:
            batch_used_agents.add(used_id)
        results.append({'brief': str(one.brief), **result})
        codes.append(code)
    code = 0 if not any(codes) else (1 if 1 in codes else 2)
    return {'status': 'batch', 'results': results,
            'next_action': 'Handle each result on its own: its status and next_action say what to do for that brief.'}, code


def run(args, judge):
    """Compact result (status, next action, pointers) for `--task` and `--brief` alike; `--verbose` prints the full result, which is on disk either way."""
    result, code = _run(args, judge)
    status = status_of(result, code)
    result = dict(result, status=status)
    agent_id = (result.get('worker') or {}).get('agent_id')
    if agent_id:
        result['continuation'] = worker_sessions.continuation_verdict(agent_id)
    # The file on disk and --verbose carry the same cause and next action as the compact answer.
    summary = compact(result, status, None)
    result.update({k: summary[k] for k in ('cause', 'next_action') if summary.get(k) and k not in result})
    if getattr(args, 'verbose', False):
        return result, code
    if status == 'dry_run':
        return compact(result, status, None), code
    path = None
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / f'{int(time.time())}-{uuid.uuid4().hex[:8]}.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    except OSError:
        return result, code  # nowhere durable to put the detail: give the caller all of it
    return compact(result, status, str(path)), code


def _run(args, judge):
    """judge is the route_judge module (plan_stage, record_constraint, usage_snapshot, subscription_env, cli_prefix)."""
    text = read_task_text(args)
    if not text.strip():
        raise ValueError('The task is empty.')
    try:
        canonical_ws = host_placement.validate_workspace(getattr(args, 'workspace', None))
    except host_placement.InvalidWorkspace as exc:
        return ({
            'stage': 'roll',
            'launched': False,
            'dispatch_mode': 'none',
            'caller_action': 'check_error',
            'error': 'invalid_workspace_directory',
            'workspace': getattr(args, 'workspace', None),
            'next': f'Nothing was launched: {exc}',
        }, 1)
    # Resolve before classification/ledger writes: never launch an orphan as a fallback.
    if args.dry_run and not (os.environ.get('PASEO_AGENT_ID', '').strip() or args.parent_id):
        parent, parent_host, parent_address = None, None, None
    else:
        parent, parent_host, parent_address = resolve_parent(args)
    task = build_task(args, text, parent)
    task.update(parent_host=parent_host, parent_address=parent_address)
    host_session = harness_session(parent) if parent else ''
    if host_session:
        task['host_session_id'] = host_session
    if parent and not args.dry_run:
        pending_rows = [r for r in registry.pending() if r.get('parent_id') == parent]
        if pending_rows:
            launchers = (provider_inventory.current().get('launchers') or {})
            can_reconcile = any(
                (r.get('task_id') == task['task_id'])
                or (launchers.get((r.get('bundle') or {}).get('provider')) or {}).get('status') != 'disabled'
                for r in pending_rows
            )
            if can_reconcile:
                for r in pending_rows:
                    dispatch_flow.reconcile(r['dispatch_id'])
                pending_rows = [r for r in registry.pending() if r.get('parent_id') == parent]
        # The same task already dispatched by this parent: hand back that worker, without classifying or launching again.
        for row in pending_rows:
            if not getattr(args, 'new_worker', False) and (row['parent_id'], row['task_id']) == (parent, task['task_id']):
                argv = entry.argv('launch', '--dispatch-id', row['dispatch_id'], '--parent-id', parent, '--watch-seconds', '120')
                return dict(stage='roll', parent_id=parent, launched=None, dispatch_mode='paseo',
                            caller_action='run_next_argv', worker=dict(dispatch_flow.public_chain(row), continuation_argv=argv),
                            next='This task already has a dispatch (state ' + row['state'] + '): run next_argv to observe it; '
                                 'it will not create a second worker.'), 0
        matched_worker = match_reusable_worker(args, task, text, parent)
        if matched_worker and matched_worker.get('bundle_id'):
            task['chain'] = {
                'id': matched_worker['chain_id'],
                'coupling': 'high' if getattr(args, 'resume', None) else 'medium',
                'worker': {
                    'agent_id': matched_worker['agent_id'],
                    'bundle': matched_worker['bundle_id'],
                    'read_only_review': matched_worker.get('read_only_review'),
                    **matched_worker['session_state'],
                },
            }
    with provider_inventory.snapshot():
        if args.provider or args.model or args.effort:
            if not args.basis:
                raise ValueError('Naming --provider/--model/--effort is a user override; quote the user with --basis.')
            args.constraint = 'user_override'
            # Only the model is pinned: the task is classified exactly as an automatic roll's, and the pinned bundle
            # is priced and reserved from that profile.
            classified = judge.classify_pinned(task, args)
            decision = classified['result'] if 'result' in classified else judge.record_constraint(args, task, classified)
        else:
            args.stage = 'roll'
            decision, _ = judge.plan_stage({'task': task}, args)
            if (getattr(args, 'no_score', False) and not getattr(args, 'dry_run', False)
                    and not isinstance(task.get('profile'), dict)
                    and decision.get('error') != 'no_ready_account'):
                decision = judge.scorer_unavailable_result(
                    task,
                    {
                        'source': 'scorer_unavailable',
                        'model': None,
                        'fallback_reason': 'no_score_without_profile',
                        'fallback_detail': 'roll --no-score requires a complete caller-supplied task.profile.',
                    },
                    time.monotonic(),
                )
    out = {'stage': 'roll', 'route': summary(decision), 'parent_id': parent,
           'parent_host': parent_host, 'parent_address': parent_address,
           **({'workspace': canonical_ws} if canonical_ws else {})}
    if decision.get('choice') == 'split':
        out['route']['reason'] = decision.get('reason')
        return dict(out, launched=False, dispatch_mode='split', caller_action='roll_each_line',
                    task_count=decision['task_count'], next=decision['next']), 2
    if (said := hours_assumption(args, decision)) is not None:
        out['assumptions'] = [said]
    if decision.get('error') == 'scorer_unavailable':
        out['route']['reason'] = decision.get('reason') or 'scorer_unavailable'
        return dict(out, launched=False, dispatch_mode='scorer_unavailable',
                    caller_action='parent_autonomous_choice', error='scorer_unavailable',
                    reason='scorer_unavailable', cause=decision.get('reason'),
                    scorer=decision.get('scorer'), next=decision.get('next')), 2
    if (decision.get('task_profile') or {}).get('urgency') == 'high' and not (decision.get('task_profile') or {}).get('wants_fast'):
        out.setdefault('assumptions', []).append(
            'The task profile notes high urgency without wants_fast: fast mode uses task_profile.wants_fast or --fast, '
            'and is enabled only when quota allows.')
    if args.dry_run:
        return dict(out, launched=False, dispatch_mode='dry_run', caller_action='none',
                    next='Dry run: nothing recorded or launched.'), 0
    bundle = decision.get('selected_bundle') or {}
    provider = bundle.get('provider')
    if decision.get('choice') == 'self':
        return dict(out, launched=False, dispatch_mode='self', caller_action='execute_self',
                    next='Execute this task directly in the current session; do not spawn a subagent.'), 2
    if decision.get('choice') in (None, 'none') or not bundle:
        return dict(out, launched=False, dispatch_mode='none', caller_action='execute_self_or_ask_user',
                    next=decision.get('next') or 'No worker fits this brief; do it yourself or ask the user.'), 2
    if decision.get('host_policy_violation'):
        violation = decision['host_policy_violation']
        return dict(out, launched=False, dispatch_mode='none', caller_action='check_host_policy',
                    host_policy_violation=violation,
                    next=f"Host policy excludes account {violation.get('account')!r}. Inspect "
                         f"{violation.get('policy_path') or 'the configured host policy'}; discovery and an explicit "
                         'task request do not change allowed_accounts.'), 2
    if (decision.get('decision') or {}).get('needs_user_decision'):
        return dict(out, launched=False, dispatch_mode='none', caller_action='ask_user',
                    next='The router needs a user decision; show route.reason to the user.'), 2
    if (decision.get('decision') or {}).get('action') == 'defer':
        wait = (decision.get('decision') or {}).get('start_after_hours') or (decision.get('decision') or {}).get('wait_hours')
        return dict(out, launched=False, dispatch_mode='defer', caller_action='wait_for_reset',
                    start_after_hours=wait,
                    next=f"Defer {bundle.get('id') or decision.get('choice')} for {wait}h until its quota window resets; do not launch now."), 2
    if provider in host_placement.REMOTE_SURFACES:
        placed = host_placement.place(provider, canonical_ws)
        if not placed['ok']:
            return placement_refused(out, provider, placed)
    text, prompt_file = prepare_worker_prompt(text, parent, parent_host, parent_address)
    out['worker_prompt_file'] = str(prompt_file)
    if provider == 'dot':
        return dot(args, out, prompt_file, task=task, decision=decision)
    if provider == 'chatgpt-web':
        return consult(args, out, prompt_file)
    if provider == 'grok-bot':
        return grok_bot(args, out, prompt_file, task, bundle, decision=decision)
    decision_profile = (
        decision.get('task_profile')
        or ((decision.get('decision') or {}).get('task') or {}).get('profile')
        or task.get('profile')
    )
    access = task_access(args, decision_profile if isinstance(decision_profile, dict) else None)
    has_paseo_parent = bool(os.environ.get('PASEO_AGENT_ID', '').strip() or getattr(args, 'parent_id', None))
    use_paseo = (
        parent_host not in ('herdr', *NATIVE_HOSTS)
        and can_launch_via_paseo(provider)
        and has_paseo_parent
        and (os.environ.get('PASEO_AGENT_ID', '').strip() or dispatch_flow.paseo_reachable())
    )
    if use_paseo:
        return paseo(args, out, decision, task, text, parent, judge.usage_snapshot(args.usage_cache), judge=judge)
    surface = 'herdr' if parent_host == 'herdr' else parent_host if parent_host in NATIVE_HOSTS else 'cli'
    placed = host_placement.place(surface, canonical_ws, default_cwd=str(Path.cwd()),
                                  bundle=bundle, address=parent_address)
    if not placed['ok']:
        return placement_refused(out, surface, placed)
    landing = placed['cwd']
    if not canonical_ws and landing and access == 'write':
        out = same_cwd_warning(out, landing, parent)
    usage = judge.usage_snapshot(getattr(args, 'usage_cache', None)) if hasattr(judge, 'usage_snapshot') else {}
    try:
        claim_row = registry.record_host_claim(
            surface, decision, task, bundle,
            label=getattr(args, 'title', None) or title_of(text),
            usage=usage, access=access,
            workspace=canonical_ws, cwd=landing,
            launched=surface in ('herdr', 'cli'),
        )
    except registry.WorkspaceBusy as busy:
        return workspace_blocked(out, busy.workspace, busy.holders,
                                 dispatch_mode='native' if surface in NATIVE_HOSTS else surface)
    if surface == 'herdr':
        import host_herdr
        res, code = host_herdr.launch(out, bundle, prompt_file, task['task_id'], judge,
                                      cwd=landing, dispatch_id=claim_row['dispatch_id'])
        if code != 0 and res.get('launched') is False:
            registry.settle_host_claim(claim_row['dispatch_id'], 'canceled', 'herdr_preflight_failed')
        return res, code
    if surface in NATIVE_HOSTS:
        return dict(out, launched=False, dispatch_mode='native', caller_action='launch_with_native_host_tool',
                    **({'dispatch_id': claim_row['dispatch_id']} if canonical_ws else {}),
                    next=placed['next']), 0
    res, code = cli(args, out, bundle, text, judge)
    if not res.get('launched'):
        registry.settle_host_claim(claim_row['dispatch_id'], 'canceled', 'cli_not_launched')
    return res, code
