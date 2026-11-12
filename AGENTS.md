# Developer Instructions

## Running Tests

Every pytest run from the repository root is hermetic by default via `conftest.py` and `hermetic_run.py`:
- Each `pytest-xdist` worker receives its own isolated `HOME`, `USERPROFILE`, `LOCALAPPDATA`, and `APPDATA` temporary directory keyed by `PYTEST_XDIST_WORKER`.
- `PYTHONUSERBASE` is pinned to the real user base before `APPDATA` redirection so user site-packages (such as `pystray`) remain importable in tests and child processes.
- Inherited state and worker session variables (`PASEO_AGENT_ID`, `ROUTER_WORKER`, `ROUTER_STATE_DIR`, `PASEO_HOME`, host session IDs, etc.) are cleared automatically before every test.
- Audit and process guards fail the test immediately if code attempts to write under the real `~/.agent-compass` or `%LOCALAPPDATA%/AgentCompass` or terminate a process the test run did not start.

Do not hand-write environment variable redirections (`HOME=...`, `USERPROFILE=...`, `ROUTER_STATE_DIR=...`) in test commands or worker briefs.

Use the two standard commands from the repository root:

```powershell
# Inner loop (fast unit and contract suite)
pytest -n auto -m "not slow"

# Pre-merge (full suite, including real subprocesses, packaging, sandbox checks, and Tk UI loops)
pytest -n auto
```

Install test dependencies with `pip install -e ".[dev]"` (`pytest`, `pytest-xdist`, `psutil`).

## Repository Hygiene

- **Worktrees**: Develop delegated changes in an isolated git worktree on a `work/<name>` branch based on `main`. Deliver new commits only; never `git commit --amend`.
- **Line endings**: On Windows, write text files in Python with `newline='\n'`.
- **Tracked root files**: `.gitignore` ignores `/*` by default; explicit root additions must be allowlisted in `.gitignore` and verified against `packaging/check_payload.py` when applicable.
- **Child processes**: Every process started by a test or verification command must exit or be terminated before finishing; never terminate processes you did not start.
