# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (creativity, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Aesthetics evaluates design at level 1 and the weaker of design and image recognition at levels 2 and 3 (the column shows design); tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.58 | -3.27 | -3.29 | -3.49 | missing! | - | unknown |
| `claude-fable-5-1` | -0.03 | -0.75 | -0.52 | -0.69 | 0.09 | 39.05 | yes |
| `claude-fable-5-2` | 0.20 | 0.20u | 0.05u | 0.27u | 0.00u | 28.383 | yes |
| `claude-haiku-4-5` | -0.75 | -3.30 | missing! | -4.68 | -2.52~ | 30.0 | yes |
| `claude-haiku-5-5` | -0.69 | -3.94 | missing! | -2.10 | -2.50~ | 3.0 | yes |
| `claude-opus-4-6` | -0.03 | -0.47 | -1.45~ | -2.18 | 0.02~ | 45.617 | yes |
| `claude-opus-4-7` | -0.05 | -0.70 | -1.45~ | -2.21 | -0.02~ | 45.617 | yes |
| `claude-opus-4-8` | -0.20 | -1.19 | -2.38~ | -2.55 | -0.47~ | 45.617 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 28.0 | yes |
| `claude-sonnet-4-6` | -0.28 | -1.73 | -3.35~ | -2.55 | -0.75~ | 39.589 | yes |
| `claude-sonnet-5-5` | -0.07 | -1.97 | -0.90 | -0.63 | -1.31 | 20.25 | yes |
| `deepseek-v4-pro` | -0.35 | -1.86 | -2.63u | -2.58 | -1.37~ | 8.904 | no |
| `deepseek-v4.1-flash-free` | -0.48 | -2.17 | -1.97 | -1.34 | -1.58 | 0.955 | yes |
| `dot` | 0.06 | -1.93~ | -0.04u | -0.27u | -1.90u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.14u | -2.67u | -2.61 | -2.76u | - | unknown |
| `flint-40b-free` | - | -3.14u | -2.67u | -2.95u | -2.76u | - | unknown |
| `gemini-3.1-flash-lite` | -0.59 | -2.59 | -3.80~ | -5.41 | -2.57 | 1.822 | yes |
| `gemini-3.1-pro-preview` | -0.16 | -0.90 | -2.94~ | -2.69 | -0.85 | 12.429 | yes |
| `gemini-3.7-flash` | -0.14 | -0.76 | -2.44 | -1.46 | -0.18~ | 5.455 | yes |
| `gemini-3.8-flash-high` | -0.08 | -0.73 | -2.15 | -1.48 | -0.12 | 5.294 | yes |
| `gemini-4-argon-high` | 0.12 | 0.14 | -0.76 | -0.58 | -1.10u | 6.0 | yes |
| `gemini-4-carbon` | 0.16 | -0.03 | 0.00u | -0.04 | -1.72u | 8.463 | yes |
| `gemini-4-hydrogen-high` | -0.13 | -0.64 | -1.23~ | -0.24 | -0.28~ | 17.094 | yes |
| `glm-5.2` | - | -1.93~ | missing! | -2.20 | missing! | 4.527 | no |
| `glm-5.3` | - | -1.74 | -1.83 | -1.68 | missing! | 4.273 | no |
| `glm-5.3-flash-free` | -0.25 | -2.08 | missing! | -1.69 | -1.93 | 1.139 | yes |
| `gpt-5.6-luna` | -0.63 | -2.74 | -2.94~ | -3.07 | -2.09 | 1.598 | yes |
| `gpt-5.6-sol` | -0.01 | -1.36 | -1.76 | -0.82 | -1.19 | 24.5 | yes |
| `gpt-5.6-terra` | -0.07 | -2.60 | -1.74~ | -2.73 | -1.88 | 12.828 | yes |
| `gpt-6-astra` | 0.06 | -1.93 | -0.04u | -0.27u | -0.89 | 26.304 | yes |
| `gpt-6-luna` | -0.63 | -3.14 | -2.34u | -2.30 | -2.76u | 0.774 | yes |
| `gpt-6-pro` | 0.06 | 0.00u | 0.02u | 0.00u | -2.85u | - | yes |
| `gpt-6-sol` | -0.01 | -2.43u | -0.71u | -1.38 | -1.19u | 20.036 | yes |
| `gpt-6.1-sol` | -0.01 | -1.95 | -0.25u | -0.36 | -0.69 | 7.546 | yes |
| `gpt-6.1-terra` | -0.07 | -2.60~ | -0.43u | -1.32 | -0.84 | 5.412 | yes |
| `gpt-6.2-astra` | 0.16 | -1.53~ | 0.73~ | 0.53~ | -0.13 | 16.145 | yes |
| `gpt-6.2-sol` | 0.06 | -1.69~ | 0.25~ | -0.09~ | -0.48~ | 7.546 | yes |
| `gpt-oss-120b` | -1.25 | -6.04 | missing! | -5.88 | -2.52~ | 13.805 | no |
| `grok-4.5` | -0.33 | -1.66 | -2.44~ | -2.19 | -1.26 | 7.508 | yes |
| `grok-4.6` | -0.40 | -1.89 | -2.23~ | -1.84 | -1.19 | 10.5 | yes |
| `grok-4.7` | -0.12 | -2.47 | -1.48 | -1.90 | -1.81~ | 10.449 | yes |
| `grok-4.7-build-fast` | -0.33 | -1.66~ | -2.44~ | -2.19~ | -1.26~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | 0.00~ | -1.19u | 28.0 | yes |
| `kimi-k2.6` | -0.37 | -2.15 | -2.94~ | -2.52 | missing | 2.38 | yes |
| `kimi-k2.7-code` | -0.37 | -2.15~ | -2.94~ | -2.73 | -0.75~ | 2.651 | yes |
| `kimi-k3` | -0.04 | -1.72~ | -1.82~ | -0.49 | -2.17 | 5.813 | yes |
| `kimi-k3.1-pro` | -0.04 | -1.87~ | -1.36~ | -0.23 | -2.74 | 6.0 | yes |
| `mimo-v2.6-flash-free` | -0.47 | -3.17 | missing! | -1.34 | -2.72 | 0.281 | yes |
| `mimo-v2.6-pro` | -0.22 | -1.75 | -1.51 | -1.28 | -1.14 | 0.667 | yes |
| `minimax-m3` | -0.57 | -3.06 | -2.67 | -2.95 | -2.00 | 7.448 | yes |
| `minimax-m3.1-flash` | -0.48 | -2.65~ | missing! | -1.61 | -2.29 | 1.556 | yes |
| `mistral-large-4` | -0.69 | -4.01 | missing! | -2.11 | -1.65 | 3.0 | yes |
| `muse-wonder-1.0` | -0.08 | -1.29 | -1.53 | -0.64 | -0.84 | 19.412 | yes |
| `nemotron-3-ultra-free` | -0.63 | -3.26~ | missing! | -4.06 | -2.31~ | 2.545 | unknown |
| `nemotron-3.5-lightning-free` | -0.98 | -4.63~ | missing! | -6.31~ | -2.52~ | 6.807 | no |
| `nemotron-3.5-super-vl-free` | -0.63 | -3.26~ | missing! | -4.90 | -2.31~ | 10.35 | yes |
| `qwen3-coder-next` | -0.18 | -1.15~ | -2.99~ | -1.01~ | -0.61~ | 4.013 | no |
| `qwen3-flash-2025-07-28` | - | -3.14u | -2.67u | -2.95u | -2.76u | - | unknown |
| `qwen3.6-plus` | -0.52 | -2.80 | -2.97~ | -3.10 | -2.64 | 1.405 | yes |
| `qwen3.7-max` | -0.52 | -2.80~ | -2.97~ | -2.37 | -2.64~ | 9.529 | no |
| `qwen3.8-max` | -0.18 | -1.15 | -1.58 | -1.35 | -2.07 | 5.299 | yes |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
