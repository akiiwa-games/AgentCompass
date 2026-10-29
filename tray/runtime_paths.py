"""Resolve host executables without usernames or a fixed Python minor version.

A missing bun, cloudflared, or node raises ToolMissing (a FileNotFoundError) whose
message names the feature and how to install it. Callers that start the tray catch
it and leave that feature off.
"""
from pathlib import Path
import os
import shutil
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


class ToolMissing(FileNotFoundError):
    """An optional executable is absent. The tray can keep running without it."""

    def __init__(self, tool: str, feature: str, install: str, override: str, detail: str = ""):
        self.tool = tool
        self.feature = feature
        self.install = install
        self.override = override
        text = (
            f"{tool} is not installed. {feature} needs it. {install} "
            f"Or set {override} to the executable. "
            f"The tray keeps running and this feature stays off until {tool} is available."
        )
        if detail:
            text = f"{detail} {text}"
        super().__init__(text)


def _hint(name: str) -> dict:
    root = str(ROOT)
    if root not in sys.path:
        sys.path.append(root)
    from agentcompass.tools import hint
    return hint(name)


def executable(name: str, override: str, candidates=(), *, prefer_candidates: bool = False) -> Path:
    def missing(detail: str = "") -> ToolMissing:
        info = _hint(name)
        return ToolMissing(info["tool"], info["feature"], info["install"], override, detail=detail)

    configured = os.environ.get(override)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise missing(f"{override} does not name an installed executable ({path}).")
        return path

    def on_path() -> Path | None:
        seen: list[str] = []
        for candidate_name in (name, Path(name).stem):
            if candidate_name in seen:
                continue
            seen.append(candidate_name)
            found = shutil.which(candidate_name)
            if found:
                return Path(found)
        return None

    def on_disk() -> Path | None:
        for path in candidates:
            if path.is_file():
                return path
        return None

    found = (on_disk() or on_path()) if prefer_candidates else (on_path() or on_disk())
    if found is not None:
        return found
    raise missing()


def uv_path() -> Path:
    user_scripts = Path(sysconfig.get_path('scripts', scheme='nt_user'))
    return executable('uv.exe', 'ROUTE_MANAGER_UV', (
        Path(sys.executable).parent / 'uv.exe',
        Path(sysconfig.get_path('scripts')) / 'uv.exe',
        user_scripts / 'uv.exe', Path.home() / '.local/bin/uv.exe',
        ROOT / 'tools/uv/uv.exe',
    ))


def bun_path() -> Path:
    """Workspace copy first, so a dev checkout keeps using tools/bun/bun.exe."""
    return executable('bun.exe', 'ROUTE_MANAGER_BUN', (
        ROOT / 'tools/bun/bun.exe',
        Path.home() / '.bun/bin/bun.exe',
    ), prefer_candidates=True)


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
