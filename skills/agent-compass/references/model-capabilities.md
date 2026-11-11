# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (divergence, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Divergence | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.57 | -3.42 | -3.00 | -3.54 | missing! | - | unknown |
| `claude-fable-5-1` | 0.03 | -0.79 | -0.53 | -0.65 | -0.00 | 36.11 | yes |
| `claude-fable-5-2` | 0.03 | 0.20u | 0.11u | 0.24u | 0.00u | 28.888 | yes |
| `claude-haiku-4-5` | -0.74 | -3.45 | missing! | -5.45 | -2.37~ | 26.67 | yes |
| `claude-haiku-5-5` | -0.67 | -4.08 | missing! | -2.03 | -2.28~ | 2.667 | yes |
| `claude-opus-4-6` | -0.02 | -0.49 | -1.39~ | -2.03 | -0.08~ | 37.727 | yes |
| `claude-opus-4-7` | -0.04 | -0.74 | -1.39~ | -2.25 | -0.13~ | 37.727 | yes |
| `claude-opus-4-8` | -0.19 | -1.24 | -1.39~ | -2.51 | -0.61~ | 37.727 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 22.859 | yes |
| `claude-sonnet-4-6` | -0.27 | -1.80 | -1.82~ | -2.32 | -0.92~ | 25.4 | yes |
| `claude-sonnet-5-5` | -0.03 | -2.02 | -1.13 | -0.57 | -1.52 | 12.825 | yes |
| `deepseek-v4-pro` | -0.34 | -1.93 | missing! | -2.80 | -1.34~ | 8.735 | no |
| `deepseek-v4.1-flash-free` | -0.47 | -2.27 | -2.07 | -1.30 | -1.61 | 1.084 | yes |
| `dot` | 0.10 | -2.03~ | -0.05u | -0.24u | -1.68u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.32u | -2.79u | -2.66 | -2.85u | - | unknown |
| `flint-40b-free` | - | -3.32u | -2.79u | -2.89u | -2.85u | - | unknown |
| `gemini-3.1-flash-lite` | -0.59 | -2.71 | -3.85~ | -5.52 | -2.00~ | 2.009 | yes |
| `gemini-3.1-pro-preview` | -0.15 | -0.93 | -3.00~ | -2.67 | -0.86 | 12.222 | yes |
| `gemini-3.7-flash` | -0.13 | -0.79 | -2.55 | -1.42 | -0.14~ | 5.889 | yes |
| `gemini-3.8-flash-high` | -0.07 | -0.74 | -2.21 | -1.47 | -0.12 | 4.953 | yes |
| `gemini-4-argon-high` | 0.13 | 0.24 | -0.81 | -0.68 | -0.20u | 6.658 | yes |
| `gemini-4-carbon` | 0.17 | 0.04 | 0.00u | 0.04 | -1.06u | 8.66 | yes |
| `glm-5.2` | - | -2.01~ | missing! | -1.98 | missing! | 4.55 | no |
| `glm-5.3` | - | -1.78 | -1.95 | -1.53 | missing! | 4.378 | no |
| `glm-5.3-flash-free` | -0.25 | -2.16 | missing! | -1.68 | -1.42 | 1.5 | yes |
| `gpt-5.6-luna` | -0.62 | -2.99~ | -2.35~ | -3.03 | -1.77~ | 1.811 | yes |
| `gpt-5.6-sol` | 0.06 | -1.38~ | -1.56~ | -0.88 | -1.10~ | 25.95 | yes |
| `gpt-5.6-terra` | 0.06 | -2.45~ | -2.36~ | -2.69 | -1.43~ | 13.929 | yes |
| `gpt-6-astra` | 0.10 | -2.03 | -0.05u | -0.24u | -1.04 | 28.853 | yes |
| `gpt-6-luna` | -0.62 | -3.32 | -2.35u | -2.30 | -2.85u | 0.885 | yes |
| `gpt-6-pro` | 0.10 | 0.00u | 0.05u | 0.00u | -2.59u | - | yes |
| `gpt-6-sol` | -0.02 | -2.54u | -0.94u | -1.31 | -0.73u | 17.226 | yes |
| `gpt-6.1-sol` | 0.06 | -2.02 | -0.41u | -0.39 | -0.73 | 6.125 | yes |
| `gpt-6.1-terra` | -0.02 | -2.36~ | -0.66u | -1.49 | -0.73 | 5.393 | yes |
| `gpt-oss-120b` | -1.24 | -6.32 | missing! | -9.73 | -2.37~ | 12.827 | no |
| `grok-4.5` | -0.32 | -1.73 | -2.17~ | -2.18 | -0.79~ | 9.228 | yes |
| `grok-4.6` | -0.40 | -1.97 | -2.01~ | -1.79 | -1.39~ | 12.971 | yes |
| `grok-4.7` | -0.10 | -2.60 | -1.54 | -1.93 | -1.98~ | 10.778 | yes |
| `grok-4.7-build-fast` | -0.32 | -1.73~ | -2.17~ | -2.18~ | -0.79~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | 0.00~ | -0.73u | - | yes |
| `kimi-k2.6` | -0.36 | -2.24 | -3.13~ | -2.47 | -0.92~ | 4.6 | yes |
| `kimi-k2.7-code` | -0.36 | -2.24~ | -3.13~ | -2.71 | -0.92~ | 6.012 | yes |
| `kimi-k3` | 0.02 | -1.78~ | -2.44~ | -0.50 | -2.22~ | 5.742 | yes |
| `kimi-k3.1-pro` | 0.02 | -0.72~ | -1.31~ | -0.24 | -2.22 | 9.75 | yes |
| `mimo-v2.6-flash-free` | -0.47 | -3.31 | missing! | -1.40 | -2.74 | 0.275 | yes |
| `mimo-v2.6-pro` | -0.21 | -1.82 | -1.58 | -1.12 | -1.35 | 0.63 | yes |
| `minimax-m3` | -0.56 | -3.20 | -2.79 | -2.89 | -2.04 | 5.829 | yes |
| `minimax-m3.1-flash` | -0.47 | -2.77~ | missing! | -1.58 | -2.14 | 1.582 | yes |
| `mistral-large-4` | -0.70 | -4.29 | missing! | -2.28 | -1.94 | 4.364 | yes |
| `muse-wonder-1.0` | -0.04 | -1.30 | -1.47 | -0.66 | -0.73 | 21.771 | yes |
| `nemotron-3-ultra-free` | -0.62 | -3.37~ | missing! | -4.97 | -2.14~ | 1.8 | unknown |
| `nemotron-3.5-lightning-free` | -0.97 | -4.76~ | missing! | -6.53~ | -2.37~ | 8.035 | no |
| `nemotron-3.5-super-vl-free` | -0.62 | -3.37~ | missing! | -4.72 | -2.14~ | 11.5 | yes |
| `qwen3-coder-next` | -0.17 | -1.20~ | -3.19~ | -1.40~ | -0.61~ | 3.496 | no |
| `qwen3-flash-2025-07-28` | - | -3.32u | -2.79u | -2.89u | -2.85u | - | unknown |
| `qwen3-max` | -0.66 | -3.28 | -3.03~ | -5.80 | -1.99~ | - | unknown |
| `qwen3.6-plus` | -0.51 | -2.92 | -3.03~ | -3.10 | -1.68~ | - | yes |
| `qwen3.7-max` | -0.51 | -2.92~ | -3.03~ | -2.27 | -1.68~ | 11.053 | no |
| `qwen3.8-max` | -0.17 | -1.20 | -1.62 | -1.40 | -1.68 | 4.913 | yes |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
