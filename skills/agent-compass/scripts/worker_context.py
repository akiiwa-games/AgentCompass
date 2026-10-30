"""Parent addressing shared by routing and dispatch preflight."""
import os
from pathlib import Path
import time

HOST_SESSION_ID_VARS = (
    'ROUTER_PARENT_AGENT_ID',
    'ANTIGRAVITY_CONVERSATION_ID',
    'CLAUDE_SESSION_ID',
    'CLAUDE_CODE_SESSION_ID',
    'CODEX_THREAD_ID',
    'CODEX_SESSION_ID',
    'PI_SESSION_ID',
    'OPENCODE_SESSION_ID',
)


def _claude_project_dir_candidates(projects_root: Path) -> list[Path]:
    try:
        cwd = str(Path.cwd().resolve())
    except OSError:
        return [projects_root]
    slug = ''.join(ch if ch.isalnum() else '-' for ch in cwd).strip('-')
    matched = [
        d for d in projects_root.iterdir()
        if d.is_dir() and d.name.strip('-').lower() == slug.lower()
    ]
    return [*matched, projects_root]


def detect_host_session():
    """Auto-detect the caller's session ID from the active host environment when outside Paseo."""
    for var in HOST_SESSION_ID_VARS:
        val = os.environ.get(var, '').strip()
        if val:
            return val
    if os.environ.get('CLAUDECODE') == '1':
        cfg = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects'
        if cfg.is_dir():
            try:
                now = time.time()
                for root in _claude_project_dir_candidates(cfg):
                    iterator = root.glob('*.jsonl') if root != cfg else cfg.rglob('*.jsonl')
                    candidates = [
                        (p.stat().st_mtime, p.stem)
                        for p in iterator
                        if p.is_file() and (now - p.stat().st_mtime) <= 3600
                    ]
                    if candidates:
                        candidates.sort(reverse=True)
                        return f'claude:{candidates[0][1]}'
            except OSError:
                pass
        return f'claude-session-{os.getppid()}'
    if os.environ.get('CODEX_SANDBOX') or os.environ.get('CODEX_CI'):
        return f'codex-session-{os.getppid()}'
    return ''


def require_parent_id(value):
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in {
        'top-level', 'none', 'null', 'unknown', 'parent', '<parent-id>', '<parent id>',
    }:
        raise ValueError('An actual addressable parent session ID is required; pass --parent-id from the host. '
                         'Do not launch an unaddressed worker or substitute top-level/task/worker IDs.')
    return value.strip()


HOSTS = ('paseo', 'native', 'orca', 't3')
NATIVE_HOSTS = ('native', 'orca', 't3')
# Orca's own mailbox spelling (src/shared/orca-session-address-prefix.ts, structured-worker-handle.ts).
ORCA_SESSION_ADDRESS_PREFIX = 'orca_session_id:'
ORCA_WORKER_HANDLE_PREFIX = 'structworker_'
# T3 Code injects these only into its Pi provider; Claude and Codex threads reach its MCP tools without any env marker.
T3_MCP_ENV = ('T3_MCP_URL', 'T3_MCP_BEARER_TOKEN')


def orca_address(session_id='', handle=''):
    """Orca addresses a worker by its minted handle and every other session as ``orca_session_id:<id>``."""
    session_id, handle = (session_id or '').strip(), (handle or '').strip()
    if handle.startswith(ORCA_WORKER_HANDLE_PREFIX):
        return handle
    return f'{ORCA_SESSION_ADDRESS_PREFIX}{session_id}' if session_id else handle


def detect_orca_session():
    """(session ID, mailbox address) of the Orca terminal agent running this process, or None outside Orca."""
    session_id = os.environ.get('ORCA_AGENT_SESSION_ID', '').strip()
    handle = os.environ.get('ORCA_TERMINAL_HANDLE', '').strip()
    if not (session_id or handle):
        return None
    return session_id or handle, orca_address(session_id, handle)


def in_t3():
    return all(os.environ.get(name, '').strip() for name in T3_MCP_ENV)


def resolve_parent(args):
    raw_paseo = os.environ.get('PASEO_AGENT_ID')
    if raw_paseo is not None and raw_paseo.strip():
        actual = require_parent_id(raw_paseo)
    else:
        actual = ''
    host = getattr(args, 'parent_host', None) or ('paseo' if actual else None)
    explicit = getattr(args, 'parent_id', None)
    if actual and (host not in ('paseo', 'auto') or (explicit and explicit.strip() != actual)):
        raise ValueError('Parent differs from actual PASEO_AGENT_ID.')
    orca = None if (actual or explicit or host not in (None, 'auto', 'orca')) else detect_orca_session()
    detected = '' if (actual or explicit) else (orca[0] if orca else detect_host_session())
    parent = require_parent_id(actual or explicit or detected)
    address = getattr(args, 'parent_address', None)
    if host in (None, 'auto'):
        env_host = os.environ.get('ROUTER_PARENT_HOST', '').strip().lower()
        if env_host in HOSTS:
            host = env_host
        elif orca:
            host = 'orca'
        elif not actual and in_t3():
            host = 't3'
        elif isinstance(address, str) and address.strip() and address.strip() != parent:
            host = 'native'
        else:
            host = 'paseo'
    elif host not in HOSTS:
        raise ValueError('Pass --parent-host paseo|native|orca|t3 so the worker can address the parent on the correct host.')
    if host == 'paseo' and address and address != parent:
        raise ValueError('Paseo parent message address must equal the actual parent session ID.')
    if host == 'native' and (not isinstance(address, str) or not address.strip()):
        raise ValueError('Native host requires --parent-address <inbox-address> as well as --parent-id <session-id>.')
    if host == 'orca' and not (isinstance(address, str) and address.strip()):
        address = orca[1] if orca and parent == orca[0] else orca_address(parent, os.environ.get('ORCA_TERMINAL_HANDLE', ''))
    # T3 delegates through its MCP credential, so the parent thread needs no address beyond its own ID.
    return parent, host, address or parent
