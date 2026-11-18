# Model capability catalogue

Generated from `accounts.json` and `five-axis-scores.json` by `scripts/capability_catalog.py`; do not edit this view.

Each model is scored on four categories (creativity, coding, aesthetics, tool operation) that gather the public boards as panel standard scores (0 = the anchor at High, one unit = one panel standard deviation), and on the quota economy; nothing else scores a model. Aesthetics evaluates design at level 1 and the weaker of design and image recognition at levels 2 and 3 (the column shows design); tool operation shows the weaker of responsiveness and image recognition. Known catalogue IDs do not prove sign-in, plan capacity, current price, or live model availability.

Route order and plan capacity ship as calibrated public defaults; plan capacity follows the discovered plan (`plan-templates.json`). Disabled models and bundles are personal settings in `accounts-override.json`. Unknown hourly cost remains unknown; it does not mean unlimited.

| Model | Tier | Creativity | Coding | Aesthetics | Tool operation | USD per hour | Image input |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `beam-free` | -0.58 | -3.30 | -3.31 | -3.51 | missing! | - | unknown |
| `claude-fable-5-1` | -0.03 | -0.82 | -0.52 | -0.72 | 0.03 | 39.05 | yes |
| `claude-fable-5-2` | 0.20 | 0.20u | 0.05u | 0.26u | -0.06u | 28.383 | yes |
| `claude-haiku-4-5` | -0.75 | -3.32 | -1.87~ | -4.70 | -2.32~ | 30.0 | yes |
| `claude-haiku-5-5` | -0.69 | -3.97 | -2.04~ | -2.11 | -2.16~ | 3.0 | yes |
| `claude-opus-4-5` | -0.27 | -1.23 | -1.45~ | -2.82 | -0.65~ | 45.617 | yes |
| `claude-opus-4-6` | -0.03 | -0.47 | -1.45~ | -2.19 | 0.02~ | 45.617 | yes |
| `claude-opus-4-7` | -0.05 | -0.71 | -1.45~ | -2.22 | -0.02~ | 45.617 | yes |
| `claude-opus-4-8` | -0.20 | -1.20 | -2.38~ | -2.56 | -0.47~ | 45.617 | yes |
| `claude-opus-5-5` | 0.00 | 0.00 | 0.00 | -0.00 | 0.00 | 28.0 | yes |
| `claude-sonnet-4-6` | -0.28 | -1.74 | -3.36~ | -2.56 | -0.75~ | 39.589 | yes |
| `claude-sonnet-5-5` | -0.07 | -1.98 | -0.90 | -0.63 | -1.32 | 20.25 | yes |
| `codestral-2508` | -0.69 | -4.04~ | -3.22~ | -4.65 | -1.68~ | 1.286 | no |
| `deepseek-v4-flash-promo` | -0.48 | -2.19~ | -1.98~ | -1.34~ | -1.60~ | - | unknown |
| `deepseek-v4-pro` | -0.35 | -1.87 | -2.64u | -2.59 | -1.87~ | 4.847 | no |
| `deepseek-v4.1-flash-economy` | -0.48 | -2.19~ | -1.98~ | -1.34~ | -1.60~ | - | unknown |
| `deepseek-v4.1-flash-free` | -0.48 | -2.19 | -1.98 | -1.34 | -1.60 | 0.955 | yes |
| `deepseek-v4.1-flash-promo` | -0.48 | -2.19~ | -1.98~ | -1.34~ | -1.60~ | - | unknown |
| `dot` | 0.06 | -1.96~ | -0.05u | -0.29u | -1.92u | - | yes |
| `doubao-seed-2-1-pro-260628` | - | -3.17u | -2.68u | -2.62 | -2.81u | - | unknown |
| `doubao-seed-2.0-mini` | - | missing! | missing! | missing! | missing! | - | unknown |
| `doubao-seed-2.1-lite` | - | missing! | missing! | missing! | missing! | - | unknown |
| `doubao-seed-2.1-pro` | - | missing! | missing! | missing! | missing! | - | unknown |
| `doubao-seed-evolving` | - | missing! | missing! | missing! | missing! | - | unknown |
| `flint-40b-free` | - | -3.17u | -2.68u | -2.96u | -2.81u | - | unknown |
| `gemini-2.5-flash-lite` | -0.27 | -1.55~ | -2.88~ | -3.20~ | -0.71~ | 0.704 | yes |
| `gemini-3-flash-preview` | -0.27 | -1.55 | -3.03~ | -3.20 | -0.71~ | 3.313 | yes |
| `gemini-3.1-flash-lite` | -0.59 | -2.61 | -3.80~ | -5.45 | -2.59 | 1.822 | yes |
| `gemini-3.1-pro-preview` | -0.16 | -0.90 | -2.95~ | -2.69 | -0.86 | 12.429 | yes |
| `gemini-3.5-flash-lite` | -0.42 | -2.07 | -3.45~ | -3.56 | -1.72 | 2.347 | yes |
| `gemini-3.6-flash` | -0.19 | -1.17 | -2.63 | -1.94 | -0.72 | 5.455 | yes |
| `gemini-3.7-flash` | -0.14 | -0.77 | -2.44 | -1.47 | -0.18~ | 5.455 | yes |
| `gemini-3.8-flash-high` | -0.08 | -0.73 | -2.16 | -1.50 | -0.13 | 5.294 | yes |
| `gemini-4-argon-high` | 0.12 | 0.14 | -0.76 | -0.60 | -1.16u | 6.0 | yes |
| `gemini-4-carbon` | 0.16 | -0.03 | 0.00u | -0.05 | -1.76u | 8.463 | yes |
| `gemini-4-hydrogen-high` | -0.13 | -0.65 | -1.23~ | -0.25 | -0.26~ | 17.094 | yes |
| `gemma-4-26b-a4b-it` | -0.56 | -3.00 | -2.74~ | -4.35 | missing | 3.111 | yes |
| `gemma-4-31b-it` | -0.44 | -2.52 | -2.33~ | -4.22 | missing | 3.423 | yes |
| `glm-4.5` | -0.77 | -3.70 | -3.10~ | -3.66 | missing! | 14.547 | no |
| `glm-4.5-air` | -1.08 | -4.84 | -3.98~ | -4.10 | missing! | 3.468 | no |
| `glm-4.5-flash` | -1.14 | -5.42~ | -4.59~ | -4.38~ | -2.96~ | - | unknown |
| `glm-4.5v` | -1.24 | -5.30 | -4.11~ | -4.77~ | missing! | 14.313 | yes |
| `glm-4.6` | -0.66 | -2.96 | -2.78~ | -4.03 | missing! | 12.919 | no |
| `glm-4.6v` | -1.03 | -4.28 | -3.85~ | -3.93~ | missing! | 6.737 | yes |
| `glm-4.6v-flash` | -1.14 | -5.42~ | -4.59~ | -4.38~ | -2.96~ | - | unknown |
| `glm-4.7` | -0.53 | -2.85 | -2.39~ | -3.30 | missing! | 10.423 | no |
| `glm-4.7-flash` | -1.14 | -5.42 | -4.59~ | -4.38 | -2.96~ | - | no |
| `glm-4.7-flashx` | -1.14 | -5.42~ | -4.59~ | -4.38~ | -2.96~ | - | unknown |
| `glm-5` | -0.40 | -1.79 | -2.02~ | -3.04 | missing! | 25.371 | no |
| `glm-5.1` | -0.34 | -1.81 | -1.84~ | -2.45 | missing! | 33.793 | no |
| `glm-5.2` | -0.34 | -1.96~ | -2.87~ | -2.20 | -0.62~ | 4.527 | no |
| `glm-5.3` | -0.34 | -1.75 | -1.84 | -1.69 | -0.62~ | 4.273 | no |
| `glm-5.3-flash-free` | -0.25 | -2.10 | -2.02~ | -1.70 | -1.93 | 1.139 | yes |
| `glm-5.3-flashx` | -1.14 | -5.42~ | -4.59~ | -4.38~ | -2.96~ | 2.837 | yes |
| `glm-5v-turbo` | -0.53 | -2.85~ | -2.39~ | -3.33 | missing! | 30.538 | yes |
| `gpt-5.2` | -0.56 | -3.34 | -2.92~ | -4.00 | missing! | - | yes |
| `gpt-5.2-pro` | - | missing! | missing! | missing! | missing! | - | yes |
| `gpt-5.4` | -0.25 | -1.95 | -1.78~ | -3.90 | missing! | - | yes |
| `gpt-5.4-mini` | -0.48 | -2.96 | -2.60~ | -3.92 | missing! | - | yes |
| `gpt-5.4-pro` | - | missing! | missing! | missing! | missing! | - | yes |
| `gpt-5.5` | -0.21 | -1.70 | -1.58~ | -2.50 | missing! | - | yes |
| `gpt-5.5-pro` | - | missing! | missing! | missing! | missing! | - | yes |
| `gpt-5.6-luna` | -0.63 | -2.76 | -2.95~ | -3.08 | -2.11 | 1.598 | yes |
| `gpt-5.6-sol` | -0.01 | -1.37 | -1.76 | -0.80 | -1.20 | 24.5 | yes |
| `gpt-5.6-terra` | -0.07 | -2.62 | -1.74~ | -2.73 | -1.89 | 12.828 | yes |
| `gpt-6-astra` | 0.06 | -1.96 | -0.05u | -0.29u | -0.92 | 26.304 | yes |
| `gpt-6-luna` | -0.63 | -3.17 | -2.35u | -2.31 | -2.81u | 0.774 | yes |
| `gpt-6-pro` | 0.06 | 0.00u | 0.02u | -0.01u | -2.90u | - | yes |
| `gpt-6-sol` | -0.01 | -2.45u | -0.72u | -1.39 | -1.19u | 20.036 | yes |
| `gpt-6.1-sol` | -0.01 | -1.96 | -0.25u | -0.36 | -0.69 | 7.546 | yes |
| `gpt-6.1-terra` | -0.07 | -2.62~ | -0.42u | -1.33 | -0.85 | 5.412 | yes |
| `gpt-6.2-astra` | 0.16 | -1.57~ | 0.72~ | 0.51~ | -0.10 | 16.145 | yes |
| `gpt-6.2-sol` | 0.06 | -1.71~ | 0.24~ | -0.11~ | -0.51~ | 7.546 | yes |
| `gpt-oss-120b` | -1.25 | -6.08 | -4.87~ | -5.88 | -3.71~ | 13.812 | no |
| `gpt-oss-20b` | -1.52 | -7.04 | -5.35~ | -6.98~ | -3.71~ | - | no |
| `grok-4.20-0309-non-reasoning` | -0.52 | -2.43~ | -3.01~ | -4.41~ | -2.03~ | - | unknown |
| `grok-4.20-0309-reasoning` | -0.52 | -2.43~ | -3.01~ | -4.41~ | -2.03~ | - | unknown |
| `grok-4.20-multi-agent-0309` | -0.52 | -2.43~ | -3.01~ | -4.41~ | -2.03~ | - | unknown |
| `grok-4.3` | -0.52 | -2.43 | -2.65~ | -4.41 | -2.03~ | 4.726 | yes |
| `grok-4.5` | -0.33 | -1.67 | -2.45~ | -2.20 | -1.27 | 7.508 | yes |
| `grok-4.6` | -0.40 | -1.91 | -2.25~ | -1.85 | -1.19 | 10.5 | yes |
| `grok-4.7` | -0.12 | -2.49 | -1.48 | -1.90 | -1.82~ | 10.449 | yes |
| `grok-4.7-build-fast` | -0.52 | -2.43~ | -3.01~ | -4.41~ | -2.03~ | - | unknown |
| `grok-bot` | 0.00 | 0.00~ | 0.00~ | -0.00~ | -1.19u | 28.0 | yes |
| `grok-build-0.1` | -0.52 | -2.43~ | -3.01~ | -4.41~ | -2.03~ | 4.398 | yes |
| `hy3` | -0.42 | -2.39 | -2.94~ | -2.61 | -1.43~ | 1.2 | no |
| `hy4` | -0.13 | -1.24~ | -0.89~ | -2.46 | -2.35~ | 2.26 | yes |
| `hy4-flash` | - | -1.64~ | -1.29~ | -2.86 | -2.35~ | 0.189 | yes |
| `hy4-preview` | -0.13 | -1.24~ | -1.75 | -2.11 | -2.35~ | 1.872 | no |
| `k3.1-pro-highspeed` | -0.37 | -2.16~ | -2.95~ | -2.53~ | -0.76~ | - | unknown |
| `kimi-for-coding-highspeed` | -0.37 | -2.16~ | -2.95~ | -2.53~ | -0.76~ | - | unknown |
| `kimi-k2.6` | -0.37 | -2.16 | -2.95~ | -2.53 | missing | 2.38 | yes |
| `kimi-k2.7-code` | -0.37 | -2.16~ | -2.95~ | -2.74 | -0.76~ | 2.651 | yes |
| `kimi-k2.8-preview` | -0.37 | -2.16~ | -2.95~ | -2.53~ | -0.76~ | - | unknown |
| `kimi-k3` | -0.04 | -1.74~ | -1.83~ | -0.49 | -2.19 | 5.813 | yes |
| `kimi-k3.1-pro` | -0.04 | -1.90~ | -1.36~ | -0.25 | -2.75 | 6.0 | yes |
| `mimo-v2.5` | -0.59 | -3.16 | -2.59~ | -2.89 | -1.58~ | 1.889 | yes |
| `mimo-v2.5-pro` | -0.33 | -2.15 | -1.83~ | -2.65 | -0.96~ | 2.205 | no |
| `mimo-v2.6-flash-free` | -0.47 | -3.20 | -2.32~ | -1.34 | -2.75 | 0.281 | yes |
| `mimo-v2.6-pro` | -0.22 | -1.76 | -1.51 | -1.29 | -1.15 | 0.667 | yes |
| `minimax-m3` | -0.57 | -3.08 | -2.68 | -2.96 | -2.01 | 7.448 | yes |
| `minimax-m3.1-flash` | -0.48 | -2.61~ | -2.73~ | -1.61 | -2.30 | 1.556 | yes |
| `ministral-14b-2512` | - | -5.47~ | -4.24~ | -2.34~ | -11.43 | 0.798 | yes |
| `ministral-3b-2512` | - | -5.47~ | -4.24~ | -2.34~ | -15.08 | 0.399 | yes |
| `ministral-8b-2512` | - | -5.47~ | -4.24~ | -2.34~ | -12.64 | 0.598 | yes |
| `mistral-large-4` | -0.69 | -4.04 | -2.41~ | -2.12 | -1.68 | 3.0 | yes |
| `mistral-small-2603` | -0.69 | -4.04~ | -3.22~ | -4.65~ | -1.68~ | 0.665 | yes |
| `muse-ember` | -0.08 | -1.30~ | -2.00~ | -0.29 | -0.85~ | 2.769 | yes |
| `muse-spark-1.3` | -0.08 | -2.28~ | -2.08~ | -0.84~ | -1.04~ | 7.297 | yes |
| `muse-spark-open` | -0.08 | -1.30~ | -3.41~ | -1.79~ | -1.92~ | 50.005 | yes |
| `muse-wonder-1.0` | -0.08 | -1.30 | -1.54 | -0.66 | -0.85 | 19.412 | yes |
| `nemotron-3-ultra-free` | -0.63 | -3.21~ | -4.11~ | -4.08 | -2.01~ | 2.545 | unknown |
| `nemotron-3.5-lightning-free` | -0.98 | -4.53~ | -4.19~ | -6.28~ | -2.97~ | 6.647 | no |
| `nemotron-3.5-super-vl-free` | -0.63 | -3.21~ | -3.47~ | -4.91 | -2.01~ | 10.364 | yes |
| `qwen-max` | -1.52 | -5.93 | -6.34~ | -7.10~ | -5.47~ | - | unknown |
| `qwen-plus` | -1.29 | -5.12 | -5.69~ | -6.21~ | -4.84~ | 1.75 | no |
| `qwen3-coder-flash` | -0.89 | -4.58~ | -4.01~ | -5.58~ | missing! | - | no |
| `qwen3-coder-next` | -0.41 | -2.20~ | -3.00~ | -2.38~ | -1.39~ | 4.013 | no |
| `qwen3-flash-2025-07-28` | -0.89 | -3.17u | -2.68u | -2.96u | -2.81u | - | unknown |
| `qwen3-omni-flash` | -0.89 | -4.58~ | -4.01~ | -5.58~ | missing! | - | unknown |
| `qwen3-vl-plus` | -1.52 | -5.93~ | -6.18~ | -7.10~ | -5.47~ | - | unknown |
| `qwen3.5-122b-a10b` | -0.73 | -3.87 | -2.95~ | -4.33 | missing | - | yes |
| `qwen3.5-27b` | -0.79 | -4.07 | -3.13~ | -4.36 | missing! | - | yes |
| `qwen3.5-35b-a3b` | -0.91 | -4.46 | -3.47~ | -5.48 | missing! | 2.996 | yes |
| `qwen3.5-397b-a17b` | -0.53 | -2.83 | -2.37~ | -3.55 | missing | - | yes |
| `qwen3.5-flash` | -0.89 | -4.58 | -3.44~ | -5.58 | -2.73~ | - | unknown |
| `qwen3.5-plus` | -1.52 | -5.93~ | -6.18~ | -7.10~ | -5.47~ | - | yes |
| `qwen3.6-27b` | -0.73 | -3.87~ | -2.95~ | -4.33~ | missing! | - | yes |
| `qwen3.6-35b-a3b` | -0.73 | -3.87~ | -2.95~ | -4.33~ | missing | - | yes |
| `qwen3.6-flash` | -0.89 | -4.58~ | -3.44~ | -5.58~ | -2.73~ | - | yes |
| `qwen3.6-plus` | -0.52 | -2.82 | -3.45~ | -3.11 | -2.66 | 1.985 | yes |
| `qwen3.7-flash` | -0.89 | -4.58~ | -3.44~ | -5.58~ | -2.73~ | 0.208 | yes |
| `qwen3.7-max` | -0.41 | -2.20~ | -2.98~ | -2.38 | -1.39~ | 9.529 | no |
| `qwen3.7-plus` | -0.41 | -2.20 | -3.16~ | -2.45 | -1.39 | 2.204 | yes |
| `qwen3.8-27b` | -0.59 | -4.03 | -2.55 | -1.86 | -1.43 | 3.478 | yes |
| `qwen3.8-flash` | -0.89 | -4.58~ | -4.45~ | -5.58~ | -2.73~ | 0.367 | yes |
| `qwen3.8-max` | -0.18 | -1.16 | -1.58 | -1.36 | -2.07 | 5.299 | yes |
| `qwen3.8-omni-flash` | -0.89 | -4.58~ | -4.45~ | -5.58~ | -2.73~ | 0.367 | yes |
| `qwen4-max` | -0.06 | -0.70~ | -1.55~ | -0.31~ | -2.69~ | 5.478 | yes |
| `step-3.5-flash` | -0.92 | -4.41 | -3.52~ | -4.31~ | -2.81~ | - | unknown |
| `step-3.5-flash-2603` | -0.92 | -4.41~ | -3.52~ | -4.31~ | -2.81~ | - | unknown |
| `step-3.7-flash` | -0.92 | -4.41~ | -3.52~ | -4.31 | -2.81~ | 1.254 | yes |
| `step-5-preview` | -0.47 | -2.67 | -2.25 | -2.06 | -2.19 | 3.894 | yes |
| `voxtral-small-2507` | - | missing! | missing! | missing! | missing! | - | unknown |

Tier is the arena.ai Overall position at High relative to the anchor (or its linear alignment from the Artificial Analysis Intelligence Index at High, or its same-series sibling tier); it is not a score and only estimates facets a model lacks when the panel regression has positive leave-one-out skill. A value marked `~` holds such an estimate (a same-series sibling, else an accepted fit of that facet on the tier); `u` marks a manual score (`user-scores.json`); `!` marks a category with one or more missing facets (`tool_operation` keeps its level-2 responsiveness when `image_recognition` is missing; `coding` stays `missing!` so a missing facet is never silently skipped); `missing` excludes the model under mode B when a demanded level requires that facet. The record of each estimate is in `five-axis-scores.json` (`estimate`).

## Effective policy

Routes describe task scope and modality requirements and ship a calibrated harness order. A route can fix the harness order or imply a modality flag; it does not rank models. The judge receives this effective policy.

## Regeneration

Run `python -X utf8 skills/agent-compass/scripts/capability_catalog.py` from the repository root. Use `--check` to verify both generated views without writing them.
