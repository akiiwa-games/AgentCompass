"""Integration card actions for the repository-scoped Grok Bot CLI; no service/auth store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from i18n import translate as _tr

ROOT = Path(__file__).resolve().parents[1] / 'skills' / 'route-agent-work'
PACKAGE = ROOT / 'grok-bot'
RUNNER = ROOT / 'scripts' / 'run_grok_bot.py'
GUIDE = ROOT / 'references' / 'grok-bot.md'
_connection = None


def _run(argv, timeout):
    return subprocess.run(argv, cwd=PACKAGE, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          creationflags=0x08000000 if os.name == 'nt' else 0)


def snapshot():
    """Local installation plus a short-lived explicit check; never poll the gateway."""
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
    if _connection and time.monotonic() - _connection[0] < 60:
        return {'label': _tr('已安装 · 连接可用') if _connection[1] else _tr('已安装 · 连接不可用'),
                'kind': 'ok' if _connection[1] else 'warn',
                'expires_in': 60 - (time.monotonic() - _connection[0])}
    return {'label': _tr('已安装 · 连接未检查'), 'kind': 'idle'}


def install():
    global _connection
    import updates

    _connection = None
    npm = shutil.which('npm')
    if not npm or not shutil.which('node'):
        return _tr('请安装 Node.js 22.19 或更新版本（含 npm），再安装/修复。')
    # The lockfile owns the version; lifecycle scripts cannot run before our reviewed patch.
    updates.run([npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], PACKAGE)
    updates.run([npm, 'run', 'patch:descriptor-v3'], PACKAGE)
    return _tr('Grok Bot CLI 已安装；请检查连接。账号启停在调度设置中管理。')


def check():
    global _connection
    _connection = None
    installed = snapshot()
    if installed['kind'] != 'idle' or not (PACKAGE / 'node_modules/grok-bot-cli/dist/bin/gbot.mjs').is_file():
        return installed['label']
    try:
        python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
        result = _run([python, '-X', 'utf8', str(RUNNER), 'doctor'], 90)
        value = json.loads(result.stdout)
        ready = result.returncode == 0 and isinstance(value, dict) and value.get('status') == 'ready'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        ready = False
    _connection = (time.monotonic(), ready)
    # Never surface arbitrary subprocess output or roster/account identifiers in this card.
    return (_tr('CLI 已安装，网关连接可用；任务 Bot 配置请见使用说明。') if ready else
            _tr('CLI 已安装，网关连接不可用。请在官方应用确认云电脑连接，再检查连接；这不表示需要重新登录。'))


def guide():
    os.startfile(GUIDE)
    return _tr('已打开 Grok Bot CLI 使用说明。')


def info():
    return _tr('按需运行，无常驻服务；账号启停在调度设置中管理。连接结果仅保留一分钟。')
