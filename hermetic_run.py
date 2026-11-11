"""Every pytest run of this repository is hermetic: it never touches the user's real AgentCompass state or processes.

The rootdir conftest.py imports this module before any test module is imported, and the run gets its own home:
HOME / USERPROFILE / LOCALAPPDATA / APPDATA point into a temporary directory, so every state-dir constant the product
derives from them at import (services.STATE_DIR, local_controls.DIRECTORY, plugins.STATE_DIR, router_data.STATE_DIR,
update_status.DIRECTORY, the router-window, cli-updates and theatre logs, usage caches, ...) lands there, and
ROUTER_STATE_DIR and the other state variables a shell may carry are dropped. Child processes inherit the same.

Two guards make a leak fail loudly instead of passing:
- a write, rename or delete under the real ~/.agent-compass or the installed product (%LOCALAPPDATA%/AgentCompass) is
  refused (sys.addaudithook) and fails the test that did it;
- psutil terminate / kill / send_signal / suspend, os.kill, or a taskkill command line, of a process this run did not
  start is refused and fails the test. (2026-11-11: tests sent "quit" to the user's real Usage Float and wrote the real logs.)
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

REAL_HOME = Path(os.path.expanduser('~'))
REAL_LOCALAPPDATA = Path(os.environ.get('LOCALAPPDATA') or REAL_HOME / 'AppData' / 'Local')
PROTECTED = tuple(os.path.normcase(os.path.abspath(p)) for p in (REAL_HOME / '.agent-compass',
                                                                  REAL_LOCALAPPDATA / 'AgentCompass'))
# State locations a shell may carry into the run; under test they come from the temporary home.
STATE_VARIABLES = ('ROUTER_STATE_DIR', 'AGENTCOMPASS_APP_DIR', 'AGENTCOMPASS_CHATGPT_CODEX_HOME',
                   'ROUTE_MANAGER_CHATGPT_CODEX_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR')

TEST_HOME = Path(tempfile.mkdtemp(prefix='agentcompass-test-home-'))
atexit.register(shutil.rmtree, TEST_HOME, True)
for name, path in {'HOME': TEST_HOME, 'USERPROFILE': TEST_HOME, 'LOCALAPPDATA': TEST_HOME / 'AppData' / 'Local',
                   'APPDATA': TEST_HOME / 'AppData' / 'Roaming'}.items():
    path.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
for name in STATE_VARIABLES:
    os.environ.pop(name, None)


class HermeticViolation(RuntimeError):
    """A test reached the user's real state or a process it did not start. Not an OSError: product code that handles
    OSError (tempfile retries a PermissionError) must not swallow or loop on it."""


VIOLATIONS = []
CURRENT = {'test': '<collection>'}


def _protected(path):
    if isinstance(path, int) or path is None:
        return False
    try:
        full = os.path.normcase(os.path.abspath(os.fsdecode(path)))
    except (TypeError, ValueError):
        return False
    return any(full == p or full.startswith(p + os.sep) for p in PROTECTED)


def _refuse(what):
    VIOLATIONS.append((CURRENT['test'], what))
    raise HermeticViolation(f'hermetic test run: refused {what}')


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_PATH_EVENTS = {'os.remove': 1, 'os.rmdir': 1, 'os.mkdir': 1, 'os.truncate': 1, 'os.utime': 1, 'os.chmod': 1,
                'shutil.rmtree': 1, 'os.rename': 2, 'shutil.move': 2, 'shutil.copyfile': 2}


def _audit(event, args):
    if event == 'open':
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        writes = (any(c in mode for c in 'wax+') if isinstance(mode, str)
                  else bool(isinstance(flags, int) and flags & _WRITE_FLAGS))
        if writes and _protected(path):
            _refuse(f'writing {os.fsdecode(path)}')
    elif event in _PATH_EVENTS:
        for path in args[:_PATH_EVENTS[event]]:
            if _protected(path):
                _refuse(f'{event} {os.fsdecode(path)}')
    elif event == 'os.kill' and not _ours(args[0]):
        _refuse(f'os.kill of pid {args[0]}')
    elif event == 'subprocess.Popen':
        _check_taskkill(args[1])


def _check_taskkill(argv):
    """taskkill by image name, or of a pid this run did not start, is refused (a mocked Popen never gets here)."""
    words = [str(a) for a in argv] if isinstance(argv, (list, tuple)) else str(argv or '').split()
    if not words or Path(words[0].strip('"')).stem.lower() != 'taskkill':
        return
    lowered = [w.lower() for w in words]
    if '/im' in lowered:
        _refuse(f'taskkill by image name: {" ".join(words)}')
    for i, word in enumerate(lowered[:-1]):
        if word == '/pid' and words[i + 1].isdigit() and not _ours(int(words[i + 1])):
            _refuse(f'taskkill of pid {words[i + 1]}, a process this test run did not start')


sys.addaudithook(_audit)

_started = set()
_popen_init = subprocess.Popen.__init__


def _recording_init(self, *args, **kwargs):
    _popen_init(self, *args, **kwargs)
    _started.add(self.pid)


subprocess.Popen.__init__ = _recording_init


def _ours(pid):
    """A process this run started (or a descendant of it), or one that is gone (nothing to harm)."""
    if pid in _started or pid == os.getpid():
        return True
    try:
        if not psutil.pid_exists(pid):
            return True
        return pid in {c.pid for c in psutil.Process(os.getpid()).children(recursive=True)}
    except psutil.Error:
        return True


def _guard(name, real):
    def guarded(self, *args, **kwargs):
        if not _ours(self.pid):
            _refuse(f'psutil {name} of pid {self.pid}, a process this test run did not start')
        return real(self, *args, **kwargs)
    guarded.__wrapped__ = real
    return guarded


for _name in ('terminate', 'kill', 'send_signal', 'suspend'):
    setattr(psutil.Process, _name, _guard(_name, getattr(psutil.Process, _name)))
