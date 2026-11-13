"""Locate a provider's subscription CLI and run it on a routed account, with no inherited API key or proxy."""
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess

import provider_inventory


def cli_prefix(provider):
    command = shutil.which(provider)
    if not command and provider == 'grok':
        # The official installer puts Grok Build in ~/.grok/bin without touching PATH; discovery counts that install.
        installed = Path.home() / '.grok' / 'bin' / ('grok.exe' if os.name == 'nt' else 'grok')
        command = str(installed) if installed.is_file() else None
    if not command:
        raise RuntimeError('Subscription CLI is not installed.')
    path = Path(command)
    if os.name != 'nt' or path.suffix.lower() == '.exe' or provider not in ('claude', 'codex'):
        return [str(path)]
    # Resolve npm shims to native executables; never interpolate a shell command.
    npm = path.parent / 'node_modules'
    if provider == 'claude':
        native = npm / '@anthropic-ai' / 'claude-code' / 'bin' / 'claude.exe'
    else:
        arm = platform.machine().lower() in ('arm64', 'aarch64')
        package = 'codex-win32-arm64' if arm else 'codex-win32-x64'
        target = 'aarch64-pc-windows-msvc' if arm else 'x86_64-pc-windows-msvc'
        root = npm / '@openai' / 'codex'
        native = root / 'node_modules' / '@openai' / package / 'vendor' / target / 'bin' / 'codex.exe'
        if not native.is_file():
            native = root / 'vendor' / target / 'bin' / 'codex.exe'
    if not native.is_file():
        raise RuntimeError('Resolve the installed subscription CLI executable.')
    return [str(native)]


def subscription_env(account=None):
    env = os.environ.copy()
    # The judge uses subscriptions, never an inherited API key or proxy destination.
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
