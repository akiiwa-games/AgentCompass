# Claude Code Instructions

Follow [AGENTS.md](AGENTS.md) for repository testing and hygiene rules.

## Test Commands

Tests are hermetic by default (`conftest.py` + `hermetic_run.py` isolate `HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA` per `PYTEST_XDIST_WORKER`, preserve `PYTHONUSERBASE`, and clear `PASEO_AGENT_ID` / `ROUTER_WORKER` for every test). Never add manual environment variable redirections to test commands or worker briefs.

```powershell
# Inner loop
pytest -n auto -m "not slow"

# Pre-merge
pytest -n auto
```
