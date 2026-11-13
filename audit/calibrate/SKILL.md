---
name: calibrate
description: >
  Maintainer-only calibration loop for AgentCompass: run audit/calibration_report.py over the recorded dispatch
  outcomes in the state directory, read where the classifier's hours, the spend estimate or the endings deviate,
  and change only the shipped reference data that carries calibration. Not installed for users.
---

# Calibration Skill (`calibrate`)

Runs on the maintainer's machine only. It lives in `audit/calibrate/`, next to the report it drives, and like
`packaging/verify-release/SKILL.md` it is not one of the skills `agentcompass setup` links into hosts
(`sync_host_globals.py` links only `skills/`). Open it by path when calibrating. What it changes reaches users through
the normal channels: shipped public reference data and releases.

## Boundaries (decided, do not relax)

- Recorded outcomes never feed routing at runtime. Quota economy uses only current-period consumption and the
  allocation modules do not import the ground-truth fields. The report proposes nothing and writes no routing input; you
  read it and edit by hand.
- Model category scores come only from public cited boards or maintainer manual scores. A local sample never becomes a
  model score, and a deviation in one model's hours or spend is not evidence about its category scores.
- Tests never pin a routing outcome (`AGENTS.md`, "Routing Tests Assert Rules, Never Outcomes"). A deviation is a reason to
  revisit a rule, a formula or a parameter, never to assert a winner.
- Personal values (task text, paths, ids, account names, local settings) stay out of tracked files.

## Loop

1. Run the report (read-only, spends no tokens):

   ```powershell
   python -X utf8 audit/calibration_report.py --since YYYY-MM-DD --until YYYY-MM-DD
   ```

   Defaults to the last 30 days of `~/.agent-compass` (or `ROUTER_STATE_DIR`, or `--state-dir`) and writes
   `calibration/<run date>/report.md` and `report.json` there, then prints a summary line.
2. Read **Coverage** first. It lists completed dispatches with no active time or tokens, by dispatch path and reason
   (only Claude and Codex session logs are read; other harnesses record the reason instead), and failed or canceled
   dispatches that left no measurement. A cell is marked *too small* under the report's `min_cell` samples: do not act
   on it, wait for more dispatches or widen the range.
3. Read the deviations, one section at a time, and decide what each may change:

   | Report section | Deviation | May change (shipped reference data) | May not change |
   |---|---|---|---|
   | Hours by work kind / demand profile | actual / expected hours far from 1 and stable across the range | the `expected_hours` of the matching cases in `skills/agent-compass/references/classifier-cases.json` (`calibration_cases`), and its `route_definitions` / `dimension_rubric` wording that anchors hours; add a neutral synthetic case for an under-covered kind | the classifier output in code, `work_kind` / `difficulty` / `profile` populated by hand, any hours rule keyed on a model or keyword |
   | Spend by model and effort | actual USD (recorded tokens at public list price) far from `expected_usd`; window share higher than the economy assumed | scoring parameters in `skills/agent-compass/references/accounts.json` (`scoring`: `context_burn_extra_at_250k`, safety margins and floors, `rotate_after_carried_tokens`) | `usd_per_hour`, list prices and plan capacities (public sources only), model category scores, per-model factors |
   | Endings (failed, relaunched, canceled, rotation) | one model or kind failing or rotating far more than the rest | provider-health and rotation parameters above; otherwise open a bug for the dispatch path | disabling a model (the user's tray checkbox does that), name-based rules |
   | Pinned and keep-whole groups | pins or `--keep-whole` common for one kind | the classifier cases and `route_definitions` for that kind (a pin is a symptom: see why the router's pick was overridden) | pins hard-coded away, exemptions |

   Write the change as a dated maintainer declaration: the date and the report's range go in the commit message, and
   `reviewed_at` in `accounts.json` moves when that file changes.
4. Rebuild and validate the shipped data (any hand-edit to `accounts.json` or `classifier-cases.json` needs it):

   ```powershell
   python -X utf8 agentcompass data refresh --no-fetch
   ```

5. Run only the tests of what you touched (for example `pytest skills/agent-compass/scripts/test_public_data.py`), never the
   full suite for a calibration change. If you edited Markdown or documented commands, also run
   `python -X utf8 skills/agent-compass/scripts/check_router_contract.py` and `pytest agentcompass/test_docs.py`.
6. Ship through the normal channels: commit the data to a `work/<name>` branch and merge to `main` (installs pick up the
   verified public-data set between releases) or include it in the next release.

## What the report cannot show

Read the gaps in `docs/technical-reference.md` ("Calibration report") before trusting a table: only completed
dispatches are measured, only Claude and Codex harnesses have active time and tokens, non-Paseo hosts record no
measurement, and the window share is derived from list price over cited capacity, not read from the provider.
