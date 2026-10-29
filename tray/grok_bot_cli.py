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
from i18n import tr
import component_launch

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


def python():
    if component_launch.frozen():
        raise RuntimeError(frozen_message())
    return str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable


def frozen_message():
    return tr('安装包暂不支持 Grok Bot CLI 管理；此功能需要外部 Python 和路由技能源码。请在已安装 Python 的终端中使用 agentcompass 路由 CLI。',
              'Grok Bot CLI management needs external Python and the router skill source. Use the agentcompass router CLI in a terminal with Python installed.')


def snapshot():
    """Local installation and last result; the active page owns check scheduling."""
    if component_launch.frozen():
        return {'label': frozen_message(), 'kind': 'warn'}
    node = shutil.which('node')
    if not node:
        return {'label': _tr('缺少 Node.js'), 'kind': 'warn'}
    if not (PACKAGE / 'runtime/bin/gbot.mjs').is_file():
        return {'label': _tr('CLI 未安装'), 'kind': 'idle'}
    try:
        valid = _run([python(), str(PACKAGE / 'install_cli.py'), '--check'], 15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        valid = False
    if not valid:
        return {'label': _tr('CLI 需要修复'), 'kind': 'warn'}
    if _connection:
        return {'label': _tr('已安装 · 连接可用') if _connection[1] else _tr('已安装 · 连接不可用'),
                'kind': 'ok' if _connection[1] else 'warn',
                'installed': True}
    return {'label': _tr('已安装 · 连接未检查'), 'kind': 'idle', 'installed': True}


def install():
    with _probe_lock:
        return _install()


def _install():
    global _connection
    import updates

    _connection = None
    if component_launch.frozen():
        return frozen_message()
    if not shutil.which('node'):
        return _tr('请安装 Node.js 22.19 或更新版本，再安装/修复。')
    updates.run([python(), str(PACKAGE / 'install_cli.py')], PACKAGE)
    return _tr('Grok Bot CLI 已安装；正在自动检查连接。账号启停在模型设置中管理。')


def check():
    global _connection
    if not _probe_lock.acquire(blocking=False):
        return info()
    try:
        installed = snapshot()
        if not installed.get('installed'):
            return installed['label']
        reason = _tr('网关检查失败；原因尚未确定。')
        try:
            result = _run([python(), '-X', 'utf8', str(RUNNER), 'doctor'], 240)
            value = json.loads(result.stdout)
            ready = result.returncode == 0 and isinstance(value, dict) and value.get('status') == 'ready'
            diagnostic = value.get('diagnostic', {}) if isinstance(value, dict) else {}
            if (isinstance(diagnostic, dict) and diagnostic.get('category') == 'http_error'
                    and diagnostic.get('operation') in ('listAgents', 'EnsureSandBox')
                    and type(diagnostic.get('status')) is int and 400 <= diagnostic['status'] <= 599):
                reason = _tr('网关 {0} 返回 HTTP {1}；原因尚未确定。').format(diagnostic['operation'], diagnostic['status'])
            elif (isinstance(diagnostic, dict) and diagnostic.get('category') == 'local_runtime'
                  and diagnostic.get('component') in ('pywin32', 'process_ownership')):
                reason = _tr('Windows 进程管理不可用；请修复 Route Manager 的 pywin32 运行环境。')
        except subprocess.TimeoutExpired:
            ready, reason = False, _tr('网关检查超时。')
        except (OSError, ValueError):
            ready = False
        _connection = (time.monotonic(), ready, '' if ready else reason, time.strftime('%H:%M:%S'))
        return info()
    finally:
        _probe_lock.release()


def guide():
    if not GUIDE.is_file():
        return frozen_message()
    os.startfile(GUIDE)
    return _tr('已打开 Grok Bot CLI 使用说明。')


def info():
    text = _tr('进入此页时检查一次，可按需唤醒云电脑；不会定时唤醒。账号启停在模型设置中管理。')
    if _connection and len(_connection) > 3:
        text += ' ' + _tr('上次检查：{0}').format(_connection[3])
    if _connection and not _connection[1]:
        text += ' ' + _connection[2]
    return text
