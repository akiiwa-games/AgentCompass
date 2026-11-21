"""Locate a provider's subscription CLI and run it on a routed account, with no inherited API key or proxy."""
import os
from pathlib import Path
import shutil
import signal
import subprocess

import launch_values
import provider_inventory


def cli_prefix(provider):
    command = shutil.which(provider)
    if not command and provider == 'grok':
        # The official installer puts Grok Build in ~/.grok/bin without touching PATH; discovery counts that install.
        installed = Path.home() / '.grok' / 'bin' / ('grok.exe' if os.name == 'nt' else 'grok')
        command = str(installed) if installed.is_file() else None
    if not command:
        raise RuntimeError('Subscription CLI is not installed.')
    return direct_prefix(command)


def direct_prefix(command):
    """The argv prefix that starts `command` with no cmd.exe in between (launch_values.program: an npm shim becomes the
    program and script it runs). A launcher that would need variables of its own set for the program is refused, as
    one that cannot be read is (launch_values.UnreadableLauncher)."""
    argv, env = launch_values.program(command)
    if env:
        raise launch_values.UnreadableLauncher(
            f'{command} sets {", ".join(sorted(env))} for the program it runs, which this launch does not carry; point '
            'the command at the executable it runs')
    return argv


def subscription_env(account=None):
    env = os.environ.copy()
    # The scorer's CLI runs and the workers launched without Paseo (roll's CLI path, Herdr) use subscriptions, never
    # an inherited API key or proxy destination.
    for name in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                 'CLAUDE_CODE_USE_FOUNDRY'):
        env.pop(name, None)
    discovered = ((provider_inventory.current().get('accounts') or {}).get(account) or {}) if account else {}
    if discovered.get('family') == 'claude' and discovered.get('home'):
        cfg = str(discovered['home'])
        env['CLAUDE_CONFIG_DIR'] = cfg
        env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = cfg
        env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
    elif discovered.get('family') == 'codex' and discovered.get('home'):
        env['CODEX_HOME'] = str(discovered['home'])
    return env


def stop_process(process):
    if process.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def run_process(command, cwd, prompt=None, timeout=120, account=None):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} \
        if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(command, cwd=cwd, env=subscription_env(account),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace', **options)
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
        return process.returncode, stdout, stderr
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        stop_process(process)
        process.communicate(timeout=10)
        raise RuntimeError('Routing CLI timed out or was interrupted; owned process stopped.') from None
