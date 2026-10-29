# Judge & Scorer Workflow

The router separates fast structured task classification (**System-One Scorer**) from deterministic mathematical allocation (**Allocator**) and optional tie-break / delegation judgment (**Judge**). Parent agents submit only factual task descriptions (`objective`, `context`, `needs.local_only_reason`, `chain`); classification (`work_kind`) and difficulty scoring (`TaskProfile`) are performed automatically by the System-One model using [`classifier-cases.json`](classifier-cases.json).

## 1. Stage Overview

| Stage | Command | Model / Engine | When to Use |
| :--- | :--- | :--- | :--- |
| **`plan`** | `route_judge.py plan --input task.json` | System-One Scorer (`gemini-3.8-flash-low` via `agy`) + deterministic `allocator.plan` | **Standard path** before launching any subagent/worker. Automatically classifies and scores `task.objective`. |
| **`score`** | `route_judge.py score --input task.json` | The configured scorer (default `gemini-3.8-flash-low` via `agy`; then `agy` when another scorer fails and `fallback_to_agy` is on; then deterministic `allocator.infer_task_profile`) | Standalone inspection and calibration testing of the System-One classifier. |
| **`scorer`** | `route_judge.py scorer --action show\|set\|test` | Reads/writes `~/.route-manager/scorer-config.json` (`ROUTER_SCORER_CONFIG`, type override `ROUTER_SCORER_TYPE`) | Choose the scorer: `agy_flash_low`, `jev` (TypeSafe `POST /v1/systemone`, key `jev_api_key` / `TYPESAFE_API_KEY` / `JEV_API_KEY`), `claude_haiku_low`, `codex_terra_low`, `openai_compatible`, `heuristic`. Keys are printed masked only; `--api-key -` reads the key from stdin. |
| **`delegate`** | `route_judge.py delegate --input task.json` | Two low-effort judges (`claude-sonnet-5-5 low` + `gpt-6.1-sol low`) | Only when uncertain whether to do work locally vs. delegate. |
| **`select`** | `route_judge.py select --input task.json` | `plan` first; calls low-effort judges only when `decision.needs_judgment` is `true` | Optional tie-breaker when top two bundles are within `tie_margin` (`0.02`). |
| **`record`** | `route_judge.py record --constraint user_override ...` | Deterministic ledger + reservation | When the user explicitly names the target model or effort in the current turn. |

## 2. System-One Classifier Output Schema

Produced automatically by `score_task` (via `agy --model gemini-3.8-flash-low --output-format json --json-schema` grounded in [`classifier-cases.json`](classifier-cases.json)):

- `work_kind`: Canonical route (`computer_use`, `visual_analysis`, `frontend_implementation`, `tool_execution`, `bounded_bulk_work`, `debug_review`, `general_implementation`, `self_contained_code`, `exploration_analysis`, `research_packet`, `web_research`, `text_in_text_out`, `dot_execution`, `complex_build_plan`, `direction_reasoning`)
- `reasoning_depth` (`1..4`): `1=routine`, `2=hard`, `3=coupled`, `4=frontier`
- `code_complexity` (`0..3`): `0=no code`, `1=script/single-file`, `2=multi-file module`, `3=cross-cutting architecture`
- `context_scale` (`1..3`): `1=localized`, `2=multi-module`, `3=repo-wide/long-horizon`
- `divergence` (`1..3`): `1=convergent execution against a defined spec/bug`, `2=comparative design/trade-off evaluation`, `3=divergent scheme formulation, creative research, or architectural innovation`
- `hallucination_risk` (`1..3`): `1=verifiable by tests`, `2=moderate synthesis`, `3=strict factual/contractual accuracy`
- `error_cost` (`1..3`): `3` sets `needs.critical = true` (`+0.3` ability floor headroom)
- `needs_visual` (`bool`), `needs_gui` (`bool`), `local_only` (`bool`)
- `interaction_style`: `"engineering" | "exploration" | "advisory" | "text_only"`
- `reason`: One-sentence justification of the classification and scores.

When the System-One classifier runs on `plan`, its `work_kind` and `difficulty` (`reasoning_depth`) are authoritative and override any manual `work_kind` or `difficulty` supplied by the caller (`caller_classification_ignored`).

If `needs.local_only` is unset, the profile's `local_only: true` is also authoritative. The router copies the profile `reason` into `needs.local_only_reason` and emits `profile_local_only_applied`; it does not apply the caller-reason validator to this judgment. The deterministic fallback supplies its own profile `reason` and follows the same rule. Explicit caller values win, including `needs.local_only: false` in `plan` input, which keeps Dot eligible subject to route and modality constraints. Caller-supplied `true` still uses the existing local-dependency validation. No separate negative flag exists on `roll`.

Route `implies` fill `local_only` and `visual` only when both the caller and the profile left that flag unset. A classifier `local_only: false` on `research_packet` or `exploration_analysis` does not exclude Dot. When the System-One scorer is unavailable, the heuristic profile sets `local_only` (and names it in `reason`) when the objective or context matches a local runtime (`ssh`, `pytest`, `daemon`, `localhost`, `credentials`, and the same set the caller validator uses) or a local path (`C:\`, `~/`, a working tree, local repo, or local daemon). Caller true or false still wins over that inference and over route implies.

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
