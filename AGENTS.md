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

## Routing Tests Assert Rules, Never Outcomes

- Never assert a routing outcome: which model or bundle wins (`choice == ...`), a model's share across scenarios, a ranking order, or an exact score, price or cost copied from current output. Nobody has validated those results as correct, so such an assertion only freezes whatever the router produced that day.
- Assert properties that follow from a stated rule, and cite the rule:
  - exclusion when a window is exhausted or a data gap is mode B;
  - ability capped at the task target;
  - quota cost non-decreasing in used share;
  - the documented tie-break order, as pairwise properties;
  - declared formulas computed from the data, not pinned constants;
  - invariance to input order;
  - local settings never leaking into public defaults.
- Refactor equivalence checks (same inputs, identical output before and after a pure refactor) are fine. They make no claim that the output is right.
- Feedback such as "simple research went to the wrong model" or "a frontier task picked a weak model while quota was ample" is a reason to revisit the routing rules, formulas and parameters. Never turn it into a test that pins the desired winner.
- When a change breaks an outcome-style assertion, delete it or replace it with the rule it stood for. Never re-pin it to the new output.

## Repository Hygiene

- **Worktrees**: Develop delegated changes in an isolated git worktree on a `work/<name>` branch based on `main`. Deliver new commits only; never `git commit --amend`.
- **Line endings**: On Windows, write text files in Python with `newline='\n'`.
- **Tracked root files**: `.gitignore` ignores `/*` by default; explicit root additions must be allowlisted in `.gitignore` and verified against `packaging/check_payload.py` when applicable.
- **Child processes**: Every process started by a test or verification command must exit or be terminated before finishing; never terminate processes you did not start.
