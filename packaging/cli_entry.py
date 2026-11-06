"""Entry point of the installer's console `agentcompass.exe` (packaging/agentcompass-tray.spec, cli build)."""

import sys

import app_bootstrap


def _main() -> int:
    if getattr(sys, "frozen", False):
        return app_bootstrap.run_cli()
    from agentcompass.cli import main
    return int(main() or 0)


if __name__ == "__main__":
    raise SystemExit(_main())
