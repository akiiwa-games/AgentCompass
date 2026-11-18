# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (creativity, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Aesthetics evaluates design at level 1 and the weaker of design and image recognition at levels 2 and 3 (the column shows design); tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.58 | -3.41 | -3.29 | -3.61 | missing! | - | unknown |
| `claude-fable-5-1` | -0.03 | -0.78 | -0.50 | -0.71 | 0.10 | 39.05 | yes |
| `claude-fable-5-2` | 0.20 | 0.21u | 0.10u | 0.25u | 0.00u | 28.383 | yes |
| `claude-haiku-4-5` | -0.75 | -3.44 | -1.92~ | -4.87 | -2.57~ | 30.0 | yes |
| `claude-haiku-5-5` | -0.69 | -4.11 | -1.72 | -2.17 | -2.55~ | 3.0 | yes |
| `claude-opus-4-6` | -0.03 | -0.49 | -0.35~ | -2.26 | 0.02~ | 45.617 | yes |
| `claude-opus-4-7` | -0.05 | -0.73 | -0.37~ | -2.28 | -0.02~ | 45.617 | yes |
| `claude-opus-4-8` | -0.20 | -1.25 | -2.47~ | -2.64 | -0.48~ | 45.617 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 28.0 | yes |
| `claude-sonnet-4-6` | -0.28 | -1.80 | -3.49~ | -2.66 | -0.76~ | 39.589 | yes |
| `claude-sonnet-5-5` | -0.07 | -2.06 | -0.90 | -0.65 | -1.34 | 20.25 | yes |
| `deepseek-v4-pro` | -0.35 | -1.94 | -2.66u | -2.69 | -1.40~ | 8.904 | no |
| `deepseek-v4.1-flash-free` | -0.48 | -2.26 | -1.98 | -1.38 | -1.62 | 0.955 | yes |
| `dot` | 0.06 | -2.02~ | -0.06u | -0.25u | -1.67u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.32u | -2.80u | -2.70 | -2.86u | - | unknown |
| `flint-40b-free` | - | -3.32u | -2.80u | -2.90u | -2.86u | - | unknown |
| `gemini-3.1-flash-lite` | -0.59 | -2.70 | -3.81~ | -5.60 | -2.62 | 1.822 | yes |
| `gemini-3.1-pro-preview` | -0.16 | -0.93 | -3.01 | -2.76 | -0.87 | 12.429 | yes |
| `gemini-3.7-flash` | -0.14 | -0.79 | -2.49 | -1.51 | -0.18~ | 5.455 | yes |
| `gemini-3.8-flash-high` | -0.08 | -0.76 | -2.16 | -1.53 | -0.12 | 5.294 | yes |
| `gemini-4-argon-high` | 0.12 | 0.14 | -0.75 | -0.60 | -0.99u | 6.0 | yes |
| `gemini-4-carbon` | 0.16 | -0.03 | 0.00u | -0.03 | -1.55u | 8.463 | yes |
| `gemini-4-hydrogen-high` | -0.13 | -0.67 | -0.20 | -0.25 | -0.29~ | 17.094 | yes |
| `glm-5.2` | - | -2.02~ | -2.93~ | -2.30 | missing! | 4.527 | no |
| `glm-5.3` | - | -1.81 | -1.83 | -1.75 | missing! | 4.273 | no |
| `glm-5.3-flash-free` | -0.25 | -2.17 | -2.02 | -1.74 | -1.74 | 1.139 | yes |
| `gpt-5.6-luna` | -0.63 | -2.86 | -2.97~ | -3.17 | -2.14 | 1.598 | yes |
| `gpt-5.6-sol` | -0.01 | -1.41 | -1.81 | -0.84 | -1.22 | 24.5 | yes |
| `gpt-5.6-terra` | -0.07 | -2.71 | -1.72~ | -2.82 | -1.92 | 12.828 | yes |
| `gpt-6-astra` | 0.06 | -2.02 | -0.06u | -0.25u | -0.80 | 26.304 | yes |
| `gpt-6-luna` | -0.63 | -3.28 | -2.37u | -2.37 | -2.82u | 0.774 | yes |
| `gpt-6-pro` | 0.06 | 0.00u | 0.05u | -0.00u | -2.60u | - | yes |
| `gpt-6-sol` | -0.01 | -2.53u | -0.93u | -1.42 | -0.74u | 20.036 | yes |
| `gpt-6.1-sol` | -0.01 | -2.03 | -0.26u | -0.37 | -0.70 | 7.546 | yes |
| `gpt-6.1-terra` | -0.07 | -2.71~ | -0.44u | -1.36 | -0.86 | 5.412 | yes |
| `gpt-6.2-astra` | 0.16 | -1.60~ | 0.70~ | 0.55~ | -0.11 | 16.145 | yes |
| `gpt-6.2-sol` | 0.06 | -1.76~ | 0.24~ | -0.09~ | -0.49~ | 7.546 | yes |
| `gpt-oss-120b` | -1.25 | -6.30 | -3.86~ | -6.11 | -2.57~ | 13.805 | no |
| `grok-4.5` | -0.33 | -1.73 | -2.56~ | -2.27 | -1.29 | 7.508 | yes |
| `grok-4.6` | -0.40 | -1.97 | -2.24~ | -1.90 | -1.21 | 10.5 | yes |
| `grok-4.7` | -0.12 | -2.58 | -1.50 | -1.95 | -1.84~ | 10.449 | yes |
| `grok-4.7-build-fast` | -0.33 | -1.73~ | -2.56~ | -2.27~ | -1.29~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | 0.00~ | -0.74u | 28.0 | yes |
| `kimi-k2.6` | -0.37 | -2.24 | -3.09~ | -2.60 | missing | 2.38 | yes |
| `kimi-k2.7-code` | -0.37 | -2.24~ | -3.09~ | -2.82 | -0.77~ | 2.651 | yes |
| `kimi-k3` | -0.04 | -1.79~ | -1.84~ | -0.51 | -2.22 | 5.813 | yes |
| `kimi-k3.1-pro` | -0.04 | -1.95~ | -0.45 | -0.24 | -2.48 | 6.0 | yes |
| `mimo-v2.6-flash-free` | -0.47 | -3.31 | -1.92 | -1.38 | -2.79 | 0.281 | yes |
| `mimo-v2.6-pro` | -0.22 | -1.82 | -1.51 | -1.34 | -1.16 | 0.667 | yes |
| `minimax-m3` | -0.57 | -3.19 | -2.70 | -3.04 | -2.04 | 7.448 | yes |
| `minimax-m3.1-flash` | -0.48 | -2.76~ | -1.92 | -1.66 | -2.34 | 1.556 | yes |
| `mistral-large-4` | -0.69 | -4.18 | -2.40 | -2.18 | -1.69 | 3.0 | yes |
| `muse-wonder-1.0` | -0.08 | -1.35 | -1.55 | -0.68 | -0.86 | 19.412 | yes |
| `nemotron-3-ultra-free` | -0.63 | -3.40~ | -4.09 | -4.23 | -2.36~ | 2.545 | unknown |
| `nemotron-3.5-lightning-free` | -0.98 | -4.83~ | -3.85~ | -6.45~ | -2.57~ | 6.807 | no |
| `nemotron-3.5-super-vl-free` | -0.63 | -3.40~ | -3.48~ | -5.00 | -2.36~ | 10.35 | yes |
| `qwen3-coder-next` | -0.18 | -1.20~ | -3.14~ | -1.06~ | -0.62~ | 4.013 | no |
| `qwen3-flash-2025-07-28` | - | -3.32u | -2.80u | -2.90u | -2.86u | - | unknown |
| `qwen3.6-plus` | -0.52 | -2.92 | -3.04~ | -3.20 | -2.70 | 1.405 | yes |
| `qwen3.7-max` | -0.52 | -2.92~ | -3.04 | -2.46 | -2.70~ | 9.529 | no |
| `qwen3.8-max` | -0.18 | -1.20 | -1.58 | -1.38 | -1.87 | 5.299 | yes |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
