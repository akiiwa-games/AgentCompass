"""Integration card actions for the repository-scoped Grok Bot CLI; no service/auth store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from i18n import translate as _tr

ROOT = Path(__file__).resolve().parents[1] / 'skills' / 'route-agent-work'
PACKAGE = ROOT / 'grok-bot'
RUNNER = ROOT / 'scripts' / 'run_grok_bot.py'
GUIDE = ROOT / 'references' / 'grok-bot.md'
_connection = None
_probe_lock = threading.Lock()


def _run(argv, timeout):
    return subprocess.run(argv, cwd=PACKAGE, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          creationflags=0x08000000 if os.name == 'nt' else 0)


def snapshot():
    """Local installation and last result; the active page owns check scheduling."""
    node = shutil.which('node')
    if not node:
        return {'label': _tr('缺少 Node.js'), 'kind': 'warn'}
    if not (PACKAGE / 'node_modules/grok-bot-cli/dist/bin/gbot.mjs').is_file():
        return {'label': _tr('CLI 未安装'), 'kind': 'idle'}
    try:
        valid = _run([node, str(PACKAGE / 'patch-descriptor-v3.mjs'), '--check'], 15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        valid = False
    if not valid:
        return {'label': _tr('CLI 需要修复'), 'kind': 'warn'}
    if _connection:
        age = time.monotonic() - _connection[0]
        return {'label': _tr('已安装 · 连接可用') if _connection[1] else _tr('已安装 · 连接不可用'),
                'kind': 'ok' if _connection[1] else 'warn',
                'due': age >= 60, 'retry_in': max(1, 60 - age), 'installed': True}
    return {'label': _tr('已安装 · 连接未检查'), 'kind': 'idle', 'due': True, 'installed': True}


def install():
    with _probe_lock:
        return _install()


def _install():
    global _connection
    import updates

    _connection = None
    npm = shutil.which('npm')
    if not npm or not shutil.which('node'):
        return _tr('请安装 Node.js 22.19 或更新版本（含 npm），再安装/修复。')
    # The lockfile owns the version; lifecycle scripts cannot run before our reviewed patch.
    updates.run([npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], PACKAGE)
    updates.run([npm, 'run', 'patch:descriptor-v3'], PACKAGE)
    return _tr('Grok Bot CLI 已安装；正在自动检查连接。账号启停在调度设置中管理。')


def check(force=True):
    global _connection
    if not _probe_lock.acquire(blocking=False):
        return info()
    try:
        installed = snapshot()
        if not installed.get('installed'):
            return installed['label']
        if not force and not installed.get('due'):
            return info()
        reason = _tr('网关检查失败；原因尚未确定。')
        try:
            python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
            result = _run([python, '-X', 'utf8', str(RUNNER), 'doctor'], 90)
            value = json.loads(result.stdout)
            ready = result.returncode == 0 and isinstance(value, dict) and value.get('status') == 'ready'
            diagnostic = value.get('diagnostic', {}) if isinstance(value, dict) else {}
            if (isinstance(diagnostic, dict) and diagnostic.get('category') == 'http_error'
                    and diagnostic.get('operation') == 'listAgents'
                    and type(diagnostic.get('status')) is int and 400 <= diagnostic['status'] <= 599):
                reason = _tr('网关 listAgents 返回 HTTP {0}；原因尚未确定。').format(diagnostic['status'])
        except subprocess.TimeoutExpired:
            ready, reason = False, _tr('网关检查超时。')
        except (OSError, ValueError):
            ready = False
        _connection = (time.monotonic(), ready, '' if ready else reason)
        return info()
    finally:
        _probe_lock.release()


def guide():
    os.startfile(GUIDE)
    return _tr('已打开 Grok Bot CLI 使用说明。')


def info():
    text = _tr('此页打开时自动检查连接；检查期间保留上次结果。账号启停在调度设置中管理。')
    if _connection and not _connection[1]:
        text += ' ' + _connection[2]
    return text
