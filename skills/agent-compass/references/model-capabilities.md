# Model capability catalogue

Generated from `accounts.json` by `scripts/capability_catalog.py`; do not edit this view.

Public scores are conservative, low-confidence routing priors, not measured benchmarks or a vendor ranking. Tier and text-capability values are the checked-in calibrated baseline; modality flags describe admission only. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order, scoring weights and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier prior | R / C / E / A / V / G priors | Responsiveness | Visual admission |
| :--- | ---: | :--- | :--- | :--- |
| `beam-free` | 2.15 | 0.53 / 0.20 / 0.53 / 0.50 / 0.00 / 0.00 | 0.85 | False |
| `claude-fable-5-1` | 4.0 | 0.98 / 0.78 / 0.90 / 0.90 / 0.00 / 0.00 | 0.59 | False |
| `claude-haiku-4-5` | 1.21 | 0.55 / 0.00 / 0.59 / 0.57 / 0.45 / 0.32 | 0.00 | True |
| `claude-haiku-5-5` | 2.94 | 0.58 / 0.49 / 0.62 / 0.62 / 0.48 / 0.35 | 0.44 | True |
| `claude-opus-4-6` | 2.74 | 0.85 / 0.41 / 0.84 / 0.88 / 0.95 / 0.52 | 0.00 | True |
| `claude-opus-4-7` | 2.82 | 0.80 / 0.43 / 0.79 / 0.83 / 0.90 / 0.49 | 0.00 | True |
| `claude-opus-4-8` | 2.81 | 0.80 / 0.42 / 0.79 / 0.83 / 0.90 / 0.49 | 0.05 | True |
| `claude-opus-5-5` | 4.6 | 0.89 / 0.91 / 0.88 / 0.92 / 1.00 / 0.55 | 0.11 | True |
| `claude-sonnet-4-6` | 2.57 | 0.71 / 0.36 / 0.83 / 0.70 / 0.60 / 0.50 | 0.00 | True |
| `claude-sonnet-5-5` | 3.9 | 0.74 / 0.82 / 0.89 / 0.73 / 0.60 / 0.55 | 0.70 | True |
| `deepseek-v4-pro` | 2.86 | 0.78 / 0.47 / 0.72 / 0.75 / 0.00 / 0.00 | 0.00 | False |
| `deepseek-v4.1-flash-free` | 2.8 | 0.48 / 0.50 / 0.50 / 0.45 / 0.00 / 0.00 | 0.46 | False |
| `dot` | 4.2 | 0.80 / 0.79 / 0.80 / 0.78 / 0.00 / 0.00 | n/a | False |
| `doubao-seed-2-1-pro-260628` | 2.55 | 0.72 / 0.35 / 0.74 / 0.70 / 0.65 / 0.50 | default | True |
| `flint-40b-free` | 2.8 | 0.48 / 0.52 / 0.48 / 0.46 / 0.00 / 0.00 | 0.96 | False |
| `gemini-3.1-flash-lite` | 1.0 | 0.47 / 0.00 / 0.45 / 0.65 / 0.27 / 0.38 | 0.72 | True |
| `gemini-3.1-pro-preview` | 2.18 | 0.70 / 0.22 / 0.77 / 0.73 / 0.68 / 0.56 | 0.57 | True |
| `gemini-3.7-flash` | 2.83 | 0.47 / 0.49 / 0.45 / 0.65 / 0.27 / 0.38 | 0.94 | True |
| `gemini-3.8-flash-high` | 3.0 | 0.52 / 0.47 / 0.50 / 0.72 / 0.30 / 0.42 | 0.49 | True |
| `gemini-4-argon-high` | 4.0 | 0.78 / 0.83 / 0.86 / 0.81 / 0.75 / 0.62 | 0.00 | True |
| `gemini-4-carbon` | 4.35 | 0.70 / 0.83 / 0.77 / 0.73 / 0.68 / 0.56 | 0.00 | True |
| `glm-5.2` | 3.0 | 0.68 / 0.52 / 0.70 / 0.68 / 0.00 / 0.00 | 0.00 | False |
| `glm-5.3` | 3.08 | 0.72 / 0.55 / 0.74 / 0.72 / 0.60 / 0.55 | 0.00 | True |
| `glm-5.3-flash-free` | 2.85 | 0.50 / 0.52 / 0.52 / 0.52 / 0.40 / 0.30 | 0.00 | True |
| `gpt-5.6-luna` | 2.55 | 0.55 / 0.35 / 0.54 / 0.59 / 0.50 / 0.45 | 0.64 | True |
| `gpt-5.6-sol` | 3.34 | 0.67 / 0.54 / 0.58 / 0.70 / 0.65 / 0.75 | 0.66 | True |
| `gpt-5.6-terra` | 2.59 | 0.69 / 0.36 / 0.66 / 0.71 / 0.67 / 0.74 | 0.74 | True |
| `gpt-6-astra` | 4.2 | 0.80 / 0.79 / 0.80 / 0.78 / 0.80 / 0.95 | 0.00 | True |
| `gpt-6-luna` | 2.61 | 0.58 / 0.47 / 0.57 / 0.62 / 0.55 / 0.50 | 0.66 | True |
| `gpt-6-pro` | 5.0 | 1.00 / 0.90 / 0.78 / 0.95 / 0.00 / 0.00 | default | False |
| `gpt-6-sol` | 3.48 | 0.69 / 0.66 / 0.60 / 0.72 / 0.68 / 0.80 | 0.73 | True |
| `gpt-6.1-sol` | 3.6 | 0.71 / 0.85 / 0.62 / 0.74 / 0.70 / 0.82 | 0.00 | True |
| `gpt-6.1-terra` | 3.2 | 0.73 / 0.65 / 0.71 / 0.75 / 0.72 / 0.80 | 0.65 | True |
| `gpt-oss-120b` | 1.0 | 0.55 / 0.55 / 0.55 / 0.50 / 0.00 / 0.00 | 0.65 | False |
| `grok-4.5` | 2.79 | 0.53 / 0.42 / 0.54 / 0.56 / 0.00 / 0.00 | 0.66 | False |
| `grok-4.6` | 3.02 | 0.53 / 0.54 / 0.54 / 0.56 / 0.00 / 0.00 | 0.13 | False |
| `grok-4.7` | 3.4 | 0.59 / 0.58 / 0.60 / 0.62 / 0.00 / 0.00 | 0.20 | False |
| `grok-4.7-build-fast` | 3.4 | 0.54 / 0.58 / 0.54 / 0.55 / 0.00 / 0.00 | default | False |
| `grok-bot` | 4.6 | 0.89 / 0.91 / 0.88 / 0.92 / 1.00 / 0.55 | n/a | False |
| `kimi-k2.6` | 2.48 | 0.71 / 0.34 / 0.75 / 0.67 / 0.61 / 0.45 | 0.00 | True |
| `kimi-k2.7-code` | 2.22 | 0.71 / 0.27 / 0.75 / 0.67 / 0.61 / 0.45 | 0.00 | True |
| `kimi-k3` | 3.14 | 0.74 / 0.61 / 0.80 / 0.70 / 0.62 / 0.45 | 0.00 | True |
| `kimi-k3.1-pro` | 3.47 | 0.79 / 0.69 / 0.83 / 0.74 / 0.68 / 0.50 | 0.00 | True |
| `mimo-v2.6-flash-free` | 3.1 | 0.51 / 0.58 / 0.51 / 0.50 / 0.40 / 0.30 | 0.00 | True |
| `mimo-v2.6-pro` | 3.15 | 0.72 / 0.56 / 0.70 / 0.69 / 0.60 / 0.45 | 0.00 | True |
| `minimax-m3` | 2.05 | 0.73 / 0.29 / 0.75 / 0.70 / 0.60 / 0.45 | 0.00 | True |
| `minimax-m3.1-flash` | 2.73 | 0.66 / 0.47 / 0.68 / 0.65 / 0.55 / 0.40 | 0.00 | True |
| `mistral-large-4` | 2.45 | 0.74 / 0.40 / 0.73 / 0.71 / 0.65 / 0.45 | 0.27 | True |
| `muse-wonder-1.0` | 3.21 | 0.77 / 0.61 / 0.79 / 0.73 / 0.72 / 0.48 | 0.79 | True |
| `nemotron-3-ultra-free` | 1.34 | 0.65 / 0.70 / 0.65 / 0.63 / 0.00 / 0.00 | 0.86 | False |
| `nemotron-3.5-lightning-free` | 1.29 | 0.50 / 0.54 / 0.50 / 0.48 / 0.00 / 0.00 | 0.64 | False |
| `nemotron-3.5-super-vl-free` | 1.5 | 0.62 / 0.13 / 0.63 / 0.62 / 0.65 / 0.45 | 0.70 | True |
| `qwen3-coder-next` | 1.71 | 0.70 / 0.82 / 0.65 / 0.66 / 0.00 / 0.00 | 0.92 | False |
| `qwen3-flash-2025-07-28` | 2.8 | 0.55 / 0.55 / 0.55 / 0.50 / 0.00 / 0.00 | default | False |
| `qwen3-max` | 1.15 | 0.70 / 0.58 / 0.68 / 0.67 / 0.58 / 0.43 | default | True |
| `qwen3.6-plus` | 2.14 | 0.70 / 0.25 / 0.68 / 0.67 / 0.58 / 0.43 | 0.00 | True |
| `qwen3.7-max` | 2.33 | 0.70 / 0.35 / 0.68 / 0.67 / 0.58 / 0.43 | 0.67 | True |
| `qwen3.8-max` | 3.14 | 0.78 / 0.65 / 0.75 / 0.74 / 0.65 / 0.48 | 0.00 | True |

R/C/E/A/V/G = reasoning, coding, exploration, anti-hallucination, visual and GUI.
Responsiveness (`0..1`, per-effort in `effort_curves.<model>.responsiveness`) measures per-turn live-loop speed (`T_turn = (ttft_s + 400 / output_tps) * harness_overhead`, where AA `ttft_s` is `headline.timeToFirstAnswerToken` including single-turn thinking time; mapped linearly from `[4.0s -> 1.0, 45.0s -> 0.0]` by `scoring.responsiveness` and refreshed offline via `scripts/refresh_speed_data.py`). It is active only when the task profile marks `realtime_loop: true` (`floor = 0.50`, evaluated per `(model, effort)` with automatic step-down to a faster effort when the configured effort is below floor, within the range in [Model names, efforts and pins](router-internals.md#model-names-efforts-and-pins)); models without community speed data use the conservative policy `default` (`0.52`, `responsiveness: default`), and packet/web channels (`dot`, `grok-bot`) are `n/a` (`not_applicable`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated `preferred` order. `capability_policy` in a local fleet can replace `preferred`, `strict_order` and `restrict_models`. A restriction is state, not an explanation string. The judge receives this effective policy and its model data.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
