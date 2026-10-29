"""Tray-facing shim. Canonical ports live in agentcompass.ports."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from agentcompass.ports import conflicts, get, listeners, load, problems  # noqa: E402

__all__ = ["conflicts", "get", "listeners", "load", "problems"]
