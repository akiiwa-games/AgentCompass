"""Where the pinned Grok Bot CLI is installed (stdlib only; used by install_cli.py, the runner, inventory and tray).

The CLI lives per user under the AgentCompass state directory, not inside the skill: an installed wheel keeps the
skill in site-packages, which every upgrade replaces (and which may not be writable).
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


def runtime_dir(environ=None) -> Path:
    """The installed per-user CLI location."""
    return install_dir(environ)
