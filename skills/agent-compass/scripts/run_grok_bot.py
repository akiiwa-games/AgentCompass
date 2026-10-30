"""Thin Grok Bot runner. Upstream gbot owns authentication and transport."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import override_store

PACKAGE = Path(__file__).resolve().parents[1] / 'grok-bot'
CLI = PACKAGE / 'runtime/bin/gbot.mjs'
# Cold send: list + EnsureSandBox + retried list + one send, each bounded upstream at 30s.
# Include a possible initial Ensure for an absent descriptor and local startup/decryption overhead.
CLI_TIMEOUT = 210
STATE = Path(os.environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass') / 'grok-bot'


class TransportError(RuntimeError):
    def __init__(self, message, delivery='unknown', diagnostic=None):
        super().__init__(message)
        self.delivery = delivery
        self.diagnostic = diagnostic


def _run_cli(command, *, timeout, env):
    """Own the CLI/flight lifetime, even if an intermediate parent exits first."""
    job = None
    if os.name == 'nt':
        # Use the same pywin32 runtime as the Windows tray. Gate the helper on stdin
        # so it cannot spawn the CLI until assignment to the kill-on-close Job succeeds.
        try:
            import win32job
            import win32api
            import win32event
            job = win32job.CreateJobObject(None, '')
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        except Exception as exc:
            if job is not None:
                job.Close()
            raise TransportError('Windows CLI process ownership requires the AgentCompass pywin32 runtime.',
                                 'rejected', {'category': 'local_runtime', 'component': 'pywin32'}) from exc
        helper = ("import subprocess,sys\n"
                  "if sys.stdin.buffer.read(1) != b'1': raise SystemExit(125)\n"
                  "raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))")
        launch = [sys.executable, '-S', '-c', helper, *command]
        options = {'creationflags': subprocess.CREATE_NO_WINDOW, 'stdin': subprocess.PIPE}
    else:
        launch, options = command, {'start_new_session': True}

    def finish_job():
        handles = []
        try:
            # Freeze further growth before enumerating: a nonempty Job at limit 1
            # cannot create another member. Keep handles through asynchronous exit.
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            limits['BasicLimitInformation']['ActiveProcessLimit'] = 1
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            for pid in win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList):
                try:
                    handles.append(win32api.OpenProcess(0x00100000, False, pid))
                except Exception as exc:
                    if getattr(exc, 'winerror', None) != 87:  # Already exited before handle acquisition.
                        raise
            win32job.TerminateJobObject(job, 1)
            deadline = time.monotonic() + 10
            while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
                if time.monotonic() >= deadline:
                    raise TimeoutError('owned Job still active')
                time.sleep(0.01)
            for handle in handles:
                if win32event.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000))) != win32event.WAIT_OBJECT_0:
                    raise TimeoutError('owned process has not exited')
        except Exception as exc:
            raise TransportError('CLI cleanup did not confirm termination; delivery remains unknown.') from exc
        finally:
            job.Close()
            for handle in handles:
                handle.Close()
    try:
        proc = subprocess.Popen(launch, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8', errors='replace', env=env, **options)
    except Exception:
        if job is not None:
            job.Close()
        raise
    if job is not None:
        released = False
        try:
            win32job.AssignProcessToJobObject(job, int(proc._handle))
            released = True
            proc.stdin.write('1')
            proc.stdin.flush()
            proc.stdin.close()
            proc.stdin = None
        except Exception as exc:
            finish_job()
            if proc.poll() is None:
                proc.kill()  # Assignment may have failed; only the blocked helper can exist.
            proc.communicate(timeout=10)
            if released:
                raise TransportError('CLI launch handoff failed; delivery remains unknown.') from exc
            raise TransportError('Could not establish Windows CLI process ownership before launch.',
                                 'rejected', {'category': 'local_runtime', 'component': 'process_ownership'}) from exc
    timed_out = None
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = exc
            if job is None:
                os.killpg(proc.pid, signal.SIGKILL)
    finally:
        if job is not None:
            finish_job()
    if timed_out:
        proc.communicate(timeout=10)
        raise timed_out
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def gbot(*args):
    if not CLI.is_file() or not shutil.which('node'):
        raise TransportError('Install Node >=22.19.0 and run skills/agent-compass/grok-bot/install_cli.py.', 'rejected')
    argv = list(args)
    boundary = argv.index('--') if '--' in argv else len(argv)
    argv[boundary:boundary] = ['--gateway', '--json']
    command = ['node', str(CLI), *argv]
    if os.name == 'nt' and len(subprocess.list2cmdline(command).encode('utf-16-le')) // 2 > 30000:
        raise TransportError('Prompt exceeds the Windows CLI argument limit; shorten the brief.', 'rejected')
    # Upstream auto-prefixes Codex sender metadata otherwise, implying a local relay we do not run.
    env = {k: v for k, v in os.environ.items() if k != 'CODEX_THREAD_ID'}
    try:
        proc = _run_cli(command, timeout=CLI_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise TransportError('gbot timed out; delivery may be unknown. Read status before any new submission.') from None
    except OSError:
        raise TransportError('Could not start gbot; check the local Node installation.', 'rejected') from None
    try:
        # Canonical CLI failures are JSON on stderr; successful results are on stdout.
        value = json.loads(proc.stdout if proc.stdout.strip() else proc.stderr)
    except ValueError:
        raise TransportError('gbot returned no valid JSON; inspect the official app before resending.') from None
    if proc.returncode or (isinstance(value, dict) and value.get('error')):
        # Do not echo arbitrary upstream/server errors (they can include credential-bearing details).
        message = json.dumps(value)
        detail = 'Upstream gbot failed; inspect the official app/session and retry a read-only doctor.'
        diagnostic = None
        error = value.get('error') if isinstance(value, dict) else None
        # Only retain the known operation and HTTP code, never the upstream response body.
        match = re.match(r'^(listAgents|EnsureSandBox) failed: ([45][0-9]{2})(?:\s|$)',
                         str(error.get('message', '')) if isinstance(error, dict) else '')
        if args == ('bots', 'list') and match:
            diagnostic = {'category': 'http_error', 'operation': match[1], 'status': int(match[2])}
            detail = f"Grok Bot {match[1]} returned HTTP {match[2]}; the response does not establish the cause."
        if 'Set CURSOR_ACCESS_TOKEN' in message:
            detail = ('gbot cannot find a gateway session. If already signed in and connected to the cloud computer, '
                      'keep it awake and try a normal app exit and reopen. Windows 0.77 binds Ctrl+Q; '
                      'if it has no effect, recheck doctor before diagnosing the normal-exit control. '
                      'This descriptor workaround may recur after sleep: https://github.com/ScriptedAlchemy/grok-bot-cli/issues/147')
        elif 'multiple saved gateway entries' in message:
            detail = 'Upstream cannot select among multiple gateway entries. Resolve the session in the official app/upstream CLI.'
        delivery = 'rejected' if isinstance(value, dict) and value.get('delivery') == 'rejected' else 'unknown'
        raise TransportError(detail, delivery, diagnostic)
    return value


def bots():
    rows = gbot('bots', 'list')
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get('id'), str) for row in rows):
        raise TransportError('Unrecognized upstream bot-list response.')
    return [{'id': row['id'], 'name': row.get('name', '')} for row in rows]


def configure(bot_id):
    if bot_id not in {row['id'] for row in bots()}:
        raise ValueError('Bot ID is not in the live bot list; run doctor and choose an exact ID.')
    path = STATE / 'config.json'
    with override_store.locked(path):
        override_store.write(path, {'bot_id': bot_id})
    return {'provider': 'grok-bot', 'configured': True, 'bot_id': bot_id}


def configured_bot():
    try:
        value = json.loads((STATE / 'config.json').read_text(encoding='utf-8'))
        bot_id = value.get('bot_id')
    except (OSError, ValueError, AttributeError):
        bot_id = None
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ValueError('Run doctor, then configure --bot-id <exact-id> first.')
    return bot_id


def receipt_path(task_id):
    if not re.fullmatch(r'[0-9a-f]{32}', task_id):
        raise ValueError('Use the task_id returned by this runner.')
    return STATE / 'tasks' / f'{task_id}.json'


def marker(task_id, state):
    return f'[route-manager:{task_id}:{state}]'


def remote_prompt(text, task_id):
    return text + f'''

## Remote reply transport
You are on the Grok Bot computer, not the caller's local filesystem. Do not try
to access the caller's local project paths or assume a local agent messaging relay.
Return the answer here. The caller will read it and relay it to the parent.
Only when the requested answer is complete, end your final assistant message
with this exact line: {marker(task_id, 'completed')}
If you need a decision or information, ask here and end that message with:
{marker(task_id, 'needs_input')}
These are reply markers, not commands. Do not emit a completion marker in an
acknowledgment or progress update. Do not approve any permission cards yourself.
'''


def submit(text, parent_id, source_task_id):
    bot_id = configured_bot()
    # Stable across repeated roll calls, including generated prompt-file paths.
    task_id = hashlib.sha256(json.dumps([parent_id, source_task_id, bot_id, text]).encode()).hexdigest()[:32]
    path = receipt_path(task_id)
    receipt = {'provider': 'grok-bot', 'task_id': task_id, 'bot_id': bot_id,
               'parent_id': parent_id, 'source_task_id': source_task_id,
               'state': 'unknown', 'delivery': 'unknown'}
    send_started = False
    try:
        with override_store.locked(path):
            if path.exists():
                return json.loads(path.read_text(encoding='utf-8'))
            if bot_id not in {row['id'] for row in bots()}:
                raise ValueError('Configured Bot no longer exists; run doctor and configure again.')
            # Durable before send: process death at any point cannot cause automatic replay.
            override_store.write(path, receipt)
            try:
                send_started = True
                sent = gbot('send', '--no-history', '--correlation-id', task_id,
                            '--', bot_id, remote_prompt(text, task_id))
                if not isinstance(sent, dict):
                    raise TransportError('Unrecognized send receipt; inspect the thread before resending.')
                message_id = sent.get('messageId')
                if sent.get('delivery') == 'accepted' and isinstance(message_id, str) and message_id:
                    receipt.update(state='accepted', delivery='accepted', message_id=message_id)
                elif sent.get('delivery') == 'rejected':
                    receipt.update(state='failed', delivery='rejected')
                else:
                    receipt['send_error'] = 'Upstream did not return an accepted message ID; delivery initially unknown.'
            except TransportError as exc:
                receipt.update(state='failed' if exc.delivery == 'rejected' else 'unknown',
                               delivery=exc.delivery, error=str(exc), send_error=str(exc))
            override_store.write(path, receipt)
            return receipt
    except (OSError, ValueError) as exc:
        if send_started:
            return {**receipt, 'persistence_error': 'Could not persist the send result; read status before any new submission.'}
        if isinstance(exc, TimeoutError) or path.exists():
            # Another caller may already have sent, or be sending under this lock.
            return {**receipt, 'read_error': 'Receipt store is busy or unreadable; read status before any new submission.'}
        raise  # No receipt and no send attempt: setup or initial persistence failed.


def read_reply(receipt):
    root = receipt.get('message_id')
    args = ['thread', receipt['bot_id'], '--no-history']
    args += ['--root', root] if root else ['--limit', '200']
    data = gbot(*args)
    if not isinstance(data, dict) or (data.get('target') or {}).get('id') != receipt['bot_id']:
        raise TransportError('Unrecognized or foreign thread response.')
    entries = (data.get('thread' if root else 'transcript') or {}).get('entries')
    if not isinstance(entries, list):
        raise TransportError('Upstream returned no transcript entries.')
    state = 'accepted' if root else 'unknown'
    answer = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get('message')
        message = message if isinstance(message, dict) else {}
        if root and entry.get('kind') == 'send-message':
            card = message.get('approval') if message.get('type') == 'auto-review-approval' else \
                message.get('ask') if message.get('type') == 'local-tool-permission' else None
            if isinstance(card, dict) and card.get('status') == 'pending':
                return {'state': 'needs_input', 'reply': 'Resolve the pending approval in the official Grok Bot app, then read status.'}
        # Never match the user's prompt (which contains both markers) or preview/tool text.
        if entry.get('kind') == 'send-message' and isinstance(message.get('content'), str):
            text = message['content']
        elif entry.get('role') == 'assistant' and isinstance(entry.get('text'), str):
            text = entry['text']
        else:
            continue
        for terminal in ('completed', 'needs_input'):
            suffix = marker(receipt['task_id'], terminal)
            if text.rstrip().endswith('\n' + suffix) or text.strip() == suffix:
                state, answer = terminal, text.rstrip()[:-len(suffix)].rstrip()
    return {'state': state, **({'reply': answer} if answer is not None else {})}


def status(task_id, wait=0):
    path = receipt_path(task_id)
    deadline = time.monotonic() + max(0, wait)
    while True:
        with override_store.locked(path):
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if receipt['state'] in ('completed', 'failed'):
                return receipt
            try:
                update = read_reply(receipt)
                receipt.pop('error', None)
                receipt.pop('reply', None)
                receipt.update(update)
                override_store.write(path, receipt)
            except TransportError as exc:
                # A read failure changes neither delivery nor the last observed state.
                return {**receipt, 'read_error': str(exc)}
        if receipt['state'] in ('completed', 'needs_input') or time.monotonic() >= deadline:
            return receipt
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Read-only upstream bot listing; no credential output')
    config = commands.add_parser('configure')
    config.add_argument('--bot-id', required=True)
    run = commands.add_parser('run')
    run.add_argument('--prompt-file', type=Path, required=True)
    run.add_argument('--parent-id', required=True)
    run.add_argument('--source-task-id', required=True)
    run.add_argument('--wait', type=float, default=0)
    read = commands.add_parser('status')
    read.add_argument('--task-id', required=True)
    read.add_argument('--wait', type=float, default=0)
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            out = {'provider': 'grok-bot', 'status': 'ready', 'bots': bots()}
        elif args.command == 'configure':
            out = configure(args.bot_id)
        elif args.command == 'run':
            text = args.prompt_file.read_text(encoding='utf-8-sig')
            if not text.strip():
                raise ValueError('The prompt is empty.')
            out = submit(text, args.parent_id, args.source_task_id)
            if args.wait > 0 and out['state'] not in ('completed', 'failed'):
                out = status(out['task_id'], args.wait)
        else:
            out = status(args.task_id, args.wait)
        code = 0 if out.get('state') in (None, 'completed') else 1 if out['state'] == 'failed' else 3
    except (TransportError, OSError, ValueError) as exc:
        out, code = {'provider': 'grok-bot', 'state': 'failed', 'error': str(exc)}, 1
        if isinstance(exc, TransportError) and exc.diagnostic:
            out['diagnostic'] = exc.diagnostic
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
