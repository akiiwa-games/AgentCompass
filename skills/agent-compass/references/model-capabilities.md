# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (divergence, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Divergence | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | 3.27 | -3.65 | -2.40 | -3.46 | -1.69~ | - | unknown |
| `claude-fable-5-1` | 4.30 | -0.76 | -0.50 | -0.63 | 0.19 | 36.11 | yes |
| `claude-fable-5-2` | 5.25 | 1.21~ | -0.20~ | 0.08 | 0.02~ | 28.888 | unknown |
| `claude-haiku-4-5` | 2.88 | -3.67 | -1.79~ | -5.19 | -2.09~ | 26.67 | yes |
| `claude-haiku-5-5` | 3.04 | -4.35 | -1.58~ | -1.96 | -1.99~ | 2.667 | yes |
| `claude-opus-4-6` | 4.29 | -0.52 | -1.20~ | -1.89 | 0.03~ | 37.727 | yes |
| `claude-opus-4-7` | 4.07 | -0.78 | -1.27~ | -2.25 | 0.11~ | 37.727 | yes |
| `claude-opus-4-8` | 3.67 | -1.32 | -1.39~ | -2.40 | -0.54~ | 37.727 | yes |
| `claude-opus-5-5` | 4.60 | 0.00 | 0.00 | 0.00 | 0.00 | 22.859 | yes |
| `claude-sonnet-4-6` | 3.67 | -1.92 | -1.43~ | -2.22 | -0.88~ | 25.4 | yes |
| `claude-sonnet-5-5` | 3.90 | -1.45 | -1.14 | -0.42 | -0.83 | 12.825 | yes |
| `deepseek-v4-pro` | 3.60 | -1.98 | -2.86~ | -2.68 | -1.25~ | 8.735 | no |
| `deepseek-v4.1-flash-free` | 3.67 | -2.15 | -2.05 | -1.11 | -1.58 | 1.084 | yes |
| `dot` | 4.20 | -1.88~ | -0.00~ | 0.43~ | missing | - | unknown |
| `doubao-seed-2-1-pro-260628` | - | missing | missing | -2.64 | missing | - | unknown |
| `flint-40b-free` | - | missing | missing | missing | missing | - | unknown |
| `gemini-3.1-flash-lite` | 3.24 | -2.89 | -2.60~ | -5.32 | -2.05~ | 2.009 | yes |
| `gemini-3.1-pro-preview` | 3.67 | -0.99 | -2.64~ | -2.54 | -0.85 | 12.222 | yes |
| `gemini-3.7-flash` | 3.67 | -0.84 | -2.04 | -1.37 | -0.04~ | 5.889 | yes |
| `gemini-3.8-flash-high` | 3.00 | -0.79 | -2.00 | -1.41 | -0.13 | 4.953 | yes |
| `gemini-4-argon-high` | 4.91 | 0.25 | -0.68 | -0.66 | -0.20~ | 6.658 | yes |
| `gemini-4-carbon` | 4.99 | 0.04 | -0.58~ | -0.10 | -1.06 | 8.66 | yes |
| `glm-5.2` | 3.67 | -1.87 | -1.30~ | -1.83 | -1.15~ | 4.55 | no |
| `glm-5.3` | 3.67 | -1.70 | -1.30 | -1.48 | -1.15~ | 4.378 | no |
| `glm-5.3-flash-free` | 3.67 | -2.30 | -1.86~ | -1.61 | -1.42 | 1.5 | yes |
| `gpt-5.6-luna` | 3.50 | -3.01 | -2.19~ | -2.84 | -2.22~ | 1.811 | yes |
| `gpt-5.6-sol` | 3.67 | -1.29 | -1.30 | -0.59 | -0.94~ | 25.95 | yes |
| `gpt-5.6-terra` | 3.64 | -2.44 | -1.93~ | -2.52 | -1.31~ | 13.929 | yes |
| `gpt-6-astra` | 4.20 | -1.88 | -0.00 | 0.43 | -1.04 | 28.853 | yes |
| `gpt-6-luna` | 3.38 | -3.10 | -2.35 | -2.09 | -2.97 | 0.885 | yes |
| `gpt-6-pro` | 3.67 | -1.29~ | -1.30~ | -0.59~ | -0.94~ | - | unknown |
| `gpt-6-sol` | 3.54 | -2.46 | -1.16 | -1.19 | -2.50~ | 17.226 | yes |
| `gpt-6.1-sol` | 3.60 | -1.90 | -0.29 | -0.36 | -0.79 | 6.125 | yes |
| `gpt-6.1-terra` | 4.16 | -0.75~ | -0.48~ | -1.23 | -0.80 | 5.393 | yes |
| `gpt-oss-120b` | 1.71 | -6.72 | -3.89~ | -9.17 | -2.09~ | 12.827 | no |
| `grok-4.5` | 3.62 | -1.85 | -1.45~ | -2.08 | -0.73~ | 9.228 | yes |
| `grok-4.6` | 3.53 | -2.10 | -1.55 | -1.72 | -1.39~ | 12.971 | yes |
| `grok-4.7` | 3.40 | -2.71 | -1.38 | -1.84 | -2.21 | 10.778 | yes |
| `grok-4.7-build-fast` | 3.62 | -1.85~ | -1.45~ | -2.08~ | -0.73~ | - | unknown |
| `grok-bot` | 4.60 | 0.00~ | 0.00~ | 0.00~ | missing | - | unknown |
| `kimi-k2.6` | 3.57 | -2.39 | -3.02~ | -2.37 | -0.88~ | 4.6 | yes |
| `kimi-k2.7-code` | 3.57 | -2.39~ | -3.02~ | -2.59 | -0.88~ | 6.012 | yes |
| `kimi-k3` | 3.67 | -1.62 | -1.58 | -0.66 | -2.09~ | 5.742 | yes |
| `kimi-k3.1-pro` | 4.16 | -0.62~ | -1.16~ | -0.40 | -2.22 | 9.75 | yes |
| `mimo-v2.6-flash-free` | 3.46 | -3.53 | -1.77~ | -1.45 | -2.62 | 0.275 | yes |
| `mimo-v2.6-pro` | 3.67 | -1.94 | -1.39 | -1.08 | -1.35 | 0.63 | yes |
| `minimax-m3` | 3.29 | -3.41 | -2.50 | -2.76 | -2.02 | 5.829 | yes |
| `minimax-m3.1-flash` | 3.45 | -2.51~ | -1.79~ | -1.52 | -2.08 | 1.582 | yes |
| `mistral-large-4` | 2.97 | -4.57 | -2.25~ | -2.18 | -1.98 | 4.364 | yes |
| `muse-wonder-1.0` | 4.72 | -0.66 | -1.33 | 0.02 | -0.80 | 21.771 | yes |
| `nemotron-3-ultra-free` | 3.19 | -3.04~ | -3.18~ | -4.71 | -1.79~ | 1.8 | unknown |
| `nemotron-3.5-lightning-free` | 1.71 | -6.11~ | -3.88~ | -7.12~ | -2.09~ | 8.035 | no |
| `nemotron-3.5-super-vl-free` | 2.90 | -3.66~ | -3.50~ | -4.45 | -2.09~ | 11.5 | yes |
| `qwen3-coder-next` | 3.67 | -1.28~ | -3.07~ | -1.38~ | -0.58~ | 3.496 | no |
| `qwen3-flash-2025-07-28` | - | missing | missing | missing | missing | - | unknown |
| `qwen3-max` | 3.06 | -3.49 | -2.21~ | -5.50 | -1.50~ | - | unknown |
| `qwen3.6-plus` | 3.41 | -3.11 | -2.42~ | -2.96 | -1.61~ | - | yes |
| `qwen3.7-max` | 3.41 | -3.11~ | -2.42~ | -2.18 | -1.61~ | 11.053 | no |
| `qwen3.8-max` | 3.67 | -1.28 | -1.49 | -1.38 | -1.68 | 4.913 | yes |

Tier is the arena.ai Overall position on the user scale (or its interpolation from another overall board, or its same-series sibling tier); it is not a score and only estimates facets a model lacks. A value marked `~` holds such an estimate (a same-series sibling, else the fit of that facet on the tier); `!` marks a category computed from only the facets that have values; `missing` is a category with no value at all (mode B when the task demands it). The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
