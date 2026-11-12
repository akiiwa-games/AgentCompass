"""Herdr core adapter. Uses hc terminals, never the older herdr TUI API.

Receipts live under router state; the terminal name makes uncertain deliveries
recoverable. No daemon, template, global setting, credential or service is installed.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entry


def runs_dir():
    return Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'herdr-workers'


def save(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


@contextmanager
def receipt_lock():
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / 'receipt-lock.sqlite3', timeout=60, isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        yield
        connection.commit()
    finally:
        connection.close()


def hc(*args, timeout=25):
    executable = shutil.which('hc')
    if not executable:
        raise RuntimeError('Herdr core hc is not on PATH. Install current herdr-core; the old herdr TUI is incompatible.')
    proc = subprocess.run([executable, *args], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    try:
        data = json.loads(proc.stdout or proc.stderr)
    except ValueError:
        data = {'stdout': proc.stdout, 'stderr': proc.stderr}
    if proc.returncode:
        raise RuntimeError(json.dumps({'exit': proc.returncode, 'reply': data}, ensure_ascii=False))
    return data


def interactive(bundle, judge):
    """Only mappings we can reproduce exactly; account selection uses the existing provider adapter."""
    import roll
    family, provider_env = roll.provider_definition(bundle['provider'], bundle=bundle)
    mode = bundle.get('modeId')
    expected = {'claude': 'bypassPermissions', 'codex': 'full-access', 'pi': 'full-access'}
    if family not in expected or mode not in (None, expected[family]):
        raise ValueError(f'Herdr cannot reproduce {family!r} permission mode {mode!r}; reroute, do not substitute.')
    if bundle.get('fast'):
        import account_fleet
        from fast_modes import validate
        accounts = account_fleet.load_accounts()
        canonical = account_fleet.canonical(bundle['model'], accounts)
        validate(family, canonical, account_fleet.model_spec(accounts, canonical))
    model, effort = bundle.get('launch_model') or bundle['model'], bundle.get('effort')
    command = roll.cli_command(family, bundle['provider'], judge)
    if family == 'claude':
        flags = ['--model', model, '--permission-mode', 'bypassPermissions',
                 *(['--effort', effort] if effort else []),
                 '--settings', json.dumps({'fastMode': bool(bundle.get('fast'))})]
    elif family == 'codex':
        flags = ['--no-daemon', '-m', model, '--dangerously-bypass-approvals-and-sandbox',
                 *(['-c', f'model_reasoning_effort="{effort}"'] if effort else []),
                 '-c', 'features.fast_mode=' + str(bool(bundle.get('fast'))).lower(),
                 '-c', 'service_tier="priority"' if bundle.get('fast') else 'service_tier="default"']
    else:
        if bundle.get('fast'):
            raise ValueError('Herdr Pi adapter cannot reproduce fast mode; reroute.')
        flags = ['--model', model if '/' in model else f'openai-codex/{model}',
                 *(['--thinking', effort] if effort else [])]
    return [*command, *flags], provider_env


def terminal_from(reply):
    term = reply.get('terminal') if isinstance(reply, dict) else None
    return term if isinstance(term, dict) else reply


def receipt_result(out, request, path):
    state = request['state']
    launched = state in ('launched', 'completed', 'failed', 'needs_input', 'cleaned')
    return dict(out, launched=True if launched else None, dispatch_mode='herdr',
                caller_action='already_launched_do_not_respawn' if launched else 'check_delivery_do_not_resend',
                worker={'terminal': request.get('terminal'), 'state': state, 'request': str(path),
                        'continuation_argv': entry.argv('herdr', 'status', '--request', path)},
                next=f'Existing Herdr operation is {state}; the same task never launches twice. Reports arrive through hc send; '
                     'run next_argv to read terminal status/output. Do not repeat an uncertain launch.'), 0


def launch(out, bundle, prompt_file, task_id, judge, *, cwd=None, dispatch_id=None):
    with receipt_lock():
        return _launch(out, bundle, prompt_file, task_id, judge, cwd=cwd, dispatch_id=dispatch_id)


def _launch(out, bundle, prompt_file, task_id, judge, *, cwd=None, dispatch_id=None):
    try:
        interactive(bundle, judge)  # validate before a terminal or durable launch intent exists
        hc('server', 'status', '--json')  # non-starting on Windows; upstream may autostart on Unix
        observed = terminal_from(hc('get', out['parent_address'], '--json'))
        if not isinstance(observed, dict) or observed.get('state') != 'live':
            raise ValueError('Herdr parent terminal is not live; nothing launched.')
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return dict(out, launched=False, dispatch_mode='herdr', error=str(exc),
                    next='Resolve the Herdr availability or routed bundle error, then roll again.'), 2
    identity = '\0'.join([os.environ.get('HC_SESSION', 'default'), out['parent_id'], task_id])
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'{key}.json'
    if path.exists():
        return receipt_result(out, json.loads(path.read_text(encoding='utf-8')), path)
    landing = cwd or str(Path.cwd())
    request = {'version': 1, 'state': 'launching', 'name': 'ac-' + key,
               'parent': out['parent_address'], 'bundle': bundle, 'cwd': landing,
               'prompt_file': str(prompt_file), 'hc_session': os.environ.get('HC_SESSION', 'default'),
               **({'dispatch_id': dispatch_id} if dispatch_id else {})}
    save(path, request)  # durable intent before the external send, under the process lock
    try:
        reply = hc('run', '--bg', '--json', '--name', request['name'], '--cwd', request['cwd'],
                   '--parent', request['parent'], *entry.argv('herdr', 'worker', '--request', path), timeout=40)
        request['launch_reply'] = reply
        terminal = terminal_from(reply)
        if not isinstance(terminal, dict) or not terminal.get('address'):
            raise ValueError('hc launch response has no terminal ID; inspect the named terminal before retrying.')
        request.update(state='launched', terminal=terminal['address'])
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # Even a nonzero run may have created a terminal before its reply was lost.
        request.update(state='unknown', error=str(exc))
    save(path, request)
    return receipt_result(out, request, path)


def load_request(path):
    request = json.loads(path.read_text(encoding='utf-8'))
    if request.get('version') != 1 or not request.get('name', '').startswith('ac-'):
        raise ValueError('Not an AgentCompass Herdr request.')
    if request.get('hc_session') != os.environ.get('HC_SESSION', 'default'):
        raise ValueError('This request belongs to a different HC_SESSION; use its original Herdr core.')
    return request


def inspect_request(path, cleanup=False):
    with receipt_lock():
        return _inspect_request(path, cleanup)


def rpc_read(target):
    """CLI read is text; the official RPC retains completeness/retention metadata."""
    proc = subprocess.Popen([shutil.which('hc'), 'connect'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    timer = threading.Timer(40, proc.kill)
    timer.start()
    try:
        # Keep stdin open until the response; EOF closes the hc transport immediately.
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'terminal.read',
                                    'params': {'terminal': target, 'lines': 1000000}}) + '\n')
        proc.stdin.flush()
        for line in proc.stdout:
            reply = json.loads(line)
            if reply.get('id') == 1:
                if 'result' in reply:
                    return reply['result']
                raise RuntimeError('Herdr read failed: ' + json.dumps(reply.get('error')))
        raise RuntimeError('Herdr read response unavailable; collect the full report artifact via hc send.')
    finally:
        timer.cancel()
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _inspect_request(path, cleanup=False):
    request = load_request(path)
    if request['state'] == 'cleaned':
        return {'status': 'cleaned', 'request': str(path),
                'next_action': 'This child was cleaned. Use a new task assignment to schedule new work.'}
    target = request.get('terminal') or request['name']
    term = terminal_from(hc('get', target, '--json'))
    if term.get('name') != request['name'] or term.get('parent') != request['parent']:
        raise ValueError('Herdr terminal ownership does not match this launch receipt.')
    program = term.get('status') or {}
    state = program.get('state')
    if term.get('state') == 'finished':
        state = 'completed' if term.get('exit_code') == 0 else 'failed'
    else:
        state = {'done': 'completed', 'blocked': 'needs_input', 'error': 'failed'}.get(state, 'launched')
    request.update(state=state, terminal=term['address'], observed=term)
    if cleanup:
        if term.get('state') == 'live':
            hc('kill', target, '--json')
        request['cleanup_reply'] = hc('rm', target, '--json')
        request['state'] = 'cleaned'
    else:
        read = rpc_read(target)
        output = path.with_suffix('.output.txt')
        output.write_bytes(read['text'].encode('utf-8'))
        save(path.with_suffix('.read.json'), read)
        request['output'] = str(output)
        request['output_complete'] = read.get('complete', False)
    save(path, request)
    if request.get('dispatch_id') and request['state'] in ('completed', 'failed', 'cleaned'):
        import dispatch_registry as registry
        target_state = ('completed' if request['state'] == 'completed'
                        else 'failed' if request['state'] == 'failed' else 'canceled')
        try:
            registry.settle_host_claim(request['dispatch_id'], target_state, f"herdr:{request['state']}")
        except OSError:
            pass
    return {'status': request['state'], 'terminal': request.get('terminal'), 'observed': term,
            'worker_output': request.get('output'), 'request': str(path),
            'output_complete': request.get('output_complete'),
            'next_action': ('Read worker_output and the report sent via hc send. '
                            'Terminal history can be retained only partially; if output_complete is false, '
                            'collect the full report artifact from the worker. Clean up this child when accepted.')}


def worker(path):
    request = load_request(path)
    if not os.environ.get('HC_TERMINAL_ID'):
        raise ValueError('Herdr worker must run inside its hc child terminal.')
    import route_judge as judge
    argv, provider_env = interactive(request['bundle'], judge)
    # The core's environment is not this routed account. Resolve account env here,
    # inside the child; do not persist credentials or inject caller session IDs.
    env = {key: value for key, value in judge.subscription_env().items()
           if not key.startswith(('CLAUDE', 'CODEX_', 'PASEO_', 'ORCA_', 'T3_', 'CINDY_', 'ROUTER_PARENT_'))}
    env.update(provider_env)
    env.update(ROUTER_WORKER='1', ROUTER_PARENT_AGENT_ID=request['parent'],
               ROUTER_PARENT_HOST='herdr', ROUTER_PARENT_ADDRESS=request['parent'])
    # Bound argv size while preserving every byte of the full assignment on disk.
    bootstrap = ('Read the entire UTF-8 assignment file ' + json.dumps(request['prompt_file']) +
                 ' and carry it out. The file includes the full task and parent return contract. '
                 'Do not delegate. Send your questions and final report with hc send ' + request['parent'] + '.')
    os.chdir(request['cwd'])
    if os.name == 'nt':
        # CRT exec does not provide Unix process replacement semantics. Keep
        # hc's supervised process alive for the actual interactive worker.
        process = subprocess.Popen([*argv, bootstrap], env=env)
        try:
            return process.wait()
        except BaseException:
            judge.stop_process(process)
            raise
    os.execvpe(argv[0], [*argv, bootstrap], env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog='agentcompass herdr')
    parser.add_argument('action', choices=('status', 'cleanup', 'worker'))
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'worker':
            return worker(args.request)
        result = inspect_request(args.request, args.action == 'cleanup')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc),
                          'next_action': 'Inspect the original request/core; do not resend an uncertain launch.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
