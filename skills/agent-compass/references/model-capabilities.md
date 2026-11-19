# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (creativity, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Aesthetics evaluates design at level 1 and the weaker of design and image recognition at levels 2 and 3 (the column shows design); tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.59 | -3.61 | -3.35 | -3.85 | missing! | - | unknown |
| `claude-fable-5` | -0.02 | -0.44 | -1.30~ | -1.25 | 0.05~ | 48.409 | yes |
| `claude-fable-5-1` | -0.02 | -0.89 | -0.53 | -0.67 | -0.21 | 39.05 | yes |
| `claude-fable-5-2` | 0.20 | 0.21u | 0.05u | 0.37u | -0.08u | 28.383 | yes |
| `claude-haiku-4-5` | -0.76 | -3.64 | -3.51~ | -4.57 | -3.38~ | 30.0 | yes |
| `claude-haiku-5-5` | -0.70 | -4.35 | -2.09~ | -1.96 | -3.12~ | 3.0 | yes |
| `claude-opus-4-5` | -0.27 | -1.35 | -1.76~ | -2.74 | -3.31 | 25.245 | yes |
| `claude-opus-4-6` | -0.03 | -0.52 | -1.50~ | -2.13 | -0.22~ | 25.245 | yes |
| `claude-opus-4-7` | -0.05 | -0.77 | -1.50~ | -2.58 | -0.26~ | 25.245 | yes |
| `claude-opus-4-8` | -0.20 | -1.32 | -2.19~ | -2.40 | -0.77~ | 25.245 | yes |
| `claude-opus-5` | -0.14 | -1.29 | -1.31~ | -1.01 | -0.56 | 25.245 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 28.0 | yes |
| `claude-sonnet-4-6` | -0.28 | -1.91 | -3.14~ | -2.49 | -1.08~ | 23.559 | yes |
| `claude-sonnet-5` | -0.38 | -2.33 | -2.14 | -2.23 | -2.00~ | 15.706 | yes |
| `claude-sonnet-5-5` | -0.04 | -2.14 | -0.89 | -0.59 | -1.69 | 20.25 | yes |
| `codestral-2508` | -0.70 | -4.85~ | -4.66~ | -4.67 | -4.66~ | 1.286 | no |
| `deepseek-v4-flash` | -0.58 | -3.05 | -2.75~ | -2.64 | -2.87~ | 1.544 | no |
| `deepseek-v4-flash-promo` | -0.58 | -3.05~ | -2.75~ | -2.64~ | -2.87~ | - | unknown |
| `deepseek-v4-pro` | -0.40 | -1.98 | -2.11u | -2.98 | -1.81~ | - | no |
| `deepseek-v4-pro-0813` | -0.64 | -3.05~ | -2.75~ | -2.64~ | -2.87~ | 2.882 | no |
| `deepseek-v4.1-flash` | -0.48 | -2.40~ | -1.93~ | -1.20 | -2.16~ | 0.955 | yes |
| `deepseek-v4.1-flash-1017` | -0.48 | -2.64~ | -1.96~ | -1.27 | -1.72 | 0.955 | yes |
| `deepseek-v4.1-flash-economy` | -0.48 | -2.64~ | -1.96~ | -1.20~ | -1.72~ | - | unknown |
| `deepseek-v4.1-flash-free` | -0.48 | -2.40~ | -1.93~ | -1.20 | -2.16~ | 0.955 | yes |
| `deepseek-v4.1-flash-promo` | -0.48 | -2.64~ | -1.96~ | -1.20~ | -1.72~ | - | unknown |
| `dot` | 0.06 | -2.13~ | 0.01u | -0.19u | -1.86u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.45u | -2.62u | missing! | -2.85u | - | unknown |
| `doubao-seed-2.0-mini` | - | missing! | missing! | missing! | missing! | - | unknown |
| `doubao-seed-2.1-lite` | - | missing! | missing! | missing! | missing! | - | unknown |
| `doubao-seed-2.1-pro` | - | missing! | missing! | missing! | missing! | - | unknown |
| `doubao-seed-evolving` | - | missing! | missing! | missing! | missing! | - | unknown |
| `flint-40b-free` | - | -3.45u | -2.62u | -2.78u | -2.85u | - | unknown |
| `gemini-2.5-flash-lite` | -0.28 | -1.70~ | -2.92~ | -3.41~ | -7.75 | 0.704 | yes |
| `gemini-3-flash-preview` | -0.28 | -1.70 | -2.92~ | -3.41 | -1.34 | 3.755 | yes |
| `gemini-3.1-flash-lite` | -0.74 | -3.66~ | -3.71~ | -5.11~ | -2.89 | 1.877 | yes |
| `gemini-3.1-pro-preview` | -0.16 | -0.99 | -2.92~ | -2.50 | -0.84 | 12.429 | yes |
| `gemini-3.5-flash` | -0.23 | -1.45 | -2.55~ | -2.17 | -0.43 | 11.264 | yes |
| `gemini-3.5-flash-lite` | -0.42 | -2.27 | -3.35~ | -3.89 | -1.69 | 2.418 | yes |
| `gemini-3.6-flash` | -0.20 | -1.28 | -2.52 | -2.02 | -0.72 | 5.455 | yes |
| `gemini-3.7-flash` | -0.14 | -0.84 | -2.43 | -1.40 | -0.19~ | 5.455 | yes |
| `gemini-3.8-flash-high` | -0.08 | -0.80 | -2.12 | -1.42 | -0.12 | 5.294 | yes |
| `gemini-4-argon-high` | 0.12 | 0.15 | -0.76 | -0.58 | -1.12u | 6.0 | yes |
| `gemini-4-carbon` | 0.16 | -0.03 | 0.00u | -0.03 | -1.70u | 8.463 | yes |
| `gemini-4-hydrogen-high` | -0.13 | -0.71 | -1.27~ | -0.45 | -0.35~ | 17.094 | yes |
| `gemini-4-neon` | 0.15 | 0.11~ | -0.65~ | -0.26~ | -0.90~ | 15.41 | yes |
| `gemma-4-26b-a4b-it` | -0.57 | -3.28 | -4.09~ | -4.67 | -3.17 | 1.215 | yes |
| `gemma-4-31b-it` | -0.45 | -2.76 | -3.76~ | -4.55 | -2.33 | 2.035 | yes |
| `glm-4.5` | -0.78 | -4.06 | -3.17~ | -3.33 | -5.09~ | 14.547 | no |
| `glm-4.5-air` | -1.09 | -5.30 | -3.83~ | -3.76 | -6.45~ | - | no |
| `glm-4.5-flash` | -1.16 | -5.81~ | -5.63~ | -5.45~ | -7.35~ | - | unknown |
| `glm-4.5v` | -1.26 | -5.81 | -3.83~ | -4.78~ | -7.20 | 1.917 | yes |
| `glm-4.6` | -0.67 | -3.24 | -2.87~ | -3.92 | -5.69~ | 1.904 | no |
| `glm-4.6v` | -1.05 | -4.69 | -3.83~ | -5.35~ | -7.35 | 2.336 | yes |
| `glm-4.6v-flash` | -1.16 | -5.93~ | -5.64~ | -5.35~ | -7.35~ | - | unknown |
| `glm-4.7` | -0.53 | -3.12 | -2.49~ | -3.21 | -4.13~ | 3.785 | no |
| `glm-4.7-flash` | -1.16 | -5.93 | -4.62~ | -4.03 | -4.75~ | 0.46 | no |
| `glm-4.7-flashx` | -1.16 | -5.93~ | -4.62~ | -4.03~ | -4.75~ | - | unknown |
| `glm-5` | -0.40 | -1.96 | -2.95~ | -2.95 | -2.60~ | 3.399 | no |
| `glm-5.1` | -0.34 | -1.98 | -2.78~ | -2.38 | -2.32~ | 4.286 | no |
| `glm-5.2` | -0.34 | -2.13~ | -2.76~ | -1.93 | -1.55~ | 4.527 | no |
| `glm-5.3` | -0.34 | -1.89 | -1.86 | -1.43 | -0.77~ | 4.273 | no |
| `glm-5.3-flash` | -0.26 | -2.30 | -1.99~ | -1.58 | -1.87 | 1.139 | yes |
| `glm-5.3-flash-free` | -0.26 | -2.30 | -1.99~ | -1.58 | -1.87 | 1.139 | yes |
| `glm-5.3-flashx` | -0.26 | -2.30~ | -1.99~ | -1.58~ | -1.87~ | 2.837 | yes |
| `glm-5v-turbo` | -0.40 | -1.96~ | -2.95~ | -2.91 | -2.60~ | 30.538 | yes |
| `gpt-5.2` | -0.57 | -3.66 | -3.12~ | -4.18 | -2.06 | 9.046 | yes |
| `gpt-5.2-pro` | -0.57 | -3.66~ | -3.12~ | -4.18~ | missing! | - | yes |
| `gpt-5.4` | -0.26 | -2.14 | -2.56~ | -4.23 | -0.61 | 11.382 | yes |
| `gpt-5.4-mini` | -0.49 | -3.24 | -3.09~ | -4.26 | -2.09 | 8.602 | yes |
| `gpt-5.4-pro` | -0.26 | -2.14~ | -2.56~ | -4.23~ | -0.61~ | - | yes |
| `gpt-5.5` | -0.21 | -1.86 | -2.42~ | -2.17 | -1.05 | 24.62 | yes |
| `gpt-5.5-pro` | -0.21 | -1.86~ | -2.42~ | -2.17~ | -1.05~ | - | yes |
| `gpt-5.6-luna` | -0.64 | -3.03 | -2.75~ | -2.72 | -2.08 | 1.598 | yes |
| `gpt-5.6-sol` | -0.21 | -1.49 | -1.74~ | -0.72 | -1.21 | 24.5 | yes |
| `gpt-5.6-terra` | -0.21 | -2.84 | -2.80~ | -2.41 | -1.88 | 12.828 | yes |
| `gpt-6-astra` | 0.06 | -2.13 | 0.01u | -0.19u | -0.89 | 26.304 | yes |
| `gpt-6-luna` | -0.64 | -3.45 | -2.27u | -2.17 | -2.85u | 0.774 | yes |
| `gpt-6-pro` | 0.06 | 0.00u | 0.52f | 0.93f | -4.44u | - | yes |
| `gpt-6-sol` | -0.29 | -2.66u | -0.53u | -1.30 | -1.16u | 20.036 | yes |
| `gpt-6.1-sol` | -0.01 | -2.13 | -0.21u | -0.34 | -0.74 | 7.546 | yes |
| `gpt-6.1-terra` | -0.04 | -2.27~ | -0.47u | -1.25 | -0.57 | 5.412 | yes |
| `gpt-6.2-astra` | 0.16 | -1.42~ | 0.81~ | -0.10~ | -0.10 | 16.145 | yes |
| `gpt-6.2-sol` | 0.06 | -1.85~ | 0.33~ | -0.09~ | 0.07~ | 7.546 | yes |
| `gpt-oss-120b` | -1.27 | -6.67 | -5.73~ | -5.86 | -5.60~ | 8.211 | no |
| `gpt-oss-20b` | -1.54 | -7.72 | -5.73~ | -5.86~ | -5.60~ | - | no |
| `grok-4.20-0309-non-reasoning` | -0.53 | -3.67~ | -2.88~ | -4.94~ | -6.07~ | - | unknown |
| `grok-4.20-0309-reasoning` | -0.53 | -2.66~ | -2.88~ | -4.11~ | -2.34~ | - | unknown |
| `grok-4.20-multi-agent-0309` | -0.53 | -2.66~ | -2.88~ | -4.11~ | -2.34~ | - | unknown |
| `grok-4.3` | -0.53 | -2.66 | -2.88~ | -4.11 | -2.34 | 4.749 | yes |
| `grok-4.5` | -0.33 | -1.83 | -2.34~ | -2.08 | -1.23 | 7.508 | yes |
| `grok-4.6` | -0.41 | -2.09 | -2.15~ | -1.74 | -1.57 | 10.5 | yes |
| `grok-4.7` | -0.07 | -2.72 | -1.44 | -1.76 | -1.88~ | 10.449 | yes |
| `grok-4.7-build-fast` | -0.07 | -2.72~ | -1.44~ | -1.76~ | -1.88~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | 0.00~ | -1.16u | 28.0 | yes |
| `grok-build-0.1` | -0.37 | -2.41~ | -2.04~ | -2.45~ | -1.68~ | 4.387 | yes |
| `hy3` | -0.43 | -2.62 | -2.91~ | -2.54 | -1.94~ | 1.2 | no |
| `hy4` | -0.07 | -1.05~ | -0.87~ | -2.28 | -2.28~ | 2.26 | yes |
| `hy4-flash` | - | -1.40~ | -1.22~ | -2.63 | -2.28~ | 0.189 | yes |
| `hy4-preview` | -0.07 | -1.05~ | -1.85 | -2.12 | -2.28~ | 1.872 | no |
| `kimi-for-coding-highspeed` | -0.20 | -2.37~ | -2.83~ | -2.58~ | -1.45~ | - | unknown |
| `kimi-k2.6` | -0.37 | -2.37 | -2.85~ | -2.38 | -1.95 | 2.38 | yes |
| `kimi-k2.7-code` | -0.37 | -2.37~ | -2.83~ | -2.58 | -1.45~ | 2.651 | yes |
| `kimi-k2.8-preview` | -0.20 | -1.66~ | -2.34~ | -1.47~ | -0.92~ | - | unknown |
| `kimi-k3` | -0.02 | -1.89~ | -1.67~ | -0.36 | -1.78 | 5.813 | yes |
| `kimi-k3.1-pro` | -0.02 | -2.06~ | -1.41~ | -0.11 | -2.66 | 6.0 | yes |
| `kimi-k3.1-pro-highspeed` | -0.02 | -2.06~ | -1.41~ | -0.11~ | -1.49~ | - | unknown |
| `mimo-v2.5` | -0.60 | -3.47 | -2.88~ | -2.72 | -2.46~ | 0.188 | yes |
| `mimo-v2.5-pro` | -0.33 | -2.36 | -2.88~ | -2.58 | -1.28~ | 0.52 | no |
| `mimo-v2.6-flash-free` | -0.48 | -3.50 | -2.38~ | -1.72 | -2.64 | 0.281 | yes |
| `mimo-v2.6-pro` | -0.22 | -1.93 | -1.50 | -1.25 | -1.53 | 0.667 | yes |
| `mimo-v2.6-pro-ultraspeed` | -0.22 | -1.93~ | -1.50~ | -1.25~ | -1.53~ | 6.67 | yes |
| `minimax-m3` | -0.58 | -3.38 | -2.62 | -2.78 | -2.00 | 7.448 | yes |
| `minimax-m3.1-flash` | -0.48 | -2.89~ | -1.89~ | -1.51 | -1.88 | 1.556 | yes |
| `ministral-14b-2512` | - | -6.49~ | -6.49~ | -6.49~ | -10.11 | 0.798 | yes |
| `ministral-3b-2512` | - | -8.14~ | -8.14~ | -8.14~ | -13.40 | 0.399 | yes |
| `ministral-8b-2512` | - | -7.04~ | -7.04~ | -7.04~ | -11.19 | 0.598 | yes |
| `mistral-large-4` | -0.70 | -4.42 | -2.42~ | -2.48 | -2.10 | 3.0 | yes |
| `mistral-small-2603` | -0.70 | -3.84~ | -3.43~ | -3.72~ | -3.15~ | 0.665 | yes |
| `muse-ember` | -0.04 | -1.00~ | -1.85~ | -0.39 | -0.24~ | 2.769 | yes |
| `muse-spark-1.3` | -0.04 | -2.34~ | -1.95~ | -0.86~ | -1.00~ | 7.297 | yes |
| `muse-spark-open` | -0.04 | -1.00~ | -3.24~ | -1.76~ | -1.54~ | 8.232 | yes |
| `muse-wonder-1.0` | -0.04 | -1.39 | -1.35 | -0.72 | -0.57 | 19.412 | yes |
| `nemotron-3-ultra-free` | -0.64 | -3.57~ | -3.83~ | -3.74 | -2.87~ | 2.545 | unknown |
| `nemotron-3.5-lightning-free` | -0.99 | -5.08~ | -4.74~ | -5.75~ | -4.41~ | 3.952 | no |
| `nemotron-3.5-super-vl-free` | -0.64 | -3.57~ | -3.47~ | -4.42 | -2.87~ | 6.161 | yes |
| `qwen-max` | -0.30 | -3.12~ | -4.00~ | -3.02~ | -2.88~ | - | unknown |
| `qwen-plus` | -0.30 | -2.11~ | -1.84~ | -2.19~ | -1.38~ | 1.715 | no |
| `qwen3-coder-flash` | -0.90 | -6.03~ | -5.62~ | -6.71~ | -4.61~ | - | no |
| `qwen3-coder-next` | - | missing! | missing! | missing! | missing! | 6.334 | no |
| `qwen3-flash-2025-07-28` | -0.90 | -3.45u | -2.62u | -2.78u | -2.85u | - | unknown |
| `qwen3-omni-flash` | -0.90 | -5.01~ | -4.32~ | -5.88~ | -3.47~ | - | unknown |
| `qwen3-vl-plus` | -0.52 | -3.06~ | -2.45~ | -3.03~ | -2.34~ | - | unknown |
| `qwen3.5-122b-a10b` | -0.74 | -4.24 | -2.88~ | -4.65 | -2.57 | - | yes |
| `qwen3.5-27b` | -0.80 | -4.46 | -3.34~ | -4.68 | -2.62 | - | yes |
| `qwen3.5-35b-a3b` | -0.92 | -4.89 | -3.67~ | -5.78 | -3.70 | - | yes |
| `qwen3.5-397b-a17b` | -0.53 | -3.10 | -2.88~ | -3.45 | -2.01 | - | yes |
| `qwen3.5-flash` | -0.90 | -5.01 | -3.62~ | -5.88 | -3.47~ | - | unknown |
| `qwen3.5-plus` | -0.52 | -3.09~ | -2.45~ | -2.91~ | -2.01~ | - | unknown |
| `qwen3.6-27b` | -0.77 | -4.28~ | -2.88~ | -4.67~ | -3.15 | - | yes |
| `qwen3.6-35b-a3b` | -0.77 | -4.28~ | -2.88~ | -4.67~ | -3.03 | - | yes |
| `qwen3.6-flash` | -0.90 | -5.01~ | -3.25~ | -5.88~ | -3.68~ | - | yes |
| `qwen3.6-plus` | -0.52 | -3.09 | -2.88~ | -2.91 | -2.21 | 1.695 | yes |
| `qwen3.7-flash` | -0.60 | -4.41~ | -3.12~ | -2.23~ | -2.27~ | 0.208 | yes |
| `qwen3.7-max` | -0.42 | -2.40~ | -2.95~ | -1.96 | -1.37~ | 9.529 | no |
| `qwen3.7-plus` | -0.42 | -2.40 | -3.12~ | -2.13 | -1.37 | 2.204 | yes |
| `qwen3.8-27b` | -0.60 | -4.41 | -2.62 | -2.23 | -1.43 | 3.478 | yes |
| `qwen3.8-flash` | -0.60 | -4.41~ | -2.62~ | -2.23~ | -2.17~ | 0.367 | yes |
| `qwen3.8-max` | -0.19 | -1.27 | -2.04 | -1.45 | -2.17 | 5.299 | yes |
| `qwen3.8-omni-flash` | -0.60 | -4.41~ | -2.62~ | -2.23~ | -2.17~ | 0.367 | yes |
| `qwen4-max` | -0.04 | -0.63~ | -1.60~ | -0.71~ | -2.60~ | 5.478 | yes |
| `step-3.5-flash` | -0.93 | -4.83 | -3.70~ | -3.81~ | -2.95~ | 0.589 | unknown |
| `step-3.5-flash-2603` | -0.93 | -4.83~ | -3.70~ | -3.81~ | -2.95~ | 0.589 | unknown |
| `step-3.7-flash` | -0.93 | -4.83~ | -3.70~ | -3.81 | -2.95 | 1.254 | yes |
| `step-5-preview` | -0.48 | -2.92 | -2.23 | -2.42 | -2.13 | 3.894 | yes |
| `voxtral-small-2507` | - | missing! | missing! | missing! | missing! | - | unknown |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `f` marks a maintainer score that its operating point bounds: the row is declared as another row at an operating point and the cell of that row there, above the score, is the value (`operating_point_floor` in the overlay); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
