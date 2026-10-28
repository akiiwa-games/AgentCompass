"""roll: one command from a written task brief to a routed, launched worker.

The classifier reads the original brief; the worker receives it unchanged plus the communication contract.
Classification, allocation, the ledger record, dispatch preparation and launch happen here.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import dispatch_flow
import dispatch_registry as registry
import provider_inventory
from worker_context import require_parent_id, resolve_parent

OBJECTIVE_CHARS = 6000
PASEO_PROVIDERS = ('codex', 'codex-2', 'claude', 'claude-2', 'pi', 'antigravity-resilient', 'opencode')
SKILLS = Path(__file__).resolve().parents[2]
DOT_RUNNER = SKILLS / 'dot-subagent' / 'scripts' / 'run_dot_task.py'
CONSULT_BRIDGE = SKILLS / 'chatgpt-pro-consult' / 'scripts' / 'consult_bridge.py'
CLI_RUNS_DIR = Path.home() / '.route-manager' / 'cli-workers'
WORKER_PROMPTS_DIR = Path.home() / '.route-manager' / 'worker-prompts'
WORKER_COMMUNICATION = SKILLS / 'route-agent-work' / 'references' / 'worker-communication.md'
# Without Paseo, each CLI family runs one routed bundle unattended with full tool access, as Paseo would launch it:
# executable and flags for (model, effort, cwd). agy takes its prompt only as the argument after --print; the
# others read it from stdin.
CLI_RUNS = {
    'claude': ('claude', lambda model, effort, cwd, fast=False: ['-p', '--model', model, *(['--effort', effort] if effort else []),
                                                                 '--permission-mode', 'bypassPermissions']),
    'codex': ('codex', lambda model, effort, cwd, fast=False: ['exec', '-m', model,
                                                               *(['-c', f'model_reasoning_effort="{effort}"'] if effort else []),
                                                               *(['-c', 'features.fast_mode=true'] if fast else []),
                                                               '--dangerously-bypass-approvals-and-sandbox',
                                                               '--skip-git-repo-check', '-C', cwd, '-']),
    'pi': ('pi', lambda model, effort, cwd, fast=False: ['-p', '--model', model if '/' in model else f'openai-codex/{model}', *(['--thinking', effort] if effort else [])]),
    'antigravity-resilient': ('agy', lambda model, effort, cwd, fast=False: ['--dangerously-skip-permissions', '--model', model,
                                                                             '--add-dir', cwd, '--print']),
    'opencode': ('opencode', lambda model, effort, cwd, fast=False: [
        'run', '-m', model if '/' in model else f'opencode/{model}',
        *(['--variant', effort] if effort else []), '--dir', cwd]),
}
PROMPT_ARGUMENT = ('antigravity-resilient', 'opencode')
# The caller's own session and subscription; a CLI worker gets its provider's, never these.
HOST_SESSION_ENV = ('CLAUDE', 'CODEX_', 'PASEO_AGENT')


def title_of(text):
    for line in text.splitlines():
        line = line.strip().lstrip('#').strip()
        if line:
            return line[:60]
    return 'task'


def build_task(args, text, parent):
    # Same brief, same parent -> same task_id, so a repeated roll cannot open a second live dispatch.
    hours = getattr(args, 'hours', None)
    if not (isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0 and hours != float('inf')):
        raise ValueError('roll requires --hours <expected_active_hours> (positive number).')
    task = {'objective': text[:OBJECTIVE_CHARS],
            'expected_hours': float(hours),
            'task_id': args.task_id or 'roll-' + hashlib.sha256(text.encode()).hexdigest()[:16]}
    if parent:
        task['parent_agent_id'] = parent
    if args.local_only:
        task['needs'] = {'local_only': True, 'local_only_reason': args.local_only}
    if getattr(args, 'file', None) or getattr(args, 'apply_to', None):
        task.setdefault('needs', {})['file_transfer'] = True
    if getattr(args, 'fast', False):
        task.setdefault('needs', {})['fast'] = True
    return task


def summary(decision):
    bundle = decision.get('selected_bundle') or {}
    return {'choice': decision.get('choice'),
            'bundle': {k: bundle[k] for k in ('provider', 'harness', 'account', 'model', 'effort', 'modeId', 'fast') if bundle.get(k)},
            'reason': ((decision.get('host_policy_violation') or {}).get('reason') or
                       (decision.get('decision') or {}).get('reason') or '')[:300] or None,
            'alerts': [a['message'] for a in decision.get('alerts') or [] if a.get('severity') == 'warn']}


def python(script, *argv):
    return [sys.executable, '-X', 'utf8', str(script), *argv]


def prepare_worker_prompt(text, parent, host='paseo', address=None):
    """One contract for every launch surface; retain the source brief and a durable native-fallback prompt."""
    parent = require_parent_id(parent)
    recipient = (f'Parent session agent ID: `{parent}`.\n'
                 f'Parent host: `{host}`. Parent message address: `{address or parent}`.')
    prompt = text + '\n\n---\n\n' + WORKER_COMMUNICATION.read_text(encoding='utf-8') + '\n' + recipient + '\n'
    WORKER_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKER_PROMPTS_DIR / f'{uuid.uuid4()}.md'
    path.write_text(prompt, encoding='utf-8')
    return prompt, path


def dot(args, out, prompt_file):
    files = [str(f) for f in args.file]
    apply_to = ['--apply-to', str(args.apply_to or Path.cwd())] if files else []
    command = python(DOT_RUNNER, 'run', '--prompt-file', str(prompt_file),
                     *[x for f in files for x in ('--file', f)], *apply_to,
                     *(['--wait', str(args.wait)] if args.wait > 0 else ['--no-wait']))
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=args.wait + 180)
    found = re.search(r'task_id=(\S+)', proc.stderr)
    launched = bool(found) or proc.returncode == 0
    result = dict(out, launched=launched, dispatch_mode='dot',
                  caller_action='already_launched_do_not_respawn' if launched else 'check_error',
                  worker={'provider': 'dot', 'task_id': found.group(1) if found else None, 'exit': proc.returncode,
                          'reply': proc.stdout[-6000:] if args.wait > 0 else None, 'stderr': proc.stderr[-1500:] or None})
    if launched:
        result['next'] = ('Worker is ALREADY launched on Dot by roll. Do NOT call create_agent, Agent, '
                          'spawn_agent, or invoke_subagent; use continuation_argv or read worker.reply.')
    if found and (args.wait <= 0 or proc.returncode == 3):
        result['continuation_argv'] = python(DOT_RUNNER, 'status', '--task-id', found.group(1), '--wait', '120', *apply_to)
    return result, 0 if proc.returncode in (0, 3) else 1


def consult(args, out, prompt_file):
    target = args.brief.with_name(f'{args.brief.stem}-consult-{time.strftime("%Y%m%d-%H%M%S")}')
    command = python(CONSULT_BRIDGE, '--prompt', str(prompt_file), '--out', str(target))
    if args.wait <= 0:
        # A browser consultation blocks for up to 30 minutes; hand over the one command instead of backgrounding it.
        return dict(out, launched=False, dispatch_mode='chatgpt-web', caller_action='run_next_argv', next_argv=command,
                    next='Run next_argv (blocks until the answer is in <out>/response.md), or roll again with --wait 1800.'), 0
    proc = subprocess.run(command + ['--timeout', str(int(args.wait))], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=args.wait + 120)
    answer = target / 'response.md'
    return dict(out, launched=True, dispatch_mode='chatgpt-web', caller_action='already_launched_do_not_respawn',
                worker={'provider': 'chatgpt-web', 'out': str(target), 'exit': proc.returncode,
                'answer_file': str(answer) if answer.is_file() else None, 'stderr': proc.stderr[-1500:] or None}), \
        0 if proc.returncode == 0 else 1


def grok_bot(args, out, prompt_file, task, bundle):
    """Same external-runner receipt/continuation surface as Dot; no Paseo adapter."""
    import run_grok_bot as runner
    if args.file or args.apply_to or args.local_only:
        raise ValueError('grok-bot runs remotely and accepts text only; local-only/file-transfer options are unsupported.')
    if bundle.get('model') != 'grok-bot' or bundle.get('account') != 'grok-bot' or bundle.get('effort'):
        raise ValueError('Use --provider grok-bot --account grok-bot --model grok-bot without an effort; upstream owns model selection.')
    try:
        receipt = runner.submit(prompt_file.read_text(encoding='utf-8'), out['parent_id'], task['task_id'])
    except (runner.TransportError, OSError, ValueError) as exc:
        return dict(out, launched=False, dispatch_mode='grok-bot', caller_action='check_error', error=str(exc)), 1
    if args.wait > 0 and receipt['state'] not in ('completed', 'failed'):
        try:
            receipt = runner.status(receipt['task_id'], args.wait)
        except (runner.TransportError, OSError, ValueError) as exc:
            # A continuation failure cannot invalidate the durable send receipt.
            receipt = {**receipt, 'read_error': str(exc)}
    delivery = receipt['delivery']
    completed = receipt['state'] == 'completed'
    result = dict(out, launched=True if completed or delivery == 'accepted' else False if delivery == 'rejected' else None,
                  dispatch_mode='grok-bot', completed=completed, worker=receipt,
                  caller_action='completed' if completed else 'already_launched_do_not_respawn' if delivery == 'accepted' else
                  'check_delivery_do_not_resend' if delivery == 'unknown' else 'check_error')
    if receipt['state'] not in ('completed', 'failed'):
        result['continuation_argv'] = python(Path(runner.__file__), 'status', '--task-id', receipt['task_id'], '--wait', '30')
        result['next'] = 'Read continuation_argv; never repeat an uncertain send. Relay needs_input to the parent; resolve cards in the official app.'
    return result, 1 if receipt['state'] == 'failed' else 0


def paseo(args, out, decision, task, text, parent, usage, judge=None):
    launch = {'title': args.title or title_of(text), 'initialPrompt': text}
    if args.workspace:
        launch['workspaceId'] = args.workspace
    dispatch_flow.reconcile()
    row = registry.prepare(decision['decision_id'], task, launch, usage)
    receipt = dispatch_flow.launch(row['dispatch_id'], parent, watch_seconds=args.wait)
    if (
        receipt.get('state') == 'failed'
        and receipt.get('error') == 'parent_agent_not_found'
        and not os.environ.get('PASEO_AGENT_ID', '').strip()
        and judge is not None
    ):
        return cli(args, out, decision['selected_bundle'], text, judge)
    failed = receipt.get('state') == 'failed'
    return dict(out, launched=not failed, dispatch_mode='paseo',
                caller_action='already_launched_do_not_respawn' if not failed else 'check_error',
                next=('Worker is ALREADY launched in Paseo by roll. Do NOT call create_agent, Agent, '
                      'spawn_agent, or invoke_subagent again; wait for its message or check status.') if not failed else 'Paseo launch failed.',
                worker=dict(receipt, provider=decision['selected_bundle']['provider'])), \
        1 if failed else 0


def _router_override_proxy_spec(provider):
    state_dir = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.route-manager')
    override_env = os.environ.get('ROUTE_MANAGER_ACCOUNTS_OVERRIDE') or os.environ.get('ROUTER_ACCOUNTS_OVERRIDE')
    override_path = Path(override_env).expanduser() if override_env else state_dir / 'accounts-override.json'
    doc, _ = provider_inventory.read_json(override_path)
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


def can_launch_via_paseo(provider):
    if provider in PASEO_PROVIDERS:
        return True
    providers = (dispatch_flow.paseo_config().get('agents') or {}).get('providers')
    if isinstance(providers, dict) and provider in providers:
        return provider_inventory.launcher_family(provider) in ('claude', 'codex', 'pi', 'opencode', 'antigravity-resilient')
    if _router_override_proxy_spec(provider) is not None:
        return False
    return provider_inventory.launcher_family(provider) in ('claude', 'codex')


def provider_definition(provider, bundle=None):
    """Paseo's definition of a provider: the CLI family it extends and the env it sets, e.g. claude-2 -> claude with
    CLAUDE_CONFIG_DIR. A secondary claude-*/codex-* slot without a Paseo entry falls back to its ~/.<provider> home."""
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
            model_name = str((bundle or {}).get('model') or '').lower()
            harness = str((bundle or {}).get('harness') or '').strip().lower()
            family = (
                harness if harness in CLI_RUNS
                else 'claude' if 'claude' in model_name
                else 'codex' if any(k in model_name for k in ('gpt', 'sol', 'astra', 'o1', 'o3'))
                else str(proxy_spec.get('family') or proxy_spec.get('extends') or 'codex').strip().lower()
            )
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
            if orig_provider.startswith('claude-'):
                provider = 'claude'
            elif orig_provider.startswith('codex-'):
                provider = 'codex'
            elif orig_provider.startswith('pi-'):
                provider = 'pi'
            elif orig_provider.startswith('opencode-'):
                provider = 'opencode'
    if provider == 'claude' and orig_provider != 'claude' and 'CLAUDE_CONFIG_DIR' not in env and 'ANTHROPIC_BASE_URL' not in env:
        env['CLAUDE_CONFIG_DIR'] = os.environ.get('CLAUDE_HOME_2') if orig_provider == 'claude-2' and os.environ.get('CLAUDE_HOME_2') else str(Path.home() / f'.{orig_provider}')
    elif provider == 'codex' and orig_provider != 'codex' and 'CODEX_HOME' not in env and 'OPENAI_BASE_URL' not in env:
        env['CODEX_HOME'] = os.environ.get('CODEX_HOME_2') if orig_provider == 'codex-2' and os.environ.get('CODEX_HOME_2') else str(Path.home() / f'.{orig_provider}')
    return provider, env


def cli(args, out, bundle, text, judge):
    """No Paseo here: run the routed bundle through its own CLI, on the account its provider definition selects."""
    family, provider_env = provider_definition(bundle['provider'], bundle=bundle)
    native = '; start it natively with exactly route.bundle and the contents of worker_prompt_file as its prompt.'
    if family not in CLI_RUNS:
        return dict(out, launched=False, dispatch_mode='native', caller_action='launch_with_native_host_tool',
                    next=f'No Paseo daemon and no CLI here for {bundle["provider"]!r}' + native), 0
    executable, flags = CLI_RUNS[family]
    cwd = str(Path.cwd())
    env = {k: v for k, v in judge.subscription_env().items() if not k.startswith(HOST_SESSION_ENV)}
    env.update(provider_env)
    env.update(ROUTER_WORKER='1', ROUTER_PARENT_AGENT_ID=out['parent_id'],
               ROUTER_PARENT_HOST=out['parent_host'], ROUTER_PARENT_ADDRESS=out['parent_address'])
    run = CLI_RUNS_DIR / f'{time.strftime("%Y%m%d-%H%M%S")}-{bundle["provider"]}-{os.getpid()}'
    run.mkdir(parents=True, exist_ok=True)
    (run / 'brief.md').write_bytes(text.encode('utf-8'))
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} \
        if os.name == 'nt' else {'start_new_session': True}
    prompt_argument = family in PROMPT_ARGUMENT
    try:
        argv = [*judge.cli_prefix(executable), *flags(bundle.get('launch_model') or bundle['model'], bundle.get('effort'), cwd, bool(bundle.get('fast')))]
        with open(run / 'brief.md', 'rb') as brief, open(run / 'output.txt', 'wb') as stdout, \
                open(run / 'stderr.txt', 'wb') as stderr:
            process = subprocess.Popen(argv + [text] if prompt_argument else argv, cwd=cwd, env=env,
                                       stdin=subprocess.DEVNULL if prompt_argument else brief,
                                       stdout=stdout, stderr=stderr, **options)
    except (RuntimeError, OSError) as exc:
        return dict(out, launched=False, dispatch_mode='native', caller_action='launch_with_native_host_tool',
                    next=f'The {executable} CLI could not start ({exc})' + native), 1
    worker = {'provider': bundle['provider'], 'cli': executable, 'pid': process.pid,
              'output': str(run / 'output.txt'), 'stderr': str(run / 'stderr.txt')}
    try:
        worker['exit'] = process.wait(timeout=args.wait) if args.wait > 0 else None
    except subprocess.TimeoutExpired:
        worker['exit'] = None
    if worker['exit'] is None:
        return dict(out, launched=True, dispatch_mode='cli', caller_action='already_launched_do_not_respawn',
                    worker=worker,
                    next=f'The worker runs as PID {process.pid}; its reply lands in worker.output. You own this process: '
                         'wait for it, or stop it before you finish.'), 0
    worker['reply'] = (run / 'output.txt').read_bytes().decode('utf-8', 'replace')[-6000:]
    return dict(out, launched=True, dispatch_mode='cli', caller_action='already_launched_do_not_respawn',
                worker=worker), 0 if worker['exit'] == 0 else 1


def run(args, judge):
    """judge is the route_judge module (plan_stage, record_constraint, usage_snapshot, subscription_env, cli_prefix)."""
    text = args.brief.read_text(encoding='utf-8-sig')
    if not text.strip():
        raise ValueError('The brief is empty.')
    # Resolve before classification/ledger writes: never launch an orphan as a fallback.
    if args.dry_run and not (os.environ.get('PASEO_AGENT_ID', '').strip() or args.parent_id):
        parent, parent_host, parent_address = None, None, None
    else:
        parent, parent_host, parent_address = resolve_parent(args)
    task = build_task(args, text, parent)
    task.update(parent_host=parent_host, parent_address=parent_address)
    if args.provider or args.model or args.effort:
        if not args.basis:
            raise ValueError('Naming --provider/--model/--effort is a user override; quote the user with --basis.')
        args.constraint = 'user_override'
        decision = judge.record_constraint(args, task)
    else:
        decision, _ = judge.plan_stage({'task': task}, args)
    out = {'stage': 'roll', 'route': summary(decision), 'parent_id': parent,
           'parent_host': parent_host, 'parent_address': parent_address}
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
                    next='No worker fits this brief; do it yourself or ask the user.'), 2
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
    text, prompt_file = prepare_worker_prompt(text, parent, parent_host, parent_address)
    out['worker_prompt_file'] = str(prompt_file)
    if provider == 'dot':
        return dot(args, out, prompt_file)
    if provider == 'chatgpt-web':
        return consult(args, out, prompt_file)
    if provider == 'grok-bot':
        return grok_bot(args, out, prompt_file, task, bundle)
    if parent_host == 'native':
        return dict(out, launched=False, dispatch_mode='native', caller_action='launch_with_native_host_tool',
                    next='Launch the native host worker with exactly route.bundle '
                    'and the contents of worker_prompt_file as --spec (not the filename). First verify that '
                    'the host can reproduce the routed provider/account/model/effort; otherwise reroute. Preserve '
                    'the live coordinator inbox, parent session ID and injected worker preamble; do not switch hosts.'), 0
    has_paseo_parent = bool(os.environ.get('PASEO_AGENT_ID', '').strip() or getattr(args, 'parent_id', None))
    if can_launch_via_paseo(provider) and has_paseo_parent and (
        os.environ.get('PASEO_AGENT_ID', '').strip() or dispatch_flow.paseo_reachable()
    ):
        return paseo(args, out, decision, task, text, parent, judge.usage_snapshot(args.usage_cache), judge=judge)
    return cli(args, out, bundle, text, judge)
