"""Entry point of the installer's console `agentcompass.exe` (packaging/agentcompass-tray.spec, cli build)."""

from agentcompass.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
