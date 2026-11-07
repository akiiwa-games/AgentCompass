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
- `context_scale` (`1..3`): `1=localized`, `2=multi-module`, `3=repo-wide/long-horizon`
- `divergence` (`1..3`): `1=convergent execution against a defined spec/bug`, `2=comparative design/trade-off evaluation or multi-hypothesis root-cause diagnosis`, `3=divergent scheme formulation, creative research, or architectural innovation`
- `hallucination_risk` (`1..3`): `1=verifiable by tests`, `2=moderate synthesis`, `3=strict factual/contractual accuracy`
- `error_cost` (`1..3`): `3` sets `needs.critical = true` (`+0.3` ability floor headroom)
- `needs_visual` (`bool`), `needs_gui` (`bool`), `local_only` (`bool`), `realtime_loop` (`bool`)
- `wants_fast` (`bool`): `true` only when the assignment affirmatively requests fast mode, priority tier, or urgent/ASAP turnaround for the task itself without negation (`"do not use fast mode"` or `"no rush"` -> `false`; runtime step adverbs such as `"click Save immediately"` -> `false`). Precedence in `normalize_task`: explicit caller `needs.fast` -> `task.fast` -> `task.wants_fast` -> `profile.wants_fast` -> `false`.
- `read_only_review` (`bool`): `true` only when the assignment is a read-only code, diff, branch, security, or release review/audit that inspects existing material and reports findings without writing or modifying repository code (`code_complexity = 0`). `false` when the worker implements, fixes, or refactors code (including fixing issues found by a prior review), runs operational commands, or performs general exploration/research. Used by `roll` and `plan_stage` to enforce independent reviewer selection (preventing a worker that authored or modified code from reviewing its own work).
- `multi_task` (`bool`) & `task_count` (`"1" | "2" | "3" | ">3"`): `multi_task: true` when the brief bundles 2+ separate deliverables that different workers could produce in parallel without waiting on each other; `false` (`task_count: "1"`) for a single deliverable with sequential steps.
- `expected_hours` (`0.05..24.0`), `hours_confidence` (`"low" | "medium" | "high"`), `urgency` (`"unknown" | "low" | "normal" | "high"`)
- `interaction_style`: `"engineering" | "exploration" | "advisory" | "text_only"`
- `reason` (optional in legacy fixtures): One-sentence justification of the classification and scores.

### Cross-Repo Sync Notes (for Magpie & Cindy)

| Field | Type | Default (when omitted in legacy fixtures) | Semantic Contract |
| :--- | :--- | :--- | :--- |
| `wants_fast` | `boolean` | `false` | Replaces `FAST_REQUEST_WORDS` / `infer_fast_preference(objective)`. Set by System-One classifier (`TASK_PROFILE_SCHEMA` boolean / Jev `noul` question) or explicit caller flag (`--fast`, `needs.fast`, `task.fast`, `task.wants_fast`). |
| `read_only_review` | `boolean` | `false` (or `work_kind == "debug_review" and code_complexity == 0` for legacy profiles without the key) | Replaces `READONLY_REVIEW_PHRASES` / `is_readonly_review_task` and `roll.py` `_REVIEW_RE` / `_AUTHORING_RE` / `is_review_title`. Gates independent-reviewer worker reuse so authoring workers never self-review. |
| `divergence` | `integer (1..3)` | `default_divergence(kind, style)` (`3` for `complex_build_plan` / `direction_reasoning` / `advisory`, else `1`) | Replaces `DIVERGENT_WORDS` / `infer_divergence(objective, ...)`. |
| `local_only` | `boolean` | `resolve_modality_needs` (`needs.local_only` -> `profile.local_only` -> `route.implies.local_only` -> `false`) | Replaces `OBJECTIVE_LOCAL_RUNTIME_RE`, `LOCAL_MACHINE_RE`, `BOGUS_LOCAL_ONLY_RE`, and `valid_local_only_reason`. |
| `multi_task`, `task_count` | `boolean`, `"1"\|"2"\|"3"\|">3"` | `false`, `"1"` | Replaces `MULTI_TASK_MARK_RE` / `multi_task_marks(objective)`. |

When the System-One classifier runs on `plan`, its `work_kind` and `difficulty` (`reasoning_depth`) are authoritative and override any manual `work_kind` or `difficulty` supplied by the caller (`caller_classification_ignored`).

If `needs.local_only` is unset, the profile's `local_only: true` is also authoritative. The router copies the profile `reason` into `needs.local_only_reason` and emits `profile_local_only_applied`. Explicit caller values (`needs.local_only: true` or `false`, or `--local-only "<reason>"`) are respected directly without regex/length filtering.

Route `implies` fill `local_only` and `visual` only when both the caller and the profile left that flag unset. A classifier `local_only: false` on `research_packet` or `exploration_analysis` does not exclude Dot.

`needs_gui` adds the visual requirement. It rewrites `work_kind` to `computer_use` only when the profile did not name a real route and the caller kind is missing or generic (`general_implementation`, `tool_execution`, `debug_review`, `other_or_mixed`). A classifier route such as `general_implementation` stays that route.

## 3. Value minus cost

Allocation has three layers. They do not mix.

| Layer | What it decides | What it ignores |
| :--- | :--- | :--- |
| **Eligible set** | Model settings, account health, host policy, modality, ability floor, and per-window reserves. | Score weights and quota price. |
| **Value** | How good this model is for this task on this harness and effort: weighted fit and ability, times `harness_factor`, plus continuity, reset bonus, free-usage, boost and quality bonus. | Which pool pays. |
| **Cost** | Which pool pays, and what other work that spend displaces. | Model quality, except through the exchange rate below. |

```text
value = harness_factor × (w_fit·fit + w_ability·ability) + w_continuity·continuity
        + reset_bonus + free_usage + boost + quality_bonus + fast_bonus
score = value − opportunity_cost − effort_penalty − overqualification − deferred_start_penalty
```

`harness_factor` defaults to `1.0` (`scoring.harness_factors`). The same model on two harnesses stays the same value unless a rule says the harness itself is worse. Antigravity Claude starts at `0.9` (200k context and a different tool surface). Codex versus Pi is not that rule.

`harness_order` still runs after the score, and only among siblings (same account, model, effort and action). The best sibling keeps its score. Each place behind subtracts `harness_step` (`0.03`). As a coefficient on the fit/ability value that subtraction is `harness_order_factor = 1 − harness_step × places / value`. The order flips by route and by interaction style, and a harness with no sibling is not penalized, so a single global coefficient cannot replace the list.

`scoring.opportunity_exchange` is the only quality-versus-quota rate. `exchange_rate` reads `score_per_displaced_usd` (`0.012` score per USD of expected displaced subscription capacity) and `score_per_displaced_worker_hour` (`0.06` score per displaced worker-hour). One score point is about `$83.33` of displaced USD, or `16.67` worker-hours. The worker-hour rate is used only when a window has no USD capacity. Top-level `shortage_loss_per_usd` and `shortage_loss_per_worker_hour` are a deprecated fallback for a policy that has not set the named field. When both are set and differ, the named field wins and `decision.warnings` says the old key was ignored.

A bundle that draws several pools is gated by the tightest one: every applicable window must clear its reserve. Opportunity cost is the sum of each window's shortage charge at this exchange. `capacity`, the expected percent and the reset bonus follow the binding window, the window with the highest opportunity cost. `decision.system.opportunity_exchange` on a plan repeats the rate and this rule.
