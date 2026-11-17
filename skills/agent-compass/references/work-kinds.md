# Choosing `work_kind` and the 4D Capability Envelope

The System-One classifier classifies observable task activity (`work_kind`, `primary_level` `1..3`, and optional composite `secondary_kind` / `secondary_level` `0..3`), and `task_intake.project_axis_demand` maps it deterministically onto `(creativity, coding, aesthetics, tool_operation)` via the tropical max envelope `d_a = max(Phi_a(k1, s1), Phi_a(k2, s2))`.

## 1. The 11 Atomic `work_kind` Signatures

The signature table is a maintainer declaration (2026-11-17): a design rule of this router, neither a measurement nor a value fitted to routing outcomes. `task_intake.WORK_KIND_SIGNATURES` is its one copy in code; change both together.

Role codes for depth `s` in `{1, 2, 3}`:
- `0` -> `0` (orthogonal)
- `B` -> `I[s >= 2]` (caps at panel minimum `t1`, zero premium above baseline)
- `S` -> `s - 1` (supporting axis: caps at panel median `t2` at `s = 3`, never opening the L3 `ceiling`)
- `P` -> `s` (primary bottleneck axis: `1:1` with depth, unlocking the L3 `ceiling` at `s = 3`)
- `F2` -> `2` (real-time `responsiveness` floor on `tool_operation`)
- `F3` -> `3` (real-time `responsiveness + image_recognition` floor on `tool_operation`)

| `work_kind` | Observable scope | `(creativity, coding, aesthetics, tool_operation)` | `s = 1` | `s = 2` | `s = 3` | Implied gates |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| `creative_ideation` | Cross-domain association, de-novo concept/mechanism invention, creative writing & dialogue | `(P, 0, B, 0)` | `(1,0,0,0)` | `(2,0,1,0)` | `(3,0,1,0)` | `advisor_for` when `access="read-only"` |
| `architecture_reasoning` | First-principles system architecture, formal/math derivation, rigorous critique or migration plan without editing code | `(S, P, 0, 0)` | `(0,1,0,0)` | `(1,2,0,0)` | `(2,3,0,0)` | `advisor_for` when `access="read-only"` |
| `code_implementation` | Write, fix, refactor, or test backend, CLI, library, algorithm, config, or analysis/experiment scripts | `(B, P, 0, B)` | `(0,1,0,0)` | `(1,2,0,1)` | `(1,3,0,1)` | local repo -> `access="write"` |
| `frontend_ui` | Implement, refactor, or style user-facing web/desktop UI views, CSS, charts, animations, and interactions | `(B, P, P, B)` | `(0,1,1,0)` | `(1,2,2,1)` | `(1,3,3,1)` | local repo -> `access="write"` |
| `code_audit` | Independent read-only code, PR, diff, branch, security, or release review/audit (including saving a report file) | `(B, P, 0, 0)` | `(0,1,0,0)` | `(1,2,0,0)` | `(1,3,0,0)` | `access="read-only"`, `read_only_review=true` |
| `exploration_forensics` | Read-only codebase mapping, log/DB/metric forensics, and runtime root-cause diagnosis without editing code | `(S, P, 0, B)` | `(0,1,0,0)` | `(1,2,0,1)` | `(2,3,0,1)` | `implies.local_only=true` |
| `web_research` | Gather, verify, and synthesize public web, literature, documentation, or vendor sources | `(S, B, 0, B)` | `(0,0,0,0)` | `(1,1,0,1)` | `(2,1,0,1)` | — |
| `visual_analysis` | Static offline inspection of images, photos, screenshots, or mockups (OCR, layout diff, fine-grained styling/material judgment) | `(B, B, P, 0)` | `(0,0,1,0)` | `(1,1,2,0)` | `(1,1,3,0)` | `needs_visual=true`, `needs_gui=false` |
| `text_processing` | Rule-defined structured text cleaning, translation, formatting, mechanical extraction, and summarization | `(B, 0, B, 0)` | `(0,0,0,0)` | `(1,0,1,0)` | `(1,0,1,0)` | — |
| `live_loop` | Low-latency observe-act loop attached to a live external text/WebSocket/API stream | `(0, S, 0, F2)` | `(0,0,0,2)` | `(0,1,0,2)` | `(0,2,0,2)` | evaluates `responsiveness` (`aa.ttfa`) |
| `gui_control` | Operate a live desktop, browser, or mobile GUI via screenshots, mouse clicks, and keyboard actions | `(0, B, S, F3)` | `(0,0,0,3)` | `(0,1,1,3)` | `(0,1,2,3)` | `needs_gui=true`, `needs_visual=true` |

## 2. Composite Tasks (`secondary_kind`)

Use `secondary_kind: "none"` (`secondary_level: 0`) for single-domain tasks (including reading files before coding, writing paired unit tests, or running git/pytest). Specify a distinct `secondary_kind` (`secondary_level: 1..3`) when one worker must deliver across two atomic domains in a single assignment:
- `code_implementation` (`L3`) + `creative_ideation` (`L3`) -> `(3, 3, 1, 1)` (inventing a novel cross-domain mechanism and implementing it in code).
- `code_implementation` (`L3`) + `frontend_ui` (`L1`) -> `(1, 3, 1, 1)` (coupled backend refactor plus a minor UI spacing tweak, without over-inflating `aesthetics` to `L3`).
- `frontend_ui` (`L2`) + `gui_control` (`L2`) -> `(1, 2, 2, 3)` with `needs_gui = true, needs_visual = true` (implementing UI and clicking through a live browser to verify).
- `visual_analysis` (`L3`) + `code_implementation` (`L2`) -> `(1, 2, 3, 1)` with `needs_visual = true` (fine-grained photo/styling classification plus writing a clustering analysis script).
