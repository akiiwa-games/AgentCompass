"""Entry used by the route (hidden) and by the desktop shortcut (`--show`) when the ChatGPT Web bridge is down."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import start_chatgpt_web

if __name__ == "__main__":
    try:
        start_chatgpt_web(show="--show" in sys.argv[1:])
    except Exception:
        log = Path.home() / ".agent-compass" / "chatgpt-web.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            import traceback

            traceback.print_exc(file=stream)
        raise SystemExit(1)
