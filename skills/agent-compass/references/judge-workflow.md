# Judge & Scorer Workflow

The router separates fast structured task classification (**System-One Scorer**) from deterministic mathematical allocation (**Allocator**) and optional tie-break / delegation judgment (**Judge**). Parent agents submit only factual task descriptions (`objective`, `context`, `needs.local_only_reason`, `chain`); classification (`work_kind`) and difficulty scoring (`TaskProfile`) are performed automatically by the System-One model using [`classifier-cases.json`](classifier-cases.json).

## 1. Stage Overview

| Stage | Command | Model / Engine | When to Use |
| :--- | :--- | :--- | :--- |
| **`plan`** | `agentcompass plan --input task.json` | The configured System-One scorer cascade (see `score` below) + deterministic `allocator.plan` | Diagnosis: ranks the bundles and shows why, without launching. To delegate, use `agentcompass roll --task "<text>"` (same classifier and allocator, then it launches). |
| **`score`** | `agentcompass score --input task.json` | The configured System-One scorer cascade (`jev` when keyed -> `claude_haiku_low` -> `codex_terra_low` -> `agy_flash_low`, led by the chosen scorer type when that is another one; returns `status: "blocked", error: "scorer_unavailable"` when all fail) | Standalone inspection and calibration testing of the System-One classifier. |
| **`scorer`** | `agentcompass scorer --action show\|set\|test` | Reads/writes `~/.agent-compass/scorer-config.json` (`ROUTER_SCORER_CONFIG`, type override `ROUTER_SCORER_TYPE`) | Choose the scorer: `agy_flash_low`, `jev` (TypeSafe `POST /v1/systemone`, key `jev_api_key` / `TYPESAFE_API_KEY` / `JEV_API_KEY`), `gemini_decision` ([Gemini Decisions API](gemini-decisions-api.md) `POST /v1beta/decisions`, key `gemini_api_key` / `GEMINI_API_KEY`; runs only when chosen), `claude_haiku_low`, `codex_terra_low`, `openai_compatible`. Keys are printed masked only; `--api-key -` reads the key from stdin. |
| **`delegate`** | `agentcompass delegate --input task.json` | Two low-effort judges (`claude-sonnet-5-5 low` + `gpt-6.1-sol low`) | Only when uncertain whether to do work locally vs. delegate. |
| **`select`** | `agentcompass select --input task.json` | `plan` first; calls low-effort judges only when `decision.needs_judgment` is `true` | Optional tie-breaker when top two bundles are within `tie_margin` (`0.02`). |
| **`record`** | `agentcompass record --constraint user_override ...` | Deterministic ledger + reservation | When the user explicitly names the target model or effort in the current turn. |

## 2. System-One Classifier Output Schema (`TASK_PROFILE_SCHEMA`)

Produced automatically by `score_task` (via the System-One scorer cascade grounded in [`classifier-cases.json`](classifier-cases.json)). There is **no offline keyword fallback**: when all System-One classifiers are unavailable, `score`, `plan`, `roll`, and `tray.router_data.simulate` return `status: "blocked"`, `choice: "none"`, `error: "scorer_unavailable"`.

- `work_kind`: One of the 11 atomic routes (`creative_ideation`, `architecture_reasoning`, `code_implementation`, `frontend_ui`, `code_audit`, `exploration_forensics`, `web_research`, `visual_analysis`, `text_processing`, `live_loop`, `gui_control`).
- `primary_level` (`1..3`): Task depth (`1=routine/localized`, `2=standard module/analysis`, `3=frontier/coupled/fine-grained`).
- `secondary_kind` (`"none"` or a distinct atomic `work_kind`) & `secondary_level` (`0..3`): Optional second domain for composite tasks (`secondary_level = 0` when `secondary_kind == "none"`).
- `access`: `"read-only" | "write"` (`code_audit` is always `"read-only"`, even when saving a review report file; local repository `code_implementation` and `frontend_ui` are always `"write"`).
- `local_only` (`bool`), `needs_visual` (`bool`)
- `wants_fast` (`bool`): `true` only when the assignment affirmatively requests fast mode, priority tier, urgent/ASAP turnaround, or a pre-reset quota sprint (`"pre-reset sprint"`, `"before quota resets"`, `"重置前冲刺"`) for the task itself without negation (`"do not use fast mode"` or `"no rush"` -> `false`; runtime step adverbs such as `"click Save immediately"` -> `false`). Precedence in `normalize_task`: explicit caller `needs.fast` -> `task.fast` -> `task.wants_fast` -> `profile.wants_fast` -> `false`.
- `expected_hours` (`0.05..24.0`), `task_count` (`"1" | "2" | "3" | ">3"`)
- `reason` (optional): One-sentence justification when supplied by a caller profile.
- **Deterministic projections in `task_intake.validate_profile`**:
  - `axis_demand` (`0..3` over `creativity`, `coding`, `aesthetics`, `tool_operation`): projected via `task_intake.project_axis_demand(work_kind, primary_level, secondary_kind, secondary_level)` using `WORK_KIND_SIGNATURES` and the tropical max envelope `max(Phi(k1, s1), Phi(k2, s2))`. Economy is not a task demand: the router prices it from cost and live quota.
  - `needs_gui` (`bool`): `true` when `"gui_control"` is in `(work_kind, secondary_kind)` (also promotes `needs_visual = true`, as does `"visual_analysis"`).
  - `read_only_review` (`bool`): `true` when `work_kind == "code_audit"`, used by `roll` and `plan_stage` to enforce independent reviewer selection so authoring workers never self-review.
  - `multi_task` (`bool`): `task_count != "1"`.

### Cross-Repo Sync Notes (for Magpie & Cindy)

| Field | Type | Semantic Contract |
| :--- | :--- | :--- |
| `wants_fast` | `boolean` | Replaces `FAST_REQUEST_WORDS` / `infer_fast_preference(objective)`. Set by System-One classifier (`TASK_PROFILE_SCHEMA` boolean / question-API `noul` question) or explicit caller flag (`--fast`, `needs.fast`, `task.fast`, `task.wants_fast`). |
| `read_only_review` | `boolean` | Derived from `work_kind == "code_audit"` (or explicit caller boolean). Gates independent-reviewer worker reuse so authoring workers never self-review. |
| `local_only` | `boolean` | Replaces `OBJECTIVE_LOCAL_RUNTIME_RE`, `LOCAL_MACHINE_RE`, `BOGUS_LOCAL_ONLY_RE`, and `valid_local_only_reason`. |
| `multi_task`, `task_count` | `boolean`, `"1"\|"2"\|"3"\|">3"` | Replaces `MULTI_TASK_MARK_RE` / `multi_task_marks(objective)`; `multi_task` is `task_count != "1"`. |

When the System-One classifier runs on `plan`, its `work_kind` and derived `difficulty` (`primary_level`) are authoritative and override any manual `work_kind` or `difficulty` supplied by the caller (`caller_classification_ignored`).

If `needs.local_only` is unset, the profile's `local_only: true` is also authoritative. The router copies the profile `reason` into `needs.local_only_reason` and emits `profile_local_only_applied`. Explicit caller values (`needs.local_only: true` or `false`, or `--local-only "<reason>"`) are respected directly without regex/length filtering.

Route `implies` fill `local_only` and `visual` only when both the caller and the profile left that flag unset. A classifier `local_only: false` on `exploration_forensics` does not exclude Dot.

`needs_gui` adds the visual requirement. It rewrites `work_kind` to `gui_control` only when the profile did not name a real route and the caller kind is missing or generic (`code_implementation`, `other_or_mixed`). A classifier route such as `code_implementation` stays that route.

## 3. Fit minus quota cost

Allocation has three layers. They do not mix.

| Layer | What it decides | What it ignores |
| :--- | :--- | :--- |
| **Eligible set** | Model settings, account health, host policy, modality (`needs_visual`, `needs_gui`, or `tool_operation >= 3`), a demanded category (`>= 1`) with no data, and per-window reserves. | Fit and quota price. |
| **Fit** | How well the model's four category scores (`creativity`, `coding`, `aesthetics`, `tool_operation`, in panel standard deviations at that effort) meet the task's demand: `sum_a w_a * (min(z_a, c_a) - t1_a) / sum_a w_a`, where `t1_a` (the panel's weakest) is the zero point, `t2_a` is the panel median, `t3_a` is the anchor (`0.0`), and the cap `c_a` is the demanded level's target at levels 1 and 2 and the category's ceiling (the best measured model) at level 3. `aesthetics` evaluates `design` at level 1 and `min(design, image_recognition)` at levels 2 and 3; `tool_operation` level 1 asks for nothing, level 2 evaluates `responsiveness`, and level 3 evaluates `min(responsiveness, image_recognition)`. | Which pool pays. |
| **Quota economy** | Which window pays, and what other work that spend displaces: the part of the spend over the window's recovery line, priced in panel-SD score units. 5-hour windows are feasibility only; long reset-period windows carry the price. | Model quality, except through the value ladder of the candidates that draw the window. |

```text
score = fit + continuity + boost - quota_cost
```

`continuity` is the largest positive `t3 - t2` among the categories at the task's highest demand level when resuming a coupled (`coupling` `medium` or `high`: auto-matched continuation or `--resume`) worker within its context rotation limit, else `0`. A temporary `boost` comes only from a user override. The order after the score is: wait before the start, earliest-expiring
quota, completion time, fewer USD, demand-weighted category level, then the harness place among siblings (same account, model,
effort and action; the best sibling keeps its score, a harness with no sibling is not penalized).

A bundle that draws several pools is gated by the tightest one: every applicable window must clear its reserve. `decision.system.quota_windows`
on a plan reports each window's price, room and reset clock.
