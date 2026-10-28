# Model Capability Map & Quantitative Basis

Updated: `2026-10-27` (core calibration: `2026-10-19`)
Machine-readable sources: [`accounts.json`](accounts.json) · [`model-capabilities.json`](model-capabilities.json)

## 1. Zero-to-Frontier-Ceiling Normalization Formula

Instead of min-max normalizing within only the 7 shortlisted frontier models (which artificially compresses the 7th frontier model near `0.0`), each benchmark metric $x$ is normalized against its absolute domain floor $B_0$ and current frontier ceiling $B_{\max}$:

$$\operatorname{norm}(x, B_0, B_{\max}) = \operatorname{clamp}\!\left(\frac{x - B_0}{B_{\max} - B_0},\, 0.0,\, 1.0\right)$$

### Dimension Composite Weights

| Capability Dimension | Independent Benchmark Inputs & Weights |
| :--- | :--- |
| **`reasoning`** | `0.30` AA Intelligence Index v4.3.2 ($B_0=15, B_{\max}=60$) + `0.15` Humanity's Last Exam ($0.10..0.65$) + `0.10` CritPt ($0.00..0.35$) + `0.45` Tier Anchor ($\frac{\text{tier}-1.5}{3.5}$) |
| **`coding`** | `0.30` Terminal-Bench 4.0 ($0.05..0.65$) + `0.25` Terminal-Bench 2.0 ($0.10..0.68$) + `0.25` SciCode ($0.20..0.68$) + `0.20` GDPval-AA ($0.15..0.70$) |
| **`exploration`** | `0.35` Bug Hunt Bench Recall ($5..50$ of $105$ planted bugs) + `0.30` Mercor BrowseComp ($0.15..0.76$) + `0.20` AA-Briefcase ($0.20..0.62$) + `0.15` AA-LCR ($0.40..0.88$) |
| **`anti_hallucination`** | `0.30` AA-Omniscience Accuracy ($0.20..0.65$) + `0.25` Bug Hunt Precision $\frac{\text{TP}}{\text{TP}+\text{FP}}$ ($0.10..0.88$) + `0.25` AA-Omniscience Index ($-10..48$) + `0.20` Tier Anchor |
| **`visual`** | MMMU-Pro ($84.5\%..87.7\%$) + calibrated visual inspection order (`Opus > Astra > Argon > Sol > Sonnet > Flash`; `Grok/Dot = 0.0`) |
| **`gui`** | OSWorld / ScreenSpot-Pro + local `cua_repl` screen/browser grounding (`Astra > Sol > Argon > Opus = Sonnet > Flash`; `Grok/Dot = 0.0`) |

---

## 2. Quantitative Capability Matrix (`2026-10-19`)

| Canonical Model | Tier | `reasoning` | `coding` | `exploration` | `anti_halluc` | `visual` | `gui` | AA High / Max | TB 4.0 / 2.0 | Bug Hunt TP (Prec) | Omniscience | $/h (`accounts.json` / 14d local) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`gpt-6-pro`** | **5.0** | `1.00` | `0.90` | `0.78` | `0.95` | `0.00` | `0.00` | Advisor | — | — | Ceiling | Web bridge |
| **`claude-opus-5-5`** | **4.6** | `0.89` | `0.91` | `0.88` | `0.92` | `1.00` | `0.55` | `53.6` / `57.6` | `56.6%` / `64.8%` | `31.7`–`41.7` (`74.8%`–`82.2%`) | `40.6` / `46.4` | `$7.5` (`$9.30`/h, 207h) |
| **`gpt-6-astra`** | **4.2** | `0.80` | `0.79` | `0.80` | `0.78` | `0.80` | `0.95` | `50.9` / `52.7` | `54.0%` / `58.2%` | `35.0`–`45.0` (`46.7%`) | `43.7` / `43.4` | `$22.6` (`$22.75`/h, 42h) |
| **`dot`** | **4.2** | `0.80` | `0.79` | `0.80` | `0.78` | `0.00` | `0.00` | Astra-class | — | — | — | `$0.0` (remote leaf) |
| **`gemini-4-argon-high`** | **4.0** | `0.78` | `0.83` | `0.86` | `0.81` | `0.75` | `0.62` | `52.6` / `52.6` | `57.1%` / — | `40.7` (`84.8%`, $n=3$) | `42.4` (`84.9%` non-halluc) | `8x` Flash pool (95h) |
| **`claude-sonnet-5-5`** | **3.9** | `0.74` | `0.82` | `0.89` | `0.73` | `0.60` | `0.55` | `46.8` / `56.0` | `43.9%` / `61.8%` | `34.3`–`51.3` (`76.2%`, #1 Max) | `21.0` / `32.3` | `$4.4` (`$3.73`/h, 64h) |
| **`gpt-6.1-sol`** | **3.6** | `0.71` | `0.85` | `0.62` | `0.74` | `0.70` | `0.82` | `50.2` / `51.8` | `51.5%` / `58.2%` | `36.5`–`44.3` (`47.7%`) | `41.5` / `41.5` | `$2.6` (`$2.19`/h, 159h) |
| **`grok-4.7`** | **3.4** | `0.59` | `0.58` | `0.60` | `0.62` | `0.00` | `0.00` | `46.3` / `46.4` | `24.7%` / `37.6%` | `19.3`–`29.0` (`55.1%`) | `30.9` / `32.0` | Inactive subscription |
| **`gemini-3.8-flash-high`** | **3.0** | `0.52` | `0.47` | `0.50` | `0.72` | `0.30` | `0.42` | `40.9` (Low `33.5`) | `19.7%` / — | `18.0` (`81.8%`, $n=3$) | `29.6` | `1x` Flash pool (39h) |

> [!NOTE]
> `claude-fable-5-1` (AA High `51.2` / Max `53.4`, Bug Hunt `33`–`43/105`) remains paused (`disabled: "Paused until Fable 5.5 ships"`).

---

## 3. Effort Curves (`scoring.effort_curves`)

Derived from AA v4.3.2 effort sweeps ($\Delta\text{tier} \approx \frac{\text{AA}(e) - \text{AA}(\text{high})}{10}$, $\text{burn}(e) \approx 0.70 \times \frac{\text{Cost}(e)}{\text{Cost}(\text{high})}$) and Bug Hunt Bench multi-run sweeps:

| Model | `low` ($\Delta\text{tier}$ / burn) | `medium` ($\Delta\text{tier}$ / burn) | `high` ($\Delta\text{tier}$ / burn) | `xhigh` ($\Delta\text{tier}$ / burn) | `max` ($\Delta\text{tier}$ / burn) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **`claude-opus-5-5`** | `-1.1` / `0.21x` | `-0.2` / `0.51x` | `0.0` / `0.70x` | `+0.2` / `1.33x` | `+0.4` / `2.30x` |
| **`claude-sonnet-5-5`** | `-1.1` / `0.27x` | `-0.6` / `0.38x` | `0.0` / `0.70x` | `+0.5` / `1.59x` | `+0.9` / `4.34x` |
| **`gpt-6-astra`** | `-0.5` / `0.33x` | `-0.1` / `0.63x` | `0.0` / `0.70x` | `+0.1` / `0.94x` | `+0.2` / `1.32x` |
| **`gpt-6.1-sol`** | `-0.8` / `0.29x` | `-0.2` / `0.47x` | `0.0` / `0.70x` | `+0.1` / `0.85x` | `+0.2` / `1.58x` |
| **`gemini-4-argon-high`** | `-0.3` / `0.63x` | `-0.1` / `0.70x` | `0.0` / `0.70x` | — | — |

---

## 4. Route Defaults & Difficulty Floors

- **Difficulty Ability Floors (`scoring.ability_floor`)**:
  - `routine` (`3.0`): Admits all active models (`flash_high` wins routine engineering/tool work via fit + low burn).
  - `hard` (`3.3`): Excludes `flash_high`; minimum-sufficient scoring gives full ability at the floor and charges tier above `floor + 0.3 = 3.6` by `0.08 * min(1, (tier - 3.6) / 0.6) * usd_per_hour / $10` (price factor capped at 3.0, so `Astra` at $22.6/h pays 2.26x what a $10/h model would; GUI work keeps the $10 cap and is exempt above `reasoning_depth` 1).
  - `coupled` (`3.6`): Admits `Sol 6.1`, `Sonnet 5.5`, `Argon 4`, `Astra`, `Opus 5.5`, `Dot`; unlocks `xhigh`/`max` effort gains.
  - `frontier` (`4.2`): Admits `Opus 5.5`, `Astra`, `Dot`, `GPT-6 Pro` (plus `sonnet55_xhigh` if enabled). Adding `needs.critical: true` raises the floor by `+0.3` (`4.5` on `frontier`, leaving `Opus 5.5` and `GPT-6 Pro`).
- **Harness quality (`scoring.harness_factors`) and harness order (`pi` vs `codex`)**:
  - `harness_factor` multiplies weighted fit and ability. Default `1.0`. Antigravity Claude (`harness` `antigravity-resilient` or `antigravity`, `model` prefix `claude`) starts at `0.9`. Gemini on Antigravity stays `1.0`.
  - `harness_order` remains the sibling preference for the same account, model, effort and action. The best sibling keeps its score, so this does not change which model wins. One place behind subtracts `harness_step` (`0.03`), also recorded as `harness_order_factor = 1 - 0.03 / value`. `web_research`, `text_in_text_out`, `exploration_analysis` and `research_packet` list `["pi", "codex"]`. `debug_review` and `computer_use` list `["codex", "pi"]`. Other routes use `scoring.profile.harness_by_style` (engineering Codex, exploration Pi) or the policy default `["codex", "pi"]`.
- **Subjective Preference Vectors (`models[*].subjective` & `TaskProfile.divergence`)**:
  - Rather than multiplying `work_kind` routes, each model carries a 3D subjective vector (`divergence`, `global_rigor`, `module_precision`) matched against `TaskProfile` (`divergence`, `context_scale`, `code_complexity`):
    - **`gemini-4-argon-high`** (`divergence: 0.96`, `global_rigor: 0.80`, `module_precision: 0.80`): Strongest divergent thinking, expansive ideation, and architectural innovation; wins scheme formulation and creative research (`divergence = 3`).
    - **`claude-opus-5-5`** (`divergence: 0.62`, `global_rigor: 0.98`, `module_precision: 0.92`): Strongest architectural landing rigor and holistic cross-module completeness; wins convergent cross-cutting architecture and repo-wide synthesis (`code_complexity = 3` or `context_scale = 3`, `divergence <= 2`).
    - **`gpt-6.1-sol`** (`divergence: 0.40`, `global_rigor: 0.25`, `module_precision: 0.95`): High precision on bounded single-module implementation (`context_scale = 1`, `code_complexity in 1..2`, `divergence = 1`), penalized on global-vision (`context_scale = 3`) tasks.


## 5. Installed Frontier Catalog Snapshot (`2026-10-27`)

Discovery used exact local catalog IDs, rather than extrapolating model generations:

- `pi --list-models`: eight `openai-codex` models, including `gpt-6.1-terra` and `gpt-6.1-sol` at 500K context, plus GPT-6 and GPT-5.6 Sol/Terra/Luna variants at 272K. The installed package is `@earendil-works/pi-coding-agent`; its bundled `@earendil-works/pi-ai/dist/models.generated.js` was also inspected. The live CLI inventory is authoritative for this signed-in provider.
- `opencode models`: `deepseek-v4.1-flash-free`, `glm-5.3-flash-free`, `mimo-v2.6-flash-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, `nemotron-3.5-super-vl-free`, `beam-free`, `flint-40b-free` (all under `opencode/`). `big-pickle` is also installed but is not a current identified vendor frontier model.
- `~/.cache/opencode/models.json`: broader provider catalog, distinct from authenticated launch availability. Verified Anthropic 5.5 / Fable 5.1, OpenAI 6.1 Terra, Google Argon 4 / Flash 3.8, xAI 4.7, DeepSeek `deepseek-v4-pro`, Zhipu `glm-5.3`, Qwen `qwen3.8-max` and `openrouter/qwen/qwen3-coder-next`, Moonshot `kimi-k3.1-pro` (2026-10-23), MiniMax `MiniMax-M3` and `MiniMax-M3.1-Flash` (2026-10-19), Volcengine `doubao-seed-2-1-pro-260628`, Xiaomi `mimo-v2.6-pro`, NVIDIA Nemotron 3 / 3.5, Mistral `mistral-large-4` (2026-10-06), and Meta `muse-wonder-1.0` (2026-10-12).
- `agy models`: `gemini-4-argon-{low,medium,high}`, `gemini-3.8-flash-{low,medium,high}`, plus Claude 5.5 variants. `~/.grok/models_cache.json` advertises `grok-4.7` and `grok-4.7-build-fast`. `~/.route-manager/usage-stats.json`, `provider-inventory.json` and `~/.paseo/config.json` corroborate the active launchers and calibrated models.
- A read-only query of `~/.cc-switch/cc-switch.db` confirms `cc-switch:claude:default` advertises `claude-haiku-4-5@20251001`, `claude-opus-4-6`, `claude-sonnet-4-6`. These three rows remain compatibility priors; new Claude family defaults resolve to 5.5. GPT-5.6 rows likewise retain their own scores because Pi still advertises them.

Catalog gaps: no full `deepseek-v4.1` or `deepseek-r2`, `gemini-3.8-pro`, or `grok-4.7-mini` / `grok-4.7-fast` appeared in the inspected catalogs. Defaults therefore use the verified DeepSeek V4 Pro, Gemini 4 Argon and Grok 4.7 Build Fast IDs. Qwen Coder Next is the catalog's current dedicated coding line; inventing a `qwen3.5-coder` ID would be incorrect. Meta's current frontier catalog uses Muse; no new Llama generation was found. Unknown `^llama` names consequently receive the generic conservative baseline (tier 3.3), not a Muse alias or the removed Llama 4 Maverick prior.

All family normalization changes **scoring only**. Provider discovery retains the exact advertised `launch_model`, including casing, namespaces and dated IDs. Catalog presence does not enable accounts, create subscriptions or prove authenticated access. Free Flash aliases describe scoring equivalence; actual paid/free launch IDs remain unchanged.

The core calibrated vectors and effort curves remain authoritative. Fable's tier is corrected to the requested 4.6 and remains paused. New entries are **provisional routing priors**, not benchmark measurements; hourly costs are routing estimates, not live API price quotes. New/default-only effort curves claim no measured effort uplift. `accounts.json` and `model-capabilities.json` contain the same complete set of scored models and vectors.

### Complete Score Snapshot

`R/C/E/A/V/G` = reasoning / coding / exploration / anti-hallucination / visual / GUI. `D/R/P` = divergence / global rigor / module precision. “Core” retains existing calibration, “prior” is a new catalog-based estimate, and “compat” denotes the three required CC Switch rows.

| Model | Tier | R/C/E/A/V/G | D/R/P | Estimated $/h | Basis |
| :--- | ---: | :--- | :--- | ---: | :--- |
| `deepseek-v4.1-flash-free` | 2.8 | 0.48 / 0.50 / 0.50 / 0.45 / 0.00 / 0.00 | 0.45 / 0.35 / 0.55 | 0.0 | core |
| `claude-fable-5-1` | 4.6 | 0.98 / 0.95 / 0.90 / 0.90 / 0.00 / 0.00 | 0.92 / 0.95 / 0.90 | 18.7 | core |
| `claude-opus-5-5` | 4.6 | 0.89 / 0.91 / 0.88 / 0.92 / 1.00 / 0.55 | 0.62 / 0.98 / 0.92 | 7.5 | core |
| `gpt-6.1-sol` | 3.6 | 0.71 / 0.85 / 0.62 / 0.74 / 0.70 / 0.82 | 0.40 / 0.25 / 0.95 | 2.6 | core |
| `gpt-6-astra` | 4.2 | 0.80 / 0.79 / 0.80 / 0.78 / 0.80 / 0.95 | 0.68 / 0.82 / 0.84 | 22.6 | core |
| `gemini-4-argon-high` | 4.0 | 0.78 / 0.83 / 0.86 / 0.81 / 0.75 / 0.62 | 0.96 / 0.80 / 0.80 | 4.0 | core |
| `claude-sonnet-5-5` | 3.9 | 0.74 / 0.82 / 0.89 / 0.73 / 0.60 / 0.55 | 0.60 / 0.76 / 0.78 | 4.4 | core |
| `grok-4.7` | 3.4 | 0.59 / 0.58 / 0.60 / 0.62 / 0.00 / 0.00 | 0.45 / 0.40 / 0.58 | 1.5 | core |
| `gemini-3.8-flash-high` | 3.0 | 0.52 / 0.47 / 0.50 / 0.72 / 0.30 / 0.42 | 0.35 / 0.35 / 0.65 | 0.5 | core |
| `dot` | 4.2 | 0.80 / 0.79 / 0.80 / 0.78 / 0.00 / 0.00 | 0.68 / 0.82 / 0.84 | bridge / free | core |
| `grok-bot` | 3.3 | 0.55 / 0.55 / 0.55 / 0.50 / 0.00 / 0.00 | 0.50 / 0.50 / 0.50 | unknown | provisional service-level text baseline; not a benchmark |
| `gpt-6-pro` | 5.0 | 1.00 / 0.90 / 0.78 / 0.95 / 0.00 / 0.00 | 0.95 / 0.96 / 0.90 | bridge / free | core |
| `claude-haiku-5-5` | 3.2 | 0.58 / 0.65 / 0.62 / 0.62 / 0.48 / 0.35 | 0.42 / 0.53 / 0.67 | 0.8 | prior |
| `gpt-6.1-terra` | 3.7 | 0.73 / 0.83 / 0.71 / 0.75 / 0.72 / 0.80 | 0.50 / 0.65 / 0.88 | 3.0 | prior |
| `gpt-6-sol` | 3.5 | 0.69 / 0.82 / 0.60 / 0.72 / 0.68 / 0.80 | 0.40 / 0.25 / 0.92 | 2.6 | prior |
| `gpt-6-luna` | 3.2 | 0.58 / 0.65 / 0.57 / 0.62 / 0.55 / 0.50 | 0.40 / 0.45 / 0.68 | 0.8 | prior |
| `gpt-5.6-sol` | 3.4 | 0.67 / 0.79 / 0.58 / 0.70 / 0.65 / 0.75 | 0.40 / 0.25 / 0.88 | 2.4 | prior |
| `gpt-5.6-terra` | 3.5 | 0.69 / 0.78 / 0.66 / 0.71 / 0.67 / 0.74 | 0.48 / 0.60 / 0.83 | 2.7 | prior |
| `gpt-5.6-luna` | 3.1 | 0.55 / 0.62 / 0.54 / 0.59 / 0.50 / 0.45 | 0.38 / 0.42 / 0.65 | 0.7 | prior |
| `grok-4.7-build-fast` | 3.1 | 0.54 / 0.58 / 0.54 / 0.55 / 0.00 / 0.00 | 0.55 / 0.45 / 0.60 | 0.5 | prior |
| `deepseek-v4-pro` | 3.9 | 0.78 / 0.82 / 0.72 / 0.75 / 0.00 / 0.00 | 0.62 / 0.74 / 0.84 | 1.2 | prior |
| `glm-5.3` | 3.7 | 0.72 / 0.78 / 0.74 / 0.72 / 0.60 / 0.55 | 0.60 / 0.72 / 0.80 | 1.0 | prior |
| `glm-5.3-flash-free` | 2.9 | 0.50 / 0.54 / 0.52 / 0.52 / 0.40 / 0.30 | 0.45 / 0.42 / 0.58 | 0 | prior |
| `qwen3-coder-next` | 3.7 | 0.70 / 0.82 / 0.65 / 0.66 / 0.00 / 0.00 | 0.48 / 0.67 / 0.84 | 1.0 | prior |
| `qwen3.8-max` | 3.9 | 0.78 / 0.80 / 0.75 / 0.74 / 0.65 / 0.48 | 0.67 / 0.75 / 0.80 | 1.5 | prior |
| `kimi-k3.1-pro` | 4.0 | 0.79 / 0.82 / 0.83 / 0.74 / 0.68 / 0.50 | 0.72 / 0.76 / 0.81 | 1.8 | prior |
| `kimi-k3` | 3.8 | 0.74 / 0.79 / 0.80 / 0.70 / 0.62 / 0.45 | 0.70 / 0.71 / 0.78 | 1.2 | prior |
| `minimax-m3` | 3.8 | 0.73 / 0.81 / 0.75 / 0.70 / 0.60 / 0.45 | 0.63 / 0.70 / 0.82 | 1.0 | prior |
| `minimax-m3.1-flash` | 3.5 | 0.66 / 0.74 / 0.68 / 0.65 / 0.55 / 0.40 | 0.58 / 0.62 / 0.76 | 0.6 | prior |
| `doubao-seed-2-1-pro-260628` | 3.7 | 0.72 / 0.74 / 0.74 / 0.70 / 0.65 / 0.50 | 0.66 / 0.68 / 0.74 | 0.8 | prior |
| `mimo-v2.6-pro` | 3.7 | 0.72 / 0.79 / 0.70 / 0.69 / 0.60 / 0.45 | 0.57 / 0.67 / 0.80 | 0.8 | prior |
| `mimo-v2.6-flash-free` | 2.9 | 0.51 / 0.55 / 0.51 / 0.50 / 0.40 / 0.30 | 0.45 / 0.42 / 0.58 | 0 | prior |
| `nemotron-3-ultra-free` | 3.4 | 0.65 / 0.70 / 0.65 / 0.63 / 0.00 / 0.00 | 0.53 / 0.62 / 0.71 | 0 | prior |
| `nemotron-3.5-super-vl-free` | 3.3 | 0.62 / 0.65 / 0.63 / 0.62 / 0.65 / 0.45 | 0.52 / 0.59 / 0.66 | 0 | prior |
| `nemotron-3.5-lightning-free` | 2.9 | 0.50 / 0.54 / 0.50 / 0.48 / 0.00 / 0.00 | 0.42 / 0.42 / 0.56 | 0 | prior |
| `mistral-large-4` | 3.8 | 0.74 / 0.78 / 0.73 / 0.71 / 0.65 / 0.45 | 0.65 / 0.71 / 0.78 | 1.2 | prior |
| `muse-wonder-1.0` | 3.9 | 0.77 / 0.77 / 0.79 / 0.73 / 0.72 / 0.48 | 0.71 / 0.73 / 0.77 | 1.5 | prior |
| `beam-free` | 3.0 | 0.53 / 0.56 / 0.53 / 0.50 / 0.00 / 0.00 | 0.45 / 0.45 / 0.58 | 0 | prior |
| `flint-40b-free` | 2.8 | 0.48 / 0.52 / 0.48 / 0.46 / 0.00 / 0.00 | 0.40 / 0.40 / 0.54 | 0 | prior |
| `claude-haiku-4-5` | 3.2 | 0.55 / 0.61 / 0.59 / 0.57 / 0.45 / 0.32 | 0.42 / 0.50 / 0.64 | 0.8 | compat |
| `claude-opus-4-6` | 4.4 | 0.85 / 0.87 / 0.84 / 0.88 / 0.95 / 0.52 | 0.60 / 0.93 / 0.88 | 7.5 | compat |
| `claude-sonnet-4-6` | 3.8 | 0.71 / 0.79 / 0.83 / 0.70 / 0.60 / 0.50 | 0.58 / 0.73 / 0.76 | 3.5 | compat |
