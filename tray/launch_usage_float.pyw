"""Desktop shortcut hook for the hosted Usage Float window."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import services

try:
    services.component_action("usage-float", "show")
except Exception:
    import traceback
    services.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (services.STATE_DIR / "usage-float-launch.log").open("a", encoding="utf-8") as log:
        traceback.print_exc(file=log)
