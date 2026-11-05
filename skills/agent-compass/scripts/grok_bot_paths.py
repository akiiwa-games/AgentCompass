"""Where the pinned Grok Bot CLI is installed (stdlib only; used by install_cli.py, the runner, inventory and tray).

The CLI lives per user under the AgentCompass state directory, not inside the skill: an installed wheel keeps the
skill in site-packages, which every upgrade replaces (and which may not be writable). A CLI installed by an
older version inside the skill (grok-bot/runtime) is still used until install/repair puts one in the new place.
"""
from __future__ import annotations

import os
from pathlib import Path

OVERRIDE = 'AGENTCOMPASS_GROK_BOT_RUNTIME'
PACKAGE = Path(__file__).resolve().parents[1] / 'grok-bot'
ENTRY = 'bin/gbot.mjs'


def install_dir(environ=None) -> Path:
    """Where install/repair writes the CLI."""
    environ = os.environ if environ is None else environ
    if environ.get(OVERRIDE):
        return Path(environ[OVERRIDE]).expanduser()
    state = environ.get('ROUTER_STATE_DIR') or Path.home() / '.agent-compass'
    return Path(state).expanduser() / 'grok-bot' / 'runtime'


def runtime_dir(package: Path | None = None, environ=None) -> Path:
    """The installed CLI: the per-user one, else a legacy copy inside the skill, else the per-user location."""
    target = install_dir(environ)
    legacy = Path(package or PACKAGE) / 'runtime'
    if not (target / ENTRY).is_file() and (legacy / ENTRY).is_file():
        return legacy
    return target
