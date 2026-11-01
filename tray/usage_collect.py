"""Run one usage collection with the router's collector (skills/agent-compass/scripts/usage_collector.py).

The tray starts this every 5 minutes as a low-priority child. It needs the agent-compass skill but not Usage Float.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import component_launch


def main(argv: list[str] | None = None) -> int:
    scripts = component_launch.skill_scripts()
    if scripts is None:
        sys.stderr.write("agent-compass skill not found; run `agentcompass setup`\n")
        return 2
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import usage_collector
    return usage_collector.main(["--once", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
