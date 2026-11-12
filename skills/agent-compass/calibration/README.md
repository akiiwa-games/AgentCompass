# Calibration: the data the router scores with

These scripts rebuild the router's measured data from public, cited boards. They are maintainer tools: the router never runs
them, and they need `numpy`, `pandas` and `pyarrow`. Raw downloads stay out of git, in
`~/.cache/agentcompass-calibration` (or `AGENTCOMPASS_CALIBRATION_DATA`, or `--data DIR`).

## The scoring these files feed

The router scores a model on **four capability categories** (`divergence`, `coding`, `aesthetics`, `tool_operation`) and on
**quota economy**. Nothing else scores a model.

- **Categories and facets.**
  - `divergence` (`creative_writing`: `lmarena.creative_writing`) — creative writing, brainstorming, research synthesis, data analysis, and first-principles problem decomposition.
  - `coding` = `min(execution, faithfulness)` (`execution`: `arena.agent_code`, `aa.terminalbench_4`, `aa.frontiercode_2`; `faithfulness`: `arena.agent_alignment`) — code execution, review/audit, and faithful completion without unrequested edits or deceptive reporting.
  - `aesthetics` (`design`: `lmarena.webdev`, `designarena.website`, `designarena.3d`, `designarena.svg`) — web/GUI design, layout, 3D and SVG visuals.
  - `tool_operation` — evaluated per demand level: level 1 imposes no ability requirement; level 2 evaluates `responsiveness` (`aa.ttfa`, `-ln(seconds)` per effort); level 3 evaluates `min(responsiveness, image_recognition)` (`aa.mmmu_pro`, `lmarena.vision`) plus image-input modality.
- **Panel standard scores (`z`).** Every scoring board is scaled on the fixed panel of fleet models at High listed on every core board that carries all six anchor rows (`n = 12` today): `z = (x - x_anchor) / sd_panel`, putting `claude-opus-5-5` at High at `0.0` with one unit equal to one panel standard deviation. Adding a new non-panel model moves neither any panel score nor any demand target. Parallel boards of one facet are centered on the anchor at High, mean-sigma linked (`z_linked = scale * z + offset`) onto the full-panel primary board, averaged, and re-centered on the anchor. Non-AA boards without per-effort rows derive effort shifts from the same model's Artificial Analysis multi-effort curve on the most correlated AA board (`effort_correlations` records `(board, r, n)`); models without an AA multi-effort curve stay `measured_partial` at their single listed effort.
- **Panel targets and fit zero point.** Demand targets come from the panel at High (`anchor.targets` in `five-axis-scores.json`): level 1 (`t1`) is the panel minimum, level 2 (`t2`) is the panel median, and level 3 (`t3`) is the anchor (`0.0`, raised to `max(t2, t3)` if `t3 < t2`, as on `tool_operation.responsiveness`). `t1` is the zero point of `axis_scores.five_axis_fit`.
- **Tier, facet estimation, and manual scores.** The **tier** is read only from the `arena.ai` Overall board (`lmarena.text_overall`) through the six anchor rows (else `tier.aa_index`, else nearest same-series sibling). The tier is never a score input; its sole capability use is estimating a facet a model has no direct measurement for: same-series sibling shifted along `facet~tier` by `b * delta_tier`, else the `facet~tier` line at the model's tier (clamped to the measured range), recording `r`, `skill`, `loo_rmse` and `n` without gating on `skill`, marked `estimated` (`~` in the catalog and tray), and replaced automatically when a direct board measurement arrives. When a required facet has no measurement, no estimate and no manual score, `coding` never skips it (`value: null`, Mode B excluded whenever `coding >= 1`), and `tool_operation` excludes the model at the demand levels that require that facet. Maintainer (`references/user-scores.json`, `kind: "maintainer"`) and local user (`~/.agent-compass/user-scores.json`, `kind: "user"`) manual scores on the 1–3 demand-level scale map dynamically onto `z` via `anchor.targets` with precedence `local user > maintainer > measured > estimated`.
- **`same_model` declarations.** A row `accounts.json` `same_model` declares to be another row's underlying model (`dot` -> `gpt-6-astra`, `grok-bot` -> `claude-opus-5-5`) inherits that model's facet curves, `duration_multiplier`, and `usd_per_hour` when listed in `carries` (`divergence`, `coding`, `aesthetics`, `duration`, `usd_per_hour`), while channel-specific `tool_operation` stays `missing`. When a `same_model` row also declares a `capacity_rule`, `axis_scores.five_axis_overlay` resolves the detected target subscription plan's monthly price from `plan-templates.json` at runtime and matches the same-price (or price-ratio-scaled nearest) reference subscription on the capacity ladder.

## Scripts

| Script | What it does |
| :--- | :--- |
| `sources.py fetch` | Downloads LMArena (`lmarena-ai/leaderboard-dataset`), Design Arena (`website`, `3d`, `svg`), Artificial Analysis (`aa-models.html`), OpenRouter models, and the community capacity pages (`community.py`), writing `fetch-manifest.json` (url, fetch time, sha256 per file). `--only FILE` refetches single files. |
| `tiers.py [ids]` | Reads each model's tier off the `arena.ai` Overall board through the six anchor rows; models that board does not list get `tier.aa_index`. |
| `series.py` | Library: same-series siblings and `same_model` links; picks the nearest base on the AA Intelligence Index, shared board readings, or tier. |
| `measurements.py` | Writes `references/public-measurements.json`: panel standard scores (`z` and AA `z_by_effort`), structural and statistical data-hygiene checks, `usd_per_hour`, list prices, image input, and fast-mode multipliers. |
| `five_axis.py [--distrust SOURCE] [--user-overrides FILE]` | Writes `references/five-axis-scores.json`: per-effort facet curves, parallel-board link offsets, category scores, `estimate_fits` (`r`, `skill`, `loo_rmse`, `n`), `usd_per_hour`, `duration_multiplier`, community plan capacities, panel `anchor.targets` (`levels` 1..3 + `continuity = t3 - t2`), and surfaced data errors. |
| `arena_capture.py import CAPTURE.json ...` / `show` | Imports browser-captured `arena.ai` Agent / Code and Alignment boards and preserves their manifest entries across `sources.py fetch`. |
| `community.py` | Reads community real-usage measurements of subscription window capacity, token mix, vendor plan multipliers, and usd-per-hour check rows. |
| `refresh.py [--no-fetch] [--report FILE] [--distrust SOURCE]` | Single entry point (`agentcompass data refresh [--no-fetch]`): runs fetch (unless `--no-fetch`), `measurements.py`, `five_axis.py`, `capability_catalog.py`, `public_data.py manifest --write`, validates `public-data` checks and `user-scores.json` schema, and prints the source-age report. Exit 0 fresh, 1 stale, 2 missing or invalid. |

## Refresh

```
agentcompass data refresh                     # fetch, rebuild shipped data + views + manifest, validate, source-age report
agentcompass data refresh --no-fetch          # rebuild and validate from cached downloads without fetching
```

Or from this directory: `python -X utf8 refresh.py [--no-fetch] [--data DIR] [--report FILE] [--distrust SOURCE]`.

One command runs the full chain in topological order: `sources.py fetch` (skipped with `--no-fetch`) -> `measurements.py` -> `five_axis.py` (`--distrust <source id>` leaves a source out) -> `capability_catalog.py` -> `public_data.py manifest --write` -> validation (`public-data` checks, `user-scores.json` maintainer schema, and generated capability views) -> source-age report. When refreshing browser-captured `arena.ai` boards, run `python -X utf8 arena_capture.py import <capture>.json ...` first, then `agentcompass data refresh --no-fetch`.

To add a model, put its source names in `tiers.NAMES` (or `measurements.EXTRA_NAMES`), then run `agentcompass data refresh --no-fetch` (or `agentcompass data refresh`). Every written value names its sources and dates.

## Rules the data follows

- The anchor tiers are the user's judgment. The scripts report contradictions with them and never change them.
- Public cited data only. A value without a trusted source is missing, and missing is a surfaced data error, never a default.
- No model names in router code. Everything above is data under `references/`.
