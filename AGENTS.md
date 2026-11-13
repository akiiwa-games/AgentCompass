# Developer Instructions

## Running Tests

Every pytest run from the repository root is hermetic by default via `conftest.py` and `hermetic_run.py`:
- Each `pytest-xdist` worker receives its own isolated `HOME`, `USERPROFILE`, `LOCALAPPDATA`, and `APPDATA` temporary directory keyed by `PYTEST_XDIST_WORKER`.
- `PYTHONUSERBASE` is pinned to the real user base before `APPDATA` redirection so user site-packages (such as `pystray`) remain importable in tests and child processes.
- Inherited state and worker session variables (`PASEO_AGENT_ID`, `AGENTCOMPASS_WORKER`, `ROUTER_STATE_DIR`, `PASEO_HOME`, host session IDs, etc.) are cleared automatically before every test.
- Audit and process guards fail the test immediately if code attempts to write under the real `~/.agent-compass` or `%LOCALAPPDATA%/AgentCompass` or terminate a process the test run did not start.

Do not hand-write environment variable redirections (`HOME=...`, `USERPROFILE=...`, `ROUTER_STATE_DIR=...`) in test commands or worker briefs.

When to test (user rule, 2026-11-12):
- **A feature change within a version** (a worker brief, a fix, a merge) runs only the tests of the touched feature, for example `pytest skills/agent-compass/scripts/test_<feature>.py`. Never run the full suite or a regression pass for it, and never repeat runs to "confirm".
- **Regression** (the full suite) runs only once, before a release:

```powershell
pytest -n auto
```

CI (`.github/workflows/ci.yml`, push to `main` and pull requests) runs the fast suite, `pytest -n auto -m "not slow"`, on `windows-latest` after `pip install -e ".[dev,tray]"`, and builds the wheel on `ubuntu-latest` to run `agentcompass plan --demo --dry-run` and `agentcompass doctor`. So a worker still runs only the tests of its feature; a break elsewhere shows up in CI. Tests marked `slow` run only in the pre-release regression.

Install test dependencies with `pip install -e ".[dev]"` (`pytest`, `pytest-xdist`, `psutil`); the tray tests also need the `tray` extra, so CI and `packaging/verify_release.py` use `pip install -e ".[dev,tray]"`.

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

## Module Ownership Map

There are no compatibility re-exports across split modules. Always import `module.name` from the owning module so monkeypatching the owner reaches all callers:

| Area | Modules & Single Responsibility |
| :--- | :--- |
| **CLI & package** (`agentcompass/`) | `cli.py` (sole public `agentcompass` entry), `locations.py` (checkout vs installed skill/setup paths), `install.py` / `doctor.py` (active install inspection), `ports.py` (canonical loopback ports), `origin.py` (public GitHub coordinates). |
| **Allocation core** (`skills/agent-compass/scripts/`) | `allocator.py` (`plan`, `explain`, `assumptions`, `named_pin_block`), `account_fleet.py` (`load_accounts`, discovery merge, `model_resolution` wiring, `row_key`, `bundles`, `HARNESS_CAPABILITIES`), `task_intake.py` (`TaskProfile` validation, `multi_task`/`task_count`, modality gates, `normalize_task`), `axis_scores.py` (four categories `divergence`/`coding`/`aesthetics`/`tool_operation`, panel `z`-scores, `targets`, `five_axis_fit`, `continuity_value`), `quota_economy.py` (`capacity_usd`, `duration_at`, `model_usd_per_hour`, `window_recovery`, `value_ladder`, `quota_price`, `apply_five_axis_economy`), `quota_pools.py` (window state, floors, in-flight reservations, `launch_blockers`), `quota_overrides.py` (temporary pre-reset boosts and the `override` stage that writes them), `candidate_eval.py` (`evaluate_bundle` gates and mode-B exclusions), `candidate_ranking.py` (`five_axis_order`, `rank`), `ledger_snapshot.py` (`decision_snapshot`), `state_upgrade.py` (local state shape checks and fail-fast upgrade messages), `worker_sessions.py` & `antigravity_telemetry.py` (session logs, `historical_context_tokens`, `rotation_threshold`), `user_scores.py` (`local user > maintainer > measured > estimated` manual scores). |
| **Dispatch & usage** (`skills/agent-compass/scripts/`) | `route_judge.py` (CLI stages: `plan`, `record`, `score`, `scorer`, the argument parser and `main`), `scorer_client.py` (System-One scorer types, configuration, prompt, backends, parsing, score cache, fallback order, `score_task`, `classify_structured`), `decision_ledger.py` (`ledger_path`, `read_ledger`, `append_ledger`, `LEDGER_WRITES`, capacity alerts, `finalize`), `subscription_judge.py` (`delegate`/`select` stage judgments: `build_request`, `judge_order`, `evaluate`), `subscription_cli.py` (`cli_prefix`, `subscription_env`, `run_process`, `stop_process`), `usage_reading.py` (`usage_snapshot`, `window_state`, binding and tightest windows), `env_vars.py` (`user_env`), `roll.py` (`classify -> allocate -> launch`), `dispatch_flow.py` / `dispatch_registry.py` / `dispatch_fallback.py` / `dispatch_ground_truth.py` (SQLite reservation state machine, Paseo launch/retry, post-completion ground truth), `provider_inventory.py` / `account_state.py` / `live_models.py` / `model_latch.py` / `provider_retry.py` (zero-network account discovery, health states, live model checks, holds), `turn_refusal.py` (why a new turn to a worker is refused now, composed from those owners; read by the gate's quota check and the continuation verdict), `usage_core.py` / `usage_collector.py` / `usage_cache.py` / `usage_magpie.py` (multi-account quota polling and cache merge). |
| **Calibration & data** (`skills/agent-compass/`) | `calibration/sources.py`, `measurements.py`, `five_axis.py`, `refresh.py` (builds `public-measurements.json` and `five-axis-scores.json`), `scripts/capability_catalog.py` (generated `model-capabilities.{json,md}`), `scripts/public_data.py` (`public-data.json` sha256 manifest and between-release fetch/verify). |
| **Host rollout & gate** (`setup/`) | `sync_host_globals.py` & `host_rules.py` (system-level skill links and managed global rule blocks), `onboard.py` (`agentcompass setup`), `router_gate_hook.py` (`PreToolUse` / `tool_call` spawn and rotation gate), `paseo_dispatch_bridge.mjs` (Paseo SDK launch and notifier). |
| **Windows tray** (`tray/`) | `app.pyw` (tray icon), `router_window.py` (panel shell) + page mixins (`panel_scoring.py`, `panel_quota.py`, `panel_policy.py`, `panel_models.py`, `panel_strategy.py`, `panel_system.py`, `panel_plugins.py`, `panel_widgets.py`), `router_data.py` (Tk-free view model), `score_overview.py`, `radar_chart.py`, `quota_line.py`, `decision_theatre.py`, `updates.py`, `cli_updates.py`, `services.py`, `i18n_catalog.py`. |
| **Packaging & audit** (`packaging/`, `audit/`) | `app_bundle.py`, `app_bootstrap.py`, `check_payload.py`, `verify_release.py`, `sandbox_check.py`, `foreign_machine_check.py`, `zero_dispatch_check.py`, `audit/router_audit.py`, `audit/calibration_report.py` (read-only maintainer calibration report over `dispatch.sqlite3` and the ledger; its loop is `audit/calibrate/SKILL.md`, not installed for users). |
| **Allocation policy & state** (`skills/agent-compass/scripts/`) | `accounts_override.py` (sparse account preferences shared by tray and allocator), `host_policy.py` (local dispatch availability), `cwd_policy.py` (account working-directory policy), `window_scope.py` (which models a quota window binds), `fast_modes.py` (verified fast-mode contracts), `auto_bundles.py` (bundles derived from detected models), `launch_failures.py` (cooldowns after a definitive create failure), `quota_exhaustion.py` (durable confirmed quota failures), `override_store.py` (process-safe override transactions), `antigravity_plan.py` (Antigravity tier lookup), `venice_usage.py` (Venice key balances). |
| **Dispatch placement & tooling** (`skills/agent-compass/scripts/`) | `host_placement.py` (host-neutral `--workspace` validation and placement), `host_herdr.py` (Herdr core adapter), `worker_context.py` (parent addressing for routing and preflight), `entry.py` (the argument vector that runs `agentcompass`, from a checkout, an install or the frozen executable), `model_resolution.py` (local model-name normalization shared by discovery and allocation), `check_router_contract.py` (routing-document syntax and local reference check), `stage_help.py` (per-stage `-h`), `demo_plan.py` (offline reference-fleet demo), `route_simulation.py` (routing distribution simulation and ledger replay), `plan_state_residue.py` (read-only list of plan residue), `grok_bot_paths.py` / `run_grok_bot.py` (pinned Grok Bot CLI location and thin runner), `router_test_support.py` (hermetic router test fixtures). |
| **Calibration extras** (`skills/agent-compass/calibration/`) | `arena_capture.py` (arena.ai boards imported from captured page text), `series.py` (same-series siblings and conversion base), `tiers.py` (tier from the arena Overall board), `community.py` (cited community measurements of window capacity). |
| **Package extras** (`agentcompass/`) | `__main__.py` (`python -m agentcompass`), `frozen_python.py` (the installer's `agentcompass.exe` doubling as the interpreter for the router scripts), `tools.py` (why an optional host executable is needed and how to install it). |
| **Setup helpers** (`setup/`) | `register_claude_account.py` (register a signed-in isolated Claude account), `bootstrap_dot2api.py` (Dot2API storage and caller credentials), `lock_dot2api_acl.py` (limit the Dot credential directory to the current user), `pi_device_login.mjs` (Pi device-code login). |
| **Tray runtime & updates** (`tray/`) | `component_launch.py` (start helper modules from source or from the frozen tray), `deploy.py` (check, install and restart the tray), `update_status.py` (update progress and status window), `cli_update_files.py` (retain locked npm native images), `runtime_paths.py` (resolve host executables), `owned_command.py` / `owned_process.py` (job-owned child processes), `plugins.py` (optional plugins and their on/off state), `local_controls.py` (local menu IPC), `router_setup.py` (first-run setup from the tray), `router_priority.py` (tray controls for the override store), `capacity_stats.py` (usage readings and worker-hours from session logs), `usage_collect.py` (one collector run as a tray child), `usage_float_host.py` (host the Usage Float loop), `model_catalog.py` (cached read-only model discovery), `grok_bot_cli.py` (Grok Bot CLI card actions). |
| **Tray bridges & mesh** (`tray/`) | `chatgpt_web_patch.py` (anchored patches on the ChatGPT Web source), `web_launcher.py` / `launch_chatgpt_web.pyw` / `launch_gemini_web.pyw` / `launch_usage_float.pyw` (bridge and window entry points), `gateways.py` (Muse2API gateway), `account_login.py` (browser sign-in imported into Muse2API), `dot_reconnect.py` / `dot_plugin.mjs` (keep the Dot connected across tunnel address changes), `easytier.py` / `easytier_paths.py` / `easytier_setup.py` / `easytier_supervisor.ps1` (the host's EasyTier node), `network_status.py` (read-only EasyTier window). |
| **Tray UI support** (`tray/`) | `i18n.py` (language preference and translations), `display_names.py` (short display names), `decision_summary.py` (compact decision verdict), `page_stack.py` (stacked pages), `ui_theme.py` (one visual language), `tk_test_support.py` (Tkinter test helpers). |
| **Packaging extras** (`packaging/`) | `build.ps1` (one-folder tray and installer build), `cli_entry.py` / `tray_entry.py` (entry points of `agentcompass.exe` and `AgentCompass.exe`), `hatch_build.py` / `stage_data.py` (wheel staging), `windows_sandbox_check.py` (guest check inside Windows Sandbox), `guest_check.py` (the checks that run inside the sandbox guest), `privacy_check.py` (tracked files against generic patterns and the local denylist), `zero_fake_cli.py` / `zero_fake_paseo_bridge.mjs` (fixture provider CLI and Paseo host for the zero-dispatch check), `f10_evidence_probe.py` / `f11_omp_probe.py` / `f13_product_layout_probe.py` (foreign-machine probes). |

## Change & Regeneration Checklist (Blast Radius)

- **Any shipped reference data** (calibrated scores, capacities, `references/user-scores.json`, or hand-edits to `accounts.json`, `plan-templates.json`, `classifier-cases.json`): run `python -X utf8 agentcompass data refresh --no-fetch` (omit `--no-fetch` only when fetching new upstream boards) to rebuild `public-measurements.json`, `five-axis-scores.json`, `model-capabilities.{json,md}`, and `public-data.json` in topological order and validate schemas.
- **Skill / reference Markdown or documented CLI commands**: run `python -X utf8 skills/agent-compass/scripts/check_router_contract.py` and `pytest agentcompass/test_docs.py` (never teach direct `route_judge.py` script calls; keep `packaging/sandbox_check.py` `DOC_RUNS` aligned if documented commands change).
- **Tray UI text**: update `tray/i18n_catalog.py` alongside `tray/panel_*.py`.

## Standing Architectural Invariants

- **Data provenance**: Shipped reference data comes only from cited public sources (`vendor`, `benchmark`, `community`, dated with URL) or dated `maintainer` declarations. Never place hand-tuned constants, model-name/keyword rules, or machine-local settings (`~/.agent-compass/*`) in public defaults.
- **Classifier authority**: Never override classifier outputs in code or hand-populate `work_kind`, `difficulty`, or `profile`.
- **No compatibility debt**: No compatibility aliases, re-export shims, old-shape readers, silent literal fallbacks on missing config keys, or dead code, with no exceptions.
- **Credentials & privacy**: Read-only toward provider OAuth tokens; never print, log, or commit credentials, tokens, raw transcripts, or unredacted API responses.
- **Documentation single source of truth**: Define scoring formulas, tie-break rules, and operational internals once (`docs/technical-reference.md` for repository/UI/operations reference; `skills/agent-compass/references/router-internals.md` for packaged skill runtime contracts). Keep `README.md` and `llms.txt` as high-level entry guides that link to the reference instead of duplicating formulas or numeric walkthroughs.

## Repository Hygiene

- **Worktrees**: Develop delegated changes in an isolated git worktree on a `work/<name>` branch based on `main`. Deliver new commits only; never `git commit --amend`.
- **Line endings**: On Windows, write text files in Python with `newline='\n'`.
- **Tracked root files**: `.gitignore` ignores `/*` by default; explicit root additions must be allowlisted in `.gitignore` and verified against `packaging/check_payload.py` when applicable.
- **Privacy**: This repository is public. A personal value in tracked code (a user name, private project or path, machine id, real e-mail address) is a design defect, not only a leak: it cannot work on anyone else's machine. Derive machine-specific values at runtime (as `packaging/guest_check.py` reads the host home staged by its launcher); test fixtures use neutral placeholders such as `D:/work/...`. `python -X utf8 packaging/privacy_check.py` scans tracked files against generic patterns and a local denylist kept outside the repository (`~/.agent-compass/privacy-denylist.txt`); it runs locally as the first step of `packaging/verify_release.py` before a release, never in CI, so the denylist never leaves the machine.
- **Child processes**: Every process started by a test or verification command must exit or be terminated before finishing; never terminate processes you did not start.
