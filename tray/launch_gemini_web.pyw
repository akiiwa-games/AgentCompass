"""Entry used by Gemini Web clients (hidden) and by a shortcut (`--show`) when the Gemini Web bridge is down."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import start_gemini_web

if __name__ == "__main__":
    try:
        start_gemini_web(show="--show" in sys.argv[1:])
    except Exception:
        log = Path.home() / ".agent-compass" / "gemini-web.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            import traceback

            traceback.print_exc(file=stream)
        raise SystemExit(1)
