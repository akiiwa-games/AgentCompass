"""Resolve host executables without usernames or a fixed Python minor version."""
from pathlib import Path
import os
import shutil
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def executable(name: str, override: str, candidates=()) -> Path:
    configured = os.environ.get(override)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f'{override} does not name an installed executable')
        return path
    found = shutil.which(name)
    if found:
        return Path(found)
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f'{name} is not installed; put it on PATH or set {override}')


def uv_path() -> Path:
    user_scripts = Path(sysconfig.get_path('scripts', scheme='nt_user'))
    return executable('uv.exe', 'ROUTE_MANAGER_UV', (
        Path(sys.executable).parent / 'uv.exe',
        Path(sysconfig.get_path('scripts')) / 'uv.exe',
        user_scripts / 'uv.exe', Path.home() / '.local/bin/uv.exe',
        ROOT / 'tools/uv/uv.exe',
    ))


def cloudflared_path() -> Path:
    return executable('cloudflared.exe', 'ROUTE_MANAGER_CLOUDFLARED', (
        ROOT / 'tools/cloudflared/cloudflared.exe',
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'cloudflared/cloudflared.exe',
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'cloudflared/cloudflared.exe',
    ))


def node_path() -> Path:
    return executable('node.exe', 'ROUTE_MANAGER_NODE', (
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'nodejs/node.exe',
        Path.home() / 'AppData/Roaming/nvm/current/node.exe',
    ))

