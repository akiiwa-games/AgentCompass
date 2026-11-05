"""Where the host's one EasyTier node lives (stdlib only, shared by the tray and easytier_setup.py).

The node is run by a SYSTEM scheduled task, so its program, config and logs need a per-machine home that
only SYSTEM and Administrators can write, and that outlives both a git checkout and the tray installer's app
directory (a reinstall or uninstall replaces the app directory; the per-user install even makes it user-writable).
``%ProgramData%\\AgentCompass\\easytier`` is that home: machine-wide like the task, not inside any user profile,
and untouched by the installer. easytier_setup.py locks it (and its AgentCompass parent) to SYSTEM and
Administrators.

Before this home existed the node lived in ``<checkout or app dir>\\easytier``. ``home()`` still finds a node
there until ``easytier_setup.py install --migrate-from`` has moved it.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = "AGENTCOMPASS_EASYTIER_HOME"      # tests and isolated checks only
CORE_NAME = "easytier-core.exe"


def program_data(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("ProgramData") or environ.get("ALLUSERSPROFILE") or r"C:\ProgramData")


def product_home(environ=None) -> Path:
    """The fixed per-machine home. An install always goes here."""
    environ = os.environ if environ is None else environ
    override = environ.get(OVERRIDE)
    if override:
        return Path(override).expanduser()
    return program_data(environ) / "AgentCompass" / "easytier"


def product_parent(environ=None) -> Path | None:
    """The AgentCompass folder under ProgramData that install also locks; None when the home is overridden."""
    environ = os.environ if environ is None else environ
    if environ.get(OVERRIDE):
        return None
    return product_home(environ).parent


def legacy_home(root: Path | None = None) -> Path:
    """The checkout-era (or app-directory) location: <root>\\easytier."""
    return Path(root or ROOT) / "easytier"


def installed_at(home: Path) -> bool:
    return (Path(home) / "bin" / CORE_NAME).is_file()


def home(environ=None, root: Path | None = None) -> Path:
    """The product home when a node is there, else a node still at the legacy location, else the product home."""
    product = product_home(environ)
    if installed_at(product):
        return product
    legacy = legacy_home(root)
    if installed_at(legacy):
        return legacy
    return product
