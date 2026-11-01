import sys
from pathlib import Path

if not __package__:
    # `python <checkout>/agentcompass <command>` (no install, any working directory): the package sits one level up.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcompass.cli import main

raise SystemExit(main())
