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
import site
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parent
REAL_USERBASE = Path(os.environ.setdefault('PYTHONUSERBASE', site.getuserbase()))
REAL_USERSITE = Path(site.getusersitepackages())
if site.ENABLE_USER_SITE:
    _user_site = str(REAL_USERSITE)
    if _user_site and os.path.isdir(_user_site) and _user_site not in sys.path:
        site.addsitedir(_user_site)
sys.dont_write_bytecode = True
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')

REAL_HOME = Path(os.environ.setdefault('_AGENTCOMPASS_REAL_HOME', os.path.expanduser('~')))
REAL_LOCALAPPDATA = Path(os.environ.setdefault(
    '_AGENTCOMPASS_REAL_LOCALAPPDATA',
    str(Path(os.environ.get('LOCALAPPDATA') or REAL_HOME / 'AppData' / 'Local')),
))
def _protected_roots():
    roots = [REAL_HOME / '.agent-compass', REAL_HOME / '.usage-float', REAL_LOCALAPPDATA / 'AgentCompass']
    for item in list(roots):
        try:
            roots.append(item.resolve())
        except OSError:
            pass
    return tuple(dict.fromkeys(os.path.normcase(os.path.abspath(p)) for p in roots))


PROTECTED = _protected_roots()
# State locations a shell may carry into the run; under test they come from the temporary home.
STATE_VARIABLES = ('ROUTER_STATE_DIR', 'AGENTCOMPASS_APP_DIR',
                   'AGENTCOMPASS_CHATGPT_CODEX_HOME',
                   'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'PASEO_HOME', 'PRO_CONSULT_HOME')
# Session and worker markers a parent/worker shell may carry into pytest; cleared at startup and before every test.
WORKER_VARIABLES = (
    'PASEO_AGENT_ID', 'AGENTCOMPASS_WORKER', 'PASEO_AGENT_CWD',
    'ROUTER_PARENT_AGENT_ID', 'ROUTER_PARENT_HOST', 'ROUTER_PARENT_ADDRESS',
    'ANTIGRAVITY_CONVERSATION_ID', 'CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDECODE',
    'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_SANDBOX', 'CODEX_CI',
    'PI_SESSION_ID', 'OPENCODE_SESSION_ID', 'ORCA_AGENT_SESSION_ID', 'ORCA_TERMINAL_HANDLE',
    'HC_TERMINAL_ID', 'T3_MCP_URL', 'T3_MCP_BEARER_TOKEN',
    'CINDY_INPUT_HANDOFF_ROUTE', 'CINDY_PI_PERMISSION_FILE', 'CINDY_PI_SESSION_ID', 'CINDY_PI_SUBAGENT_RUN_ROOT',
)


def main_checkout_root(root: Path = REPO_ROOT) -> Path | None:
    """Return the main repository root when ``root`` is a git worktree, else None."""
    dot_git = root / '.git'
    if dot_git.is_file():
        raw = dot_git.read_text(encoding='utf-8', errors='ignore').strip()
        if raw.startswith('gitdir:'):
            gitdir = (root / raw.split(':', 1)[1].strip()).resolve()
            if gitdir.parent.name == 'worktrees':
                return gitdir.parents[2]
    return None


def worker_key(environ=None) -> str:
    env = os.environ if environ is None else environ
    raw = (env.get('PYTEST_XDIST_WORKER') or 'main').strip()
    return ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_' for ch in raw) or 'main'


WORKER_ID = worker_key()
TEST_HOME = Path(tempfile.mkdtemp(prefix=f'agentcompass-test-home-{WORKER_ID}-'))
TEST_STATE_DIR = TEST_HOME / '.agent-compass'
atexit.register(shutil.rmtree, TEST_HOME, True)
for name, path in {'HOME': TEST_HOME, 'USERPROFILE': TEST_HOME, 'LOCALAPPDATA': TEST_HOME / 'AppData' / 'Local',
                   'APPDATA': TEST_HOME / 'AppData' / 'Roaming'}.items():
    path.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
for name in (*STATE_VARIABLES, *WORKER_VARIABLES):
    os.environ.pop(name, None)

if 'ROUTER_PASEO_SDK_ROOT' not in os.environ and not (REPO_ROOT / 'setup' / 'paseo-bridge' / 'node_modules').is_dir():
    _main_root = main_checkout_root()
    for _sdk in (
        *([_main_root / 'setup' / 'paseo-bridge' / 'node_modules'] if _main_root else []),
        REAL_HOME / '.agent-compass' / 'paseo-bridge' / 'node_modules',
    ):
        if _sdk.is_dir():
            os.environ['ROUTER_PASEO_SDK_ROOT'] = str(_sdk)
            break


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
_PATH_INDICES = {
    'os.remove': (0,), 'os.rmdir': (0,), 'os.mkdir': (0,), 'os.truncate': (0,), 'os.utime': (0,), 'os.chmod': (0,),
    'shutil.rmtree': (0,), 'os.rename': (0, 1), 'os.replace': (0, 1), 'shutil.move': (0, 1), 'shutil.copyfile': (1,),
}


def _audit(event, args):
    if event == 'open':
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        writes = (any(c in mode for c in 'wax+') if isinstance(mode, str)
                  else bool(isinstance(flags, int) and flags & _WRITE_FLAGS))
        if writes and _protected(path):
            _refuse(f'writing {os.fsdecode(path)}')
    elif event in _PATH_INDICES:
        for idx in _PATH_INDICES[event]:
            if idx < len(args) and _protected(args[idx]):
                _refuse(f'{event} {os.fsdecode(args[idx])}')
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
