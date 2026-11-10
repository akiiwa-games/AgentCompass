# Judge & Scorer Workflow

The router separates fast structured task classification (**System-One Scorer**) from deterministic mathematical allocation (**Allocator**) and optional tie-break / delegation judgment (**Judge**). Parent agents submit only factual task descriptions (`objective`, `context`, `needs.local_only_reason`, `chain`); classification (`work_kind`) and difficulty scoring (`TaskProfile`) are performed automatically by the System-One model using [`classifier-cases.json`](classifier-cases.json).

## 1. Stage Overview

| Stage | Command | Model / Engine | When to Use |
| :--- | :--- | :--- | :--- |
| **`plan`** | `agentcompass plan --input task.json` | System-One Scorer (`gemini-3.8-flash-low` via `agy`) + deterministic `allocator.plan` | Diagnosis: ranks the bundles and shows why, without launching. To delegate, use `agentcompass roll --task "<text>"` (same classifier and allocator, then it launches). |
| **`score`** | `agentcompass score --input task.json` | The configured System-One scorer cascade (`jev` -> `claude_haiku_low` -> `codex_terra_low` -> `agy_flash_low`; returns `status: "blocked", error: "scorer_unavailable"` when all fail) | Standalone inspection and calibration testing of the System-One classifier. |
| **`scorer`** | `agentcompass scorer --action show\|set\|test` | Reads/writes `~/.agent-compass/scorer-config.json` (`ROUTER_SCORER_CONFIG`, type override `ROUTER_SCORER_TYPE`) | Choose the scorer: `agy_flash_low`, `jev` (TypeSafe `POST /v1/systemone`, key `jev_api_key` / `TYPESAFE_API_KEY` / `JEV_API_KEY`), `claude_haiku_low`, `codex_terra_low`, `openai_compatible`. Keys are printed masked only; `--api-key -` reads the key from stdin. |
| **`delegate`** | `agentcompass delegate --input task.json` | Two low-effort judges (`claude-sonnet-5-5 low` + `gpt-6.1-sol low`) | Only when uncertain whether to do work locally vs. delegate. |
| **`select`** | `agentcompass select --input task.json` | `plan` first; calls low-effort judges only when `decision.needs_judgment` is `true` | Optional tie-breaker when top two bundles are within `tie_margin` (`0.02`). |
| **`record`** | `agentcompass record --constraint user_override ...` | Deterministic ledger + reservation | When the user explicitly names the target model or effort in the current turn. |

## 2. System-One Classifier Output Schema (`TASK_PROFILE_SCHEMA`)

Produced automatically by `score_task` (via the System-One scorer cascade grounded in [`classifier-cases.json`](classifier-cases.json)). There is **no offline keyword fallback**: when all System-One classifiers are unavailable, `score`, `plan`, `roll`, and `tray.router_data.simulate` return `status: "blocked"`, `choice: "none"`, `error: "scorer_unavailable"`.

- `work_kind`: Canonical route (`computer_use`, `visual_analysis`, `frontend_implementation`, `tool_execution`, `bounded_bulk_work`, `debug_review`, `general_implementation`, `self_contained_code`, `exploration_analysis`, `research_packet`, `web_research`, `text_in_text_out`, `dot_execution`, `complex_build_plan`, `direction_reasoning`)
- `reasoning_depth` (`1..4`): `1=routine`, `2=hard`, `3=coupled`, `4=frontier`
- `code_complexity` (`0..3`): `0=no code`, `1=script/single-file`, `2=multi-file module`, `3=cross-cutting architecture`
- `axis_demand` (object, every key required, each `0..3`): how much the task needs of `divergence`, `global_rigor`, `module_precision` and `vision_tools`. The levels are stated in `allocator.FIVE_AXIS_DEMAND_LEVELS`, which is the one text the JSON classifiers' schema and the System-One (Jev) questions are built from. Economy is not a task demand: the router prices it from cost and live quota. No other field is turned into a demand; a profile without `axis_demand` is rejected.
- `needs_visual` (`bool`), `needs_gui` (`bool`), `local_only` (`bool`), `realtime_loop` (`bool`)
- `wants_fast` (`bool`): `true` only when the assignment affirmatively requests fast mode, priority tier, urgent/ASAP turnaround, or a pre-reset quota sprint (`"pre-reset sprint"`, `"before quota resets"`, `"重置前冲刺"`) for the task itself without negation (`"do not use fast mode"` or `"no rush"` -> `false`; runtime step adverbs such as `"click Save immediately"` -> `false`). Strictly required boolean in classifier outputs. Precedence in `normalize_task`: explicit caller `needs.fast` -> `task.fast` -> `task.wants_fast` -> `profile.wants_fast` -> `false`.
- `read_only_review` (`bool`): `true` only when the assignment is a read-only code, diff, branch, security, or release review/audit that inspects existing material and reports findings without writing or modifying repository production/test code (`code_complexity = 0`, even if saving a review report file to a specified output path). `false` when the worker implements, fixes, or refactors code (including fixing issues found by a prior review), runs operational commands, or performs general exploration/research. Strictly required boolean in classifier outputs; `read_only_review = true` with `code_complexity != 0` is rejected as contradictory by `validate_profile`. Used by `roll` and `plan_stage` to enforce independent reviewer selection (preventing a worker that authored or modified code from reviewing its own work).
- `multi_task` (`bool`) & `task_count` (`"1" | "2" | "3" | ">3"`): `multi_task: true` when the brief bundles 2+ separate deliverables that different workers could produce in parallel without waiting on each other; `false` (`task_count: "1"`) for a single deliverable with sequential steps.
- `expected_hours` (`0.05..24.0`), `hours_confidence` (`"low" | "medium" | "high"`), `urgency` (`"unknown" | "low" | "normal" | "high"`)
- `interaction_style`: `"engineering" | "exploration" | "advisory" | "text_only"`
- `reason` (optional): One-sentence justification of the classification and scores.

### Cross-Repo Sync Notes (for Magpie & Cindy)

| Field | Type | Semantic Contract |
| :--- | :--- | :--- |
| `wants_fast` | `boolean` | Replaces `FAST_REQUEST_WORDS` / `infer_fast_preference(objective)`. Set by System-One classifier (`TASK_PROFILE_SCHEMA` boolean / Jev `noul` question) or explicit caller flag (`--fast`, `needs.fast`, `task.fast`, `task.wants_fast`). |
| `read_only_review` | `boolean` | Replaces `READONLY_REVIEW_PHRASES` / `is_readonly_review_task` and `roll.py` `_REVIEW_RE` / `_AUTHORING_RE` / `is_review_title`. Gates independent-reviewer worker reuse so authoring workers never self-review. |
| `local_only` | `boolean` | Replaces `OBJECTIVE_LOCAL_RUNTIME_RE`, `LOCAL_MACHINE_RE`, `BOGUS_LOCAL_ONLY_RE`, and `valid_local_only_reason`. |
| `multi_task`, `task_count` | `boolean`, `"1"\|"2"\|"3"\|">3"` | Replaces `MULTI_TASK_MARK_RE` / `multi_task_marks(objective)`. |

When the System-One classifier runs on `plan`, its `work_kind` and `difficulty` (`reasoning_depth`) are authoritative and override any manual `work_kind` or `difficulty` supplied by the caller (`caller_classification_ignored`).

If `needs.local_only` is unset, the profile's `local_only: true` is also authoritative. The router copies the profile `reason` into `needs.local_only_reason` and emits `profile_local_only_applied`. Explicit caller values (`needs.local_only: true` or `false`, or `--local-only "<reason>"`) are respected directly without regex/length filtering.

Route `implies` fill `local_only` and `visual` only when both the caller and the profile left that flag unset. A classifier `local_only: false` on `research_packet` or `exploration_analysis` does not exclude Dot.

`needs_gui` adds the visual requirement. It rewrites `work_kind` to `computer_use` only when the profile did not name a real route and the caller kind is missing or generic (`general_implementation`, `tool_execution`, `debug_review`, `other_or_mixed`). A classifier route such as `general_implementation` stays that route.

## 3. Fit minus quota cost

Allocation has three layers. They do not mix.

| Layer | What it decides | What it ignores |
| :--- | :--- | :--- |
| **Eligible set** | Model settings, account health, host policy, modality, a demanded category with no data, and per-window reserves. | Fit and quota price. |
| **Fit** | How well the model's four category scores meet the task's demand, per demanded category: `min(score, target) - shortfall x max(0, target - score)`, averaged by demand. `target = 0.8 x demand / 3`, the anchor model's score scaled by the demand. Thinking categories scale by the model's measured effort curve. | Which pool pays. |
| **Quota economy** | Which window pays, and what other work that spend displaces: the part of the spend over the window's recovery line, priced in score units. 5-hour windows are feasibility only; long reset-period windows carry the price. | Model quality, except through the value ladder of the candidates that draw the window. |

```text
score = fit + w_continuity x continuity + boost - quota_cost
```

A temporary `boost` comes only from a user override. The order after the score is: wait before the start, earliest-expiring
quota, completion time, fewer USD, demand-weighted category level, then the harness place among siblings (same account, model,
effort and action; the best sibling keeps its score, a harness with no sibling is not penalized).

A bundle that draws several pools is gated by the tightest one: every applicable window must clear its reserve. `decision.system.quota_windows`
on a plan reports each window's price, room and reset clock.
