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
