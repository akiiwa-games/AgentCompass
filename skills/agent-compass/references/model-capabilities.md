# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (divergence, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Divergence | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | 3.27 | -3.39 | -3.00 | -3.49 | -1.66~ | - | unknown |
| `claude-fable-5-1` | 4.30 | -0.86 | -0.52 | -0.55 | -0.10 | 36.11 | yes |
| `claude-fable-5-2` | 4.30 | -0.86~ | -0.52~ | 0.21 | -0.10~ | 28.888 | unknown |
| `claude-haiku-4-5` | 2.88 | -3.41 | -1.96~ | -5.32 | -2.05~ | 26.67 | yes |
| `claude-haiku-5-5` | 3.04 | -4.05 | -1.74~ | -1.91 | -1.95~ | 2.667 | yes |
| `claude-opus-4-6` | 4.29 | -0.48 | -1.20~ | -1.92 | -0.10~ | 37.727 | yes |
| `claude-opus-4-7` | 4.07 | -0.73 | -1.27~ | -2.13 | -0.14~ | 37.727 | yes |
| `claude-opus-4-8` | 3.67 | -1.23 | -1.39~ | -2.39 | -0.62~ | 37.727 | yes |
| `claude-opus-5-5` | 4.60 | 0.00 | 0.00 | 0.00 | 0.00 | 22.859 | yes |
| `claude-sonnet-4-6` | 3.67 | -1.79 | -1.42~ | -2.22 | -0.93~ | 25.4 | yes |
| `claude-sonnet-5-5` | 3.90 | -2.00 | -1.13 | -0.46 | -1.51 | 12.825 | yes |
| `deepseek-v4-pro` | 3.60 | -1.91 | missing~ | -2.67 | missing~ | 8.735 | no |
| `deepseek-v4.1-flash-free` | 3.67 | -2.23 | -2.06 | -1.20 | -1.64 | 1.084 | yes |
| `dot` | 4.20 | -1.86~ | -0.05~ | 0.38~ | missing! | - | unknown |
| `doubao-seed-2-1-pro-260628` | - | missing! | missing! | -2.56 | missing! | - | unknown |
| `flint-40b-free` | - | missing! | missing! | missing! | missing! | - | unknown |
| `gemini-3.1-flash-lite` | 3.24 | -2.69 | -3.11~ | -5.56 | -1.99~ | 2.009 | yes |
| `gemini-3.1-pro-preview` | 3.67 | -0.92 | -2.99~ | -2.55 | -0.88 | 12.222 | yes |
| `gemini-3.7-flash` | 3.67 | -0.78 | -2.54 | -1.32 | -0.15~ | 5.889 | yes |
| `gemini-3.8-flash-high` | 3.00 | -0.73 | -2.21 | -1.36 | -0.12 | 4.953 | yes |
| `gemini-4-argon-high` | 4.91 | 0.23 | -0.81 | -0.56 | -0.20~ | 6.658 | yes |
| `gemini-4-carbon` | 4.99 | 0.04 | -0.58~ | 0.27 | -1.06 | 8.66 | yes |
| `glm-5.2` | 3.67 | missing | missing~ | -1.87 | -1.14~ | 4.55 | no |
| `glm-5.3` | 3.67 | -1.35 | -1.30 | -1.35 | -1.14~ | 4.378 | no |
| `glm-5.3-flash-free` | 3.67 | -2.14 | -2.08~ | -1.56 | -1.42 | 1.5 | yes |
| `gpt-5.6-luna` | 3.50 | missing | -2.18~ | -2.89 | missing~ | 1.811 | yes |
| `gpt-5.6-sol` | 3.67 | missing | missing | missing | missing~ | 25.95 | yes |
| `gpt-5.6-terra` | 3.64 | missing | missing~ | -2.56 | missing~ | 13.929 | yes |
| `gpt-6-astra` | 4.20 | -1.86 | -0.05 | 0.38 | -1.04 | 28.853 | yes |
| `gpt-6-luna` | 3.38 | -3.43 | -2.35 | -2.21 | -2.98 | 0.885 | yes |
| `gpt-6-pro` | 3.67 | -2.85~ | -0.74~ | -2.47~ | -1.20~ | - | unknown |
| `gpt-6-sol` | 3.54 | missing | missing | -1.21 | missing~ | 17.226 | yes |
| `gpt-6.1-sol` | 3.60 | -2.04 | -0.40 | -0.32 | -0.75 | 6.125 | yes |
| `gpt-6.1-terra` | 4.16 | -0.98~ | -0.64~ | -1.38 | -0.74 | 5.393 | yes |
| `gpt-oss-120b` | 1.71 | -6.26 | -4.00~ | -9.40 | -2.05~ | 12.827 | no |
| `grok-4.5` | 3.62 | -1.72 | -1.25~ | -2.07 | -0.79~ | 9.228 | yes |
| `grok-4.6` | 3.53 | -1.95 | missing | -1.67 | -1.39~ | 12.971 | yes |
| `grok-4.7` | 3.40 | -2.55 | -1.54 | -1.80 | -2.17 | 10.778 | yes |
| `grok-4.7-build-fast` | 3.62 | -1.72~ | -1.25~ | -2.07~ | -0.79~ | - | unknown |
| `grok-bot` | 4.60 | 0.00~ | 0.00~ | 0.00~ | missing! | - | unknown |
| `kimi-k2.6` | 3.57 | -2.22 | -3.13~ | -2.36 | -0.93~ | 4.6 | yes |
| `kimi-k2.7-code` | 3.57 | -2.22~ | -3.13~ | -2.61 | -0.93~ | 6.012 | yes |
| `kimi-k3` | 3.67 | missing | missing | -0.42 | -2.07~ | 5.742 | yes |
| `kimi-k3.1-pro` | 4.16 | -1.12~ | -1.16~ | -0.16 | -2.22 | 9.75 | yes |
| `mimo-v2.6-flash-free` | 3.46 | -3.28 | -1.97~ | -1.24 | -2.81 | 0.275 | yes |
| `mimo-v2.6-pro` | 3.67 | -1.81 | -1.57 | -1.01 | -1.35 | 0.63 | yes |
| `minimax-m3` | 3.29 | -3.17 | -2.79 | -2.78 | -2.08 | 5.829 | yes |
| `minimax-m3.1-flash` | 3.45 | -2.39~ | -1.99~ | -1.47 | -2.21 | 1.582 | yes |
| `mistral-large-4` | 2.97 | -4.25 | -2.51~ | -2.16 | -1.93 | 4.364 | yes |
| `muse-wonder-1.0` | 4.72 | -0.85 | -1.47 | -0.09 | -0.74 | 21.771 | yes |
| `nemotron-3-ultra-free` | 3.19 | -2.88~ | -3.97~ | -4.80 | -1.76~ | 1.8 | unknown |
| `nemotron-3.5-lightning-free` | 1.71 | -5.67~ | -3.99~ | -7.49~ | -2.05~ | 8.035 | no |
| `nemotron-3.5-super-vl-free` | 2.90 | -3.44~ | -3.61~ | -4.53 | -2.05~ | 11.5 | yes |
| `qwen3-coder-next` | 3.67 | -1.19~ | -3.18~ | -1.27~ | -0.63~ | 3.496 | no |
| `qwen3-flash-2025-07-28` | - | missing! | missing! | missing! | missing! | - | unknown |
| `qwen3-max` | 3.06 | -3.25 | -2.42~ | -5.62 | -1.48~ | - | unknown |
| `qwen3.6-plus` | 3.41 | -2.89 | -3.02~ | -2.99 | -1.60~ | - | yes |
| `qwen3.7-max` | 3.41 | -2.89~ | -3.02~ | -2.16 | -1.60~ | 11.053 | no |
| `qwen3.8-max` | 3.67 | -1.19 | -1.62 | -1.27 | -1.68 | 4.913 | yes |

Tier is the arena.ai Overall position on the user scale (or its interpolation from another overall board, or its same-series sibling tier); it is not a score and only estimates facets a model lacks. A value marked `~` holds such an estimate (a same-series sibling, else the fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
