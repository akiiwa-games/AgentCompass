---
name: verify-release
description: >
  Pre-release behavioral verification for AgentCompass (router + tray) and usage-float.
  Installs from the built release artifacts (wheel and Inno Setup installer) into clean,
  foreign-looking machine profiles and Windows Sandbox, proving user-visible behaviors
  before tagging or syncing to another computer.
---

# Release Verification Skill (`verify-release`)

Verify **AgentCompass** (router + tray) and **usage-float** from their packaged artifacts in a clean, foreign-looking machine profile and Windows Sandbox before cutting a release or syncing to a second host (`LITTLESPRING` / `LS`).

## Principles (pstack)

1. **Test behavior, not implementation** (`principle-test-behavior-not-implementation`): invoke the installed `agentcompass` console script, `router_gate_hook.py`, `router_data` / `router_window`, `usage_float.py`, and `AgentCompass.exe` against local HTTP fixtures and fake provider CLIs.
2. **Prove it works from packaged artifacts** (`principle-prove-it-works`): build the `.whl` and `AgentCompass-Setup-X.exe` from an isolated git copy, install into a fresh venv / Windows Sandbox guest, and verify that `site-packages` (never the repo checkout) is executing.
3. **Never touch the real user profile**: all non-guest harnesses run inside throwaway `HOME`/`USERPROFILE`/`ROUTER_STATE_DIR` directories with `sitecustomize.py` network interception (`sandbox_net.json`) and verify `real_state_snapshot()` is unpolluted afterwards.

## Environment & Prerequisites

- **OS:** Windows 10/11 with Python 3.11+ (`python -X utf8`) and Node.js (`node`) on `PATH`.
- **Optional for full installer / GUI check:** Windows Sandbox (`wsb.exe`) enabled and Inno Setup installer staged under `%TEMP%\agentcompass-tools\innosetup-*.exe`.
- **No live credentials or network required:** provider HTTP (`chatgpt.com`, `api.anthropic.com`, `raw.githubusercontent.com`, `cloudcode-pa.googleapis.com`, Magpie gateway) and CLIs (`claude`, `codex`, `agy`) are served by local fixtures inside each harness.

## How to Run

### 1. Fast Foreign-Machine + Zero-Dispatch Suite (every pre-release iteration)

```powershell
python -X utf8 packaging/verify_release.py
```

This runs:
- `packaging/foreign_machine_check.py`: builds the wheel + isolated `usage-float` repo, installs into a clean venv, and executes `F01`–`F11` across foreign profiles (`--only F10` runs one feature; F09 always runs).
- `packaging/zero_dispatch_check.py`: verifies every dispatch surface (`D01`–`D13`) from the installed wheel.
- Pytest behavior & regression suites across `setup/`, `skills/agent-compass/scripts/`, `tray/`, and `usage-float/`.

### 2. Full Release Verification (including Windows Sandbox Installer & GUI)

```powershell
python -X utf8 packaging/verify_release.py --with-windows-sandbox
```

This additionally boots an offline Windows Sandbox (`packaging/windows_sandbox_check.py --connect`), installs the wheel and builds/installs `AgentCompass-Setup-<version>.exe` via Inno Setup, drives the tray GUI and `router_window`, and verifies clean uninstall.

### 3. Second-Host (`LITTLESPRING` / `LS`) Post-Release Acceptance

After pushing the release tag and waiting for `.github/workflows/release.yml` to publish the release assets:
1. Upgrade the wheel on `LS` (`pip install --upgrade <wheel or git tag>`) and update `usage-float`.
2. Run `agentcompass data --refresh`, `agentcompass doctor`, `agentcompass providers`, `agentcompass usage`, and `agentcompass plan --demo --dry-run` on `LS` and verify zero errors, `schema 2` reference data, and fresh quota readings.

## Feature Inventory

Tracked in [`features.json`](features.json). Update both `features.json` and `packaging/foreign_machine_check.py` whenever a user-visible routing, quota, gate, setup, tray, or `usage-float` behavior changes (`maintain-verification-skill`).
