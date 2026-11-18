# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (creativity, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Aesthetics evaluates design at level 1 and the weaker of design and image recognition at levels 2 and 3 (the column shows design); tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.58 | -3.27 | -3.02 | -3.49 | missing! | - | unknown |
| `claude-fable-5-1` | -0.03 | -0.75 | -0.63 | -0.69 | 0.09 | 36.11 | yes |
| `claude-fable-5-2` | 0.20 | 0.20u | 0.05u | 0.27u | 0.00u | 28.888 | yes |
| `claude-haiku-4-5` | -0.75 | -3.30 | missing! | -4.68 | -2.52~ | 26.67 | yes |
| `claude-haiku-5-5` | -0.69 | -3.94 | missing! | -2.10 | -2.50~ | 2.667 | yes |
| `claude-opus-4-6` | -0.03 | -0.47 | -1.45~ | -2.18 | 0.02~ | 37.242 | yes |
| `claude-opus-4-7` | -0.05 | -0.70 | -1.45~ | -2.21 | -0.02~ | 37.242 | yes |
| `claude-opus-4-8` | -0.20 | -1.19 | -2.53~ | -2.55 | -0.47~ | 37.242 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 22.859 | yes |
| `claude-sonnet-4-6` | -0.28 | -1.73 | -3.50~ | -2.55 | -0.75~ | 25.073 | yes |
| `claude-sonnet-5-5` | -0.07 | -1.97 | -1.22 | -0.63 | -1.31 | 12.825 | yes |
| `deepseek-v4-pro` | -0.35 | -1.86 | -2.79u | -2.58 | -1.37~ | 8.904 | no |
| `deepseek-v4.1-flash-free` | -0.48 | -2.17 | -2.11 | -1.34 | -1.58 | 1.084 | yes |
| `dot` | 0.06 | -1.93~ | -0.25u | -0.27u | -1.90u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.14u | -2.81u | -2.61 | -2.76u | - | unknown |
| `flint-40b-free` | - | -3.14u | -2.81u | -2.95u | -2.76u | - | unknown |
| `gemini-3.1-flash-lite` | -0.59 | -2.59 | -3.95~ | -5.41 | -2.57 | 1.705 | yes |
| `gemini-3.1-pro-preview` | -0.16 | -0.90 | -2.99~ | -2.69 | -0.85 | 12.222 | yes |
| `gemini-3.7-flash` | -0.14 | -0.76 | -2.53 | -1.46 | -0.18~ | 5.889 | yes |
| `gemini-3.8-flash-high` | -0.08 | -0.73 | -2.27 | -1.48 | -0.12 | 4.953 | yes |
| `gemini-4-argon-high` | 0.12 | 0.14 | -0.93 | -0.58 | -1.10u | 6.658 | yes |
| `gemini-4-carbon` | 0.16 | -0.03 | 0.00u | -0.04 | -1.72u | 8.66 | yes |
| `gemini-4-hydrogen-high` | -0.13 | -0.64 | -1.23~ | -0.24 | -0.28~ | 7.6 | yes |
| `glm-5.2` | - | -1.93~ | missing! | -2.20 | missing! | 4.55 | no |
| `glm-5.3` | - | -1.74 | -1.97 | -1.68 | missing! | 4.378 | no |
| `glm-5.3-flash-free` | -0.25 | -2.08 | missing! | -1.69 | -1.93 | 1.5 | yes |
| `gpt-5.6-luna` | -0.63 | -2.74 | -3.09~ | -3.07 | -2.09 | 1.827 | yes |
| `gpt-5.6-sol` | -0.01 | -1.36 | -1.94 | -0.82 | -1.19 | 25.95 | yes |
| `gpt-5.6-terra` | -0.07 | -2.60 | -1.89~ | -2.73 | -1.88 | 13.929 | yes |
| `gpt-6-astra` | 0.06 | -1.93 | -0.25u | -0.27u | -0.89 | 28.853 | yes |
| `gpt-6-luna` | -0.63 | -3.14 | -2.48u | -2.30 | -2.76u | 0.885 | yes |
| `gpt-6-pro` | 0.06 | 0.00u | 0.02u | 0.00u | -2.85u | - | yes |
| `gpt-6-sol` | -0.01 | -2.43u | -0.86u | -1.38 | -1.19u | 17.226 | yes |
| `gpt-6.1-sol` | -0.01 | -1.95 | -0.50u | -0.36 | -0.69 | 6.125 | yes |
| `gpt-6.1-terra` | -0.07 | -2.60~ | -0.77u | -1.32 | -0.84 | 5.393 | yes |
| `gpt-6.2-astra` | 0.16 | -1.54~ | 0.55~ | 0.54~ | -0.13 | 17.71 | yes |
| `gpt-6.2-sol` | 0.06 | -1.69~ | 0.08~ | -0.08~ | -0.48~ | 6.125 | yes |
| `gpt-oss-120b` | -1.25 | -6.04 | missing! | -5.88 | -2.52~ | 13.805 | no |
| `grok-4.5` | -0.33 | -1.66 | -2.60~ | -2.19 | -1.26 | 9.274 | yes |
| `grok-4.6` | -0.40 | -1.89 | -2.35~ | -1.84 | -1.19 | 12.971 | yes |
| `grok-4.7` | -0.12 | -2.47 | -1.64 | -1.90 | -1.81~ | 10.778 | yes |
| `grok-4.7-build-fast` | -0.33 | -1.66~ | -2.60~ | -2.19~ | -1.26~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | 0.00~ | -1.19u | 22.859 | yes |
| `kimi-k2.6` | -0.37 | -2.15 | -3.10~ | -2.52 | missing | 2.351 | yes |
| `kimi-k2.7-code` | -0.37 | -2.15~ | -3.10~ | -2.73 | -0.75~ | 2.618 | yes |
| `kimi-k3` | -0.04 | -1.72~ | -1.99~ | -0.49 | -2.17 | 5.742 | yes |
| `kimi-k3.1-pro` | -0.04 | -1.87~ | -1.36~ | -0.23 | -2.74 | 9.75 | yes |
| `mimo-v2.6-flash-free` | -0.47 | -3.17 | missing! | -1.34 | -2.72 | 0.275 | yes |
| `mimo-v2.6-pro` | -0.22 | -1.75 | -1.66 | -1.28 | -1.14 | 0.63 | yes |
| `minimax-m3` | -0.57 | -3.06 | -2.81 | -2.95 | -2.00 | 5.829 | yes |
| `minimax-m3.1-flash` | -0.48 | -2.63~ | missing! | -1.61 | -2.29 | 1.582 | yes |
| `mistral-large-4` | -0.69 | -4.01 | missing! | -2.11 | -1.65 | 4.364 | yes |
| `muse-wonder-1.0` | -0.08 | -1.29 | -1.72 | -0.64 | -0.84 | 21.771 | yes |
| `nemotron-3-ultra-free` | -0.63 | -3.25~ | missing! | -4.06 | -2.31~ | 1.8 | unknown |
| `nemotron-3.5-lightning-free` | -0.98 | -4.61~ | missing! | -6.35~ | -2.52~ | 6.807 | no |
| `nemotron-3.5-super-vl-free` | -0.63 | -3.25~ | missing! | -4.90 | -2.31~ | 10.35 | yes |
| `qwen3-coder-next` | -0.18 | -1.15~ | -3.16~ | -0.97~ | -0.61~ | 4.654 | no |
| `qwen3-flash-2025-07-28` | - | -3.14u | -2.81u | -2.95u | -2.76u | - | unknown |
| `qwen3-max` | -0.67 | -3.15 | -3.31~ | -4.81 | -3.16~ | - | unknown |
| `qwen3.6-plus` | -0.52 | -2.80 | -3.06~ | -3.10 | -2.64 | 1.629 | yes |
| `qwen3.7-max` | -0.52 | -2.80~ | -3.06~ | -2.37 | -2.64~ | 11.053 | no |
| `qwen3.8-max` | -0.18 | -1.15 | -1.70 | -1.35 | -2.07 | 4.913 | yes |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
