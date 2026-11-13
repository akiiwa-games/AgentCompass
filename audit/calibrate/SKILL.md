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
   `calibration/<run date>/report.md` and `report.json` there, then prints a summary line. The whole run is read-only: it
   writes only those two files. `--out DIR` is the one way to put them elsewhere. A missing state directory or an
   unreadable database is an error, not an empty report.
2. Read **Coverage** first. It lists completed dispatches with no active time or tokens, by dispatch path and reason
   (only Claude and Codex session logs are read; other harnesses record the reason instead), failed or canceled
   dispatches that left no measurement, ledger decisions that never reached a dispatch (deliberate `self`/`none`/`split`
   choices are counted apart), and any rows or ledger lines that could not be read. A cell is marked *too small* under the report's `min_cell` samples: do not act
   on it, wait for more dispatches or widen the range.
3. Read the deviations, one section at a time, and decide what each may change:

   | Report section | Deviation | May change (shipped reference data) | May not change |
   |---|---|---|---|
   | Hours by work kind / demand profile | actual / expected hours far from 1 and stable across the range | the `expected_hours` of the matching cases in `skills/agent-compass/references/classifier-cases.json` (`calibration_cases`), and its `route_definitions` / `dimension_rubric` wording that anchors hours; add a neutral synthetic case for an under-covered kind | the classifier output in code, `work_kind` / `difficulty` / `profile` populated by hand, any hours rule keyed on a model or keyword |
   | Spend by model and effort | read the two errors apart. *Spend ratio* (actual list-price USD over `expected_usd`) mixes them. *Rate ratio* (actual USD per active hour over the `usd_per_hour` the estimate used) does not depend on the hours: if it is far from 1 the rate or the burn is off, if only the spend ratio is, the hours are (see the hours rows). A `>=` USD omits cache-write tokens the public data does not price: the shipped data lists no cache-write price for any model, so every Claude spend cell is a lower bound and its spend and rate ratios read low (the omitted write cost was about 14% of a real Claude mix). This is permanent until the reference data carries a cited cache-write price per model (from a public board or vendor price page, through `agentcompass data refresh`); then the cells tighten with no code change | burn or window parameters in `skills/agent-compass/references/accounts.json` (`scoring`: `context_burn_extra_at_250k`, safety margins and floors); a hours deviation goes to the classifier rows above | `usd_per_hour`, list prices and plan capacities (public sources only: a rate deviation is a note for the next upstream refresh), model category scores, per-model factors |
   | Endings (failed, relaunched, canceled, rotation) | one model or kind failing far more than the rest, or its workers *rotated away* (replaced for context) often | `rotate_after_carried_tokens` in `accounts.json` `scoring`; for failures open a bug for the dispatch path | disabling a model (the user's tray checkbox does that), name-based rules |
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

The rotation link (*rotated away*, found through the rotating decision's `allocation_task.chain.worker.agent_id`) has only been exercised on synthetic rows, so check the first real rotation by hand.

Read the gaps in `docs/technical-reference.md` ("Calibration report") before trusting a table: a retried task's later
attempts are left out of the hours and spend cells (only the completed attempt carries active time), ratios are medians
of per-dispatch ratios, only completed dispatches are measured, only Claude and Codex harnesses have active time and tokens, non-Paseo hosts record no
measurement, and the window share is derived from list price over cited capacity, not read from the provider.
