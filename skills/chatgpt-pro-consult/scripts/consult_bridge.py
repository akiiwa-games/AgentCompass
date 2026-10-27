"""Advice-only client for the isolated codex-chatgpt-web Browser-only bridge.

Only the selected UTF-8 prompt is sent. No Codex harness, OAuth file or tools.
Uses the public local Responses endpoint, never ChatGPT private endpoints.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error
import uuid

# Configuration comes from the environment; nothing about a particular installation is built in.
HOME_ENV = 'PRO_CONSULT_HOME'            # required: the bridge's isolated home with config.json
LAUNCHER_ENV = 'PRO_CONSULT_LAUNCHER'    # optional: starts the bridge with its isolated profile
LAUNCHER_ARGS_ENV = 'PRO_CONSULT_LAUNCHER_ARGS'
START_WAIT_SECONDS = 90
# The legacy /pro alias leaves the browser model family unpinned. Use the skill's
# declared GPT-6 Pro target; the bridge verifies model family and Pro before send.
MODEL = 'chatgpt-web/gpt-6-pro'
INSTRUCTIONS = ('Provide consultation and planning advice only. Do not execute actions or '
                'request tools. Separate supplied facts, assumptions and inferences. Compare '
                'meaningful alternatives, identify failure cases and unresolved questions, '
                'and suggest verification steps. The parent agent owns implementation and decisions.')


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def user_env(name):
    """Process environment, falling back to the Windows user environment for older processes."""
    value = os.environ.get(name)
    if not value and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                value = winreg.QueryValueEx(key, name)[0]
        except OSError:
            pass
    return os.path.expandvars(value) if value else None


def bridge_home():
    value = user_env(HOME_ENV)
    if not value:
        raise ValueError(f'{HOME_ENV} is not configured; set it to the bridge home containing config.json.')
    return Path(value).expanduser()


def connection(home):
    config = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    if config.get('mode') != 'browser-only' or config.get('browserInteractionMode') != 'automatic':
        raise ValueError('The consultation bridge must be automatic Browser-only mode.')
    if config.get('host') != '127.0.0.1' or not isinstance(config.get('port'), int):
        raise ValueError('Expected an isolated loopback bridge.')
    base = f"http://127.0.0.1:{config['port']}"
    with urllib.request.urlopen(base + '/healthz', timeout=5) as response:
        health = json.load(response)
    if health.get('service') != 'codex-chatgpt-web' or health.get('mode') != 'browser-only':
        raise ValueError('Unexpected bridge service or tools-enabled mode.')
    if not health.get('accepting_turns', False):
        raise ValueError('Bridge is draining; no request submitted.')
    if not config.get('proAvailable'):
        raise ValueError('Setup has not detected Pro; refresh account capabilities after login.')
    return base, health


def launcher_command():
    value = user_env(LAUNCHER_ENV)
    if not value:
        return None
    launcher = Path(value).expanduser()
    if not launcher.is_file():
        return None
    extra = shlex.split(user_env(LAUNCHER_ARGS_ENV) or '', posix=os.name != 'nt')
    if launcher.suffix.lower() == '.ps1':
        return ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                '-File', str(launcher), *extra]
    return [str(launcher), *extra]


def ensure_connection(home):
    """Connect, starting the bridge through its configured launcher if it is not running."""
    try:
        return connection(home)
    except urllib.error.URLError:
        command = launcher_command()
        if not command:
            raise
    # Only the configured launcher carries the isolated profile; the upstream app's default
    # shortcut can open a different, logged-out profile.
    print(json.dumps({'status': 'starting_bridge', 'launcher': user_env(LAUNCHER_ENV)}), file=sys.stderr, flush=True)
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            return connection(home)
        except urllib.error.URLError:
            continue
    raise ValueError(f'Started the bridge launcher, but the bridge was not ready within {START_WAIT_SECONDS}s. '
                     'If it needs login, ask the user to sign in through the bridge\'s own profile. '
                     'Do not start the upstream app directly.')


def answer_text(response):
    if response.get('status') != 'completed':
        raise ValueError('No completed response received.')
    messages = [item for item in response.get('output', []) if item.get('type') == 'message']
    if any(item.get('type') not in ('message', 'reasoning') for item in response.get('output', [])):
        raise ValueError('Unexpected non-advisory output; no tool is executed.')
    final = [item for item in messages if item.get('phase') == 'final_answer'] or messages
    text = '\n\n'.join(part['text'] for item in final for part in item.get('content', [])
                       if part.get('type') == 'output_text' and isinstance(part.get('text'), str))
    if not text.strip():
        raise ValueError('Completed response has no answer text.')
    return text


def receive(stream, directory, state):
    """Persist SSE before interpretation; EOF/deltas alone never count as success."""
    with (directory / 'events.sse').open('wb') as log:
        data = []
        for raw in stream:
            log.write(raw)
            log.flush()
            line = raw.decode('utf-8').rstrip('\r\n')
            if line.startswith('data:'):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = '\n'.join(data)
                data = []
                if payload == '[DONE]':
                    continue
                event = json.loads(payload)
                kind = event.get('type', '')
                response = event.get('response', {})
                if response.get('id'):
                    state['response_id'] = response['id']
                    save(directory / 'state.json', state)
                if kind in ('response.failed', 'response.incomplete', 'error'):
                    save(directory / 'terminal.json', event)
                    raise ValueError('Bridge returned ' + kind + '; inspect terminal.json, do not resend automatically.')
                if kind == 'response.completed':
                    save(directory / 'response.json', response)
                    return response
        raise ValueError('Stream ended without response.completed; submission state is uncertain.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prompt', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    base, health = ensure_connection(bridge_home())
    if args.check:
        print(json.dumps({'ready': True, 'mode': 'browser-only', 'model': MODEL,
                          'version': health.get('version'), 'port': health.get('port'),
                          'model_evidence': 'account capability; verify exact live model label separately'}))
        return
    if not args.prompt or not args.out:
        parser.error('--prompt and --out are required for a consultation')
    prompt = args.prompt.read_text(encoding='utf-8-sig')
    if not prompt.strip():
        raise ValueError('Prompt is empty.')
    # Exclusive creation prevents accidental duplicate submission of the same run.
    args.out.mkdir(parents=True, exist_ok=False)
    # The Windows app can leave a newly acquired BrowserView at 0x0 after settings/reload.
    # Restore only its observed Browser navigation before creating or sending this request.
    descriptor = bridge_home() / 'runtime' / 'launcher-browser.json'
    if os.name == 'nt' and descriptor.is_file():
        helper = Path(__file__).with_name('browser_surface_preflight.mjs')
        preflight = subprocess.run(['node', str(helper), '--home', str(bridge_home()),
                                    '--out', str(args.out / 'browser_surface_preflight.json'),
                                    '--base', base],
                                   capture_output=True, timeout=25,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if preflight.returncode != 0:
            save(args.out / 'state.json', {'status': 'ui_preflight_failed',
                 'submission_attempted': False, 'UI_submitted': False,
                 'created_at': datetime.now(timezone.utc).isoformat(),
                 'recovery': 'Inspect the observed bridge Browser surface; no request was sent.'})
            raise ValueError('Browser surface restoration failed before submission; inspect the UI. '
                             'No login, model substitution or automatic retry was attempted.')
    request_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    payload = {'model': MODEL, 'reasoning': {'effort': 'max'}, 'instructions': INSTRUCTIONS,
               'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}],
                          'internal_chat_message_metadata_passthrough': {'turn_id': turn_id}}],
               'tools': [], 'tool_choice': 'none', 'stream': True,
               'store': False, 'prompt_cache_key': 'consult-' + request_id,
               # Protocol lifecycle IDs belong to this consultation, not the parent Codex task.
               # No workspace/sandbox authority is supplied or needed in Browser-only mode.
               'client_metadata': {'x-codex-turn-metadata': json.dumps({
                   'thread_id': 'consult-' + request_id, 'turn_id': turn_id})}}
    save(args.out / 'request.json', payload)
    state = {'status': 'prepared', 'request_id': request_id,
             'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
             'created_at': datetime.now(timezone.utc).isoformat(),
             'requested_model': MODEL, 'observed_model': None}
    save(args.out / 'state.json', state)
    req = urllib.request.Request(base + '/v1/responses', data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
    state['status'] = 'submission_attempted'
    save(args.out / 'state.json', state)
    print(json.dumps({'status': state['status'], 'out': str(args.out)}), flush=True)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as stream:
            result = receive(stream, args.out, state)
        text = answer_text(result)
        (args.out / 'response.md').write_text(text, encoding='utf-8')
        state.update(status='completed', response_id=result.get('id'), returned_model=result.get('model'))
    except BaseException as exc:
        if isinstance(exc, urllib.error.HTTPError):
            (args.out / 'http-error.txt').write_bytes(exc.read(65536))
        state.update(status='unresolved', error_type=type(exc).__name__)
        save(args.out / 'state.json', state)
        raise
    save(args.out / 'state.json', state)
    print(json.dumps({'status': 'completed', 'response': str(args.out / 'response.md'),
                      'model_evidence': 'Inspect live model before claiming exact GPT version.'}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
