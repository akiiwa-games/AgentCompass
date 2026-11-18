# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (creativity, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Aesthetics evaluates design at level 1 and the weaker of design and image recognition at levels 2 and 3 (the column shows design); tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.58 | -3.30 | -3.31 | -3.51 | missing! | - | unknown |
| `claude-fable-5-1` | -0.03 | -0.82 | -0.52 | -0.72 | 0.03 | 39.05 | yes |
| `claude-fable-5-2` | 0.20 | 0.20u | 0.05u | 0.26u | -0.06u | 28.383 | yes |
| `claude-haiku-4-5` | -0.75 | -3.32 | -1.91~ | -4.70 | -2.44~ | 30.0 | yes |
| `claude-haiku-5-5` | -0.69 | -3.97 | -2.04~ | -2.11 | -2.42~ | 3.0 | yes |
| `claude-opus-4-6` | -0.03 | -0.47 | -1.45~ | -2.19 | 0.02~ | 45.617 | yes |
| `claude-opus-4-7` | -0.05 | -0.71 | -1.45~ | -2.22 | -0.02~ | 45.617 | yes |
| `claude-opus-4-8` | -0.20 | -1.20 | -2.38~ | -2.56 | -0.47~ | 45.617 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | -0.00 | 0.00 | 28.0 | yes |
| `claude-sonnet-4-6` | -0.28 | -1.74 | -3.36~ | -2.56 | -0.75~ | 39.589 | yes |
| `claude-sonnet-5-5` | -0.07 | -1.98 | -0.90 | -0.63 | -1.32 | 20.25 | yes |
| `deepseek-v4-flash-promo` | -0.48 | -2.19~ | -1.98~ | -1.34~ | -1.60~ | - | unknown |
| `deepseek-v4-pro` | -0.35 | -1.87 | -2.64u | -2.59 | -1.87~ | 4.847 | no |
| `deepseek-v4.1-flash-free` | -0.48 | -2.19 | -1.98 | -1.34 | -1.60 | 0.955 | yes |
| `deepseek-v4.1-flash-promo` | -0.48 | -2.19~ | -1.98~ | -1.34~ | -1.60~ | - | unknown |
| `dot` | 0.06 | -1.96~ | -0.05u | -0.29u | -1.92u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.17u | -2.68u | -2.62 | -2.81u | - | unknown |
| `flint-40b-free` | - | -3.17u | -2.68u | -2.96u | -2.81u | - | unknown |
| `gemini-3.1-flash-lite` | -0.59 | -2.61 | -3.80~ | -5.45 | -2.59 | 1.822 | yes |
| `gemini-3.1-pro-preview` | -0.16 | -0.90 | -2.95~ | -2.69 | -0.86 | 12.429 | yes |
| `gemini-3.6-flash` | -0.19 | -1.17 | -2.63 | -1.94 | -0.72 | 5.455 | yes |
| `gemini-3.7-flash` | -0.14 | -0.77 | -2.44 | -1.47 | -0.18~ | 5.455 | yes |
| `gemini-3.8-flash-high` | -0.08 | -0.73 | -2.16 | -1.50 | -0.13 | 5.294 | yes |
| `gemini-4-argon-high` | 0.12 | 0.14 | -0.76 | -0.60 | -1.16u | 6.0 | yes |
| `gemini-4-carbon` | 0.16 | -0.03 | 0.00u | -0.05 | -1.76u | 8.463 | yes |
| `gemini-4-hydrogen-high` | -0.13 | -0.65 | -1.23~ | -0.25 | -0.28~ | 17.094 | yes |
| `glm-5.2` | - | -1.96~ | -2.87~ | -2.20 | -2.20~ | 4.527 | no |
| `glm-5.3` | - | -1.75 | -1.84 | -1.69 | -1.72~ | 4.273 | no |
| `glm-5.3-flash-free` | -0.25 | -2.10 | -2.02~ | -1.70 | -1.93 | 1.139 | yes |
| `glm-5.3-flashx` | -0.25 | -2.10~ | -2.02~ | -1.70~ | -1.93~ | 2.837 | yes |
| `gpt-5.6-luna` | -0.63 | -2.76 | -2.95~ | -3.08 | -2.11 | 1.598 | yes |
| `gpt-5.6-sol` | -0.01 | -1.37 | -1.76 | -0.80 | -1.20 | 24.5 | yes |
| `gpt-5.6-terra` | -0.07 | -2.62 | -1.74~ | -2.73 | -1.89 | 12.828 | yes |
| `gpt-6-astra` | 0.06 | -1.96 | -0.05u | -0.29u | -0.92 | 26.304 | yes |
| `gpt-6-luna` | -0.63 | -3.17 | -2.35u | -2.31 | -2.81u | 0.774 | yes |
| `gpt-6-pro` | 0.06 | 0.00u | 0.02u | -0.01u | -2.90u | - | yes |
| `gpt-6-sol` | -0.01 | -2.45u | -0.72u | -1.39 | -1.19u | 20.036 | yes |
| `gpt-6.1-sol` | -0.01 | -1.96 | -0.25u | -0.36 | -0.69 | 7.546 | yes |
| `gpt-6.1-terra` | -0.07 | -2.62~ | -0.42u | -1.33 | -0.85 | 5.412 | yes |
| `gpt-6.2-astra` | 0.16 | -1.55~ | 0.72~ | 0.50~ | -0.10 | 16.145 | yes |
| `gpt-6.2-sol` | 0.06 | -1.70~ | 0.24~ | -0.11~ | -0.49~ | 7.546 | yes |
| `gpt-oss-120b` | -1.25 | -6.08 | -4.87~ | -5.88 | -2.44~ | 13.812 | no |
| `grok-4.5` | -0.33 | -1.67 | -2.45~ | -2.20 | -1.27 | 7.508 | yes |
| `grok-4.6` | -0.40 | -1.91 | -2.25~ | -1.85 | -1.19 | 10.5 | yes |
| `grok-4.7` | -0.12 | -2.49 | -1.48 | -1.90 | -1.82~ | 10.449 | yes |
| `grok-4.7-build-fast` | -0.33 | -1.67~ | -2.45~ | -2.20~ | -1.27~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | -0.00~ | -1.19u | 28.0 | yes |
| `hy3` | -0.42 | -2.39 | -2.94~ | -2.61 | -1.59~ | 1.2 | no |
| `hy4` | -0.13 | -1.16~ | -0.89~ | -2.46 | -2.35~ | 2.26 | yes |
| `hy4-flash` | - | -1.56~ | -1.29~ | -2.86 | -2.35~ | 0.189 | yes |
| `hy4-preview` | -0.13 | -1.16~ | -1.75 | -2.11 | -2.35~ | 1.872 | no |
| `kimi-k2.6` | -0.37 | -2.16 | -2.95~ | -2.53 | missing | 2.38 | yes |
| `kimi-k2.7-code` | -0.37 | -2.16~ | -2.95~ | -2.74 | -0.76~ | 2.651 | yes |
| `kimi-k2.8-preview` | -0.37 | -2.16~ | -2.95~ | -2.53~ | -0.76~ | - | unknown |
| `kimi-k3` | -0.04 | -1.74~ | -1.83~ | -0.49 | -2.19 | 5.813 | yes |
| `kimi-k3.1-pro` | -0.04 | -1.90~ | -1.36~ | -0.25 | -2.75 | 6.0 | yes |
| `mimo-v2.6-flash-free` | -0.47 | -3.20 | -2.32~ | -1.34 | -2.75 | 0.281 | yes |
| `mimo-v2.6-pro` | -0.22 | -1.76 | -1.51 | -1.29 | -1.15 | 0.667 | yes |
| `minimax-m3` | -0.57 | -3.08 | -2.68 | -2.96 | -2.01 | 7.448 | yes |
| `minimax-m3.1-flash` | -0.48 | -2.69~ | -2.97~ | -1.61 | -2.30 | 1.556 | yes |
| `mistral-large-4` | -0.69 | -4.04 | -2.41~ | -2.12 | -1.68 | 3.0 | yes |
| `muse-ember` | -0.08 | -1.30~ | -2.00~ | -0.29 | -0.85~ | 2.769 | yes |
| `muse-spark-1.3` | -0.08 | -2.28~ | -2.08~ | -0.84~ | -1.04~ | 7.297 | yes |
| `muse-spark-open` | -0.08 | -1.30~ | -3.41~ | -1.79~ | -1.92~ | 50.005 | yes |
| `muse-wonder-1.0` | -0.08 | -1.30 | -1.54 | -0.66 | -0.85 | 19.412 | yes |
| `nemotron-3-ultra-free` | -0.63 | -3.33~ | -4.11~ | -4.08 | -2.25~ | 2.545 | unknown |
| `nemotron-3.5-lightning-free` | -0.98 | -4.74~ | -4.29~ | -6.25~ | -2.44~ | 6.647 | no |
| `nemotron-3.5-super-vl-free` | -0.63 | -3.33~ | -3.47~ | -4.91 | -2.25~ | 10.364 | yes |
| `qwen3-coder-next` | -0.18 | -1.16~ | -3.00~ | -1.08~ | -0.62~ | 4.013 | no |
| `qwen3-flash-2025-07-28` | - | -3.17u | -2.68u | -2.96u | -2.81u | - | unknown |
| `qwen3.6-plus` | -0.52 | -2.82 | -2.98~ | -3.11 | -2.66 | 1.405 | yes |
| `qwen3.7-max` | -0.52 | -2.82~ | -2.98~ | -2.38 | -2.66~ | 9.529 | no |
| `qwen3.8-27b` | -0.59 | -4.03 | -2.55 | -1.86 | -1.43 | 3.478 | yes |
| `qwen3.8-flash` | - | -2.87~ | -2.19~ | -1.58~ | -2.07~ | 0.367 | yes |
| `qwen3.8-max` | -0.18 | -1.16 | -1.58 | -1.36 | -2.07 | 5.299 | yes |
| `qwen3.8-omni-flash` | - | -2.87~ | -2.19~ | -1.58~ | -2.07~ | 0.367 | yes |
| `qwen4-max` | -0.06 | -0.67~ | -1.55~ | -0.31~ | -2.69~ | 5.478 | yes |
| `step-5-preview` | -0.47 | -2.67 | -2.25 | -2.06 | -2.19 | 3.894 | yes |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
