"""Entry point of the installer's windowed `AgentCompass.exe` (packaging/agentcompass-tray.spec)."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import app_bootstrap


def _main() -> int:
    try:
        return int(app_bootstrap.run_tray() or 0)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except Exception:
        text = traceback.format_exc()
        if sys.stderr is not None:
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except Exception:
                pass
        try:
            log_dir = Path.home() / ".agent-compass"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "tray-bootstrap-error.log").write_text(text, encoding="utf-8")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
