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
- **Panel standard scores (`z`).** Every scoring board is scaled on the fixed panel of fleet models at High listed on every core board that carries all six anchor rows (`n = 12` today): `z = (x - x_anchor) / sd_panel`, putting `claude-opus-5-5` at High at `0.0` with one unit equal to one panel standard deviation. Adding a new non-panel model moves neither any panel score nor any demand target (it can raise a level-3 ceiling). Membership follows that rule, never a hand list; a refresh whose panel members differ from the shipped `five-axis-scores.json` prints the added and removed models with the old and new `sd_panel`, `t1`, `t2` and ceiling per category and facet, and writes nothing unless `--accept-panel-change` is passed, which records the change in `anchor.panel_change`. Parallel boards of one facet are centered on the anchor at High, mean-sigma linked (`z_linked = scale * z + offset`) onto the full-panel primary board, averaged, and re-centered on the anchor. Non-AA boards without per-effort rows derive effort shifts from the same model's Artificial Analysis multi-effort curve on the most correlated AA board (`effort_correlations` records `(board, r, n)`); models without an AA multi-effort curve stay `measured_partial` at their single listed effort.
- **Panel targets and fit zero point.** Demand targets come from the panel at High (`anchor.targets` in `five-axis-scores.json`): level 1 (`t1`) is the panel minimum, level 2 (`t2`) is the panel median, and level 3 (`t3`) is the anchor (`0.0`, raised to `max(t2, t3)` if `t3 < t2`, as on `tool_operation.responsiveness`). `t1` is the zero point of `axis_scores.five_axis_fit`. Each level-3 row also carries its `ceiling` (value, model, status): the highest measured or interpolated High value among all catalogued models (no estimated, series-converted or manual value). Levels 1 and 2 cap the fit at their target; level 3 caps it at the ceiling, and `t3` keeps its other roles (formula in [`docs/technical-reference.md`](../../../docs/technical-reference.md#1-how-scoring--allocation-work)).
- **Tier, facet estimation, and manual scores.** The **tier** is read only from the `arena.ai` Overall board (`lmarena.text_overall`) through the six anchor rows (else `tier.aa_index`, else nearest same-series sibling). The tier is never a score input; its sole capability use is estimating a facet a model has no direct measurement for: same-series sibling shifted along `facet~tier` by `b * delta_tier`, else the `facet~tier` line at the model's tier (clamped to the measured range), recording `r`, `skill`, `loo_rmse` and `n` without gating on `skill`, marked `estimated` (`~` in the catalog and tray), and replaced automatically when a direct board measurement arrives. When a required facet has no measurement, no estimate and no manual score, `coding` never skips it (`value: null`, Mode B excluded whenever `coding >= 1`), and `tool_operation` excludes the model at the demand levels that require that facet. Maintainer (`references/user-scores.json`, `kind: "maintainer"`) and local user (`~/.agent-compass/user-scores.json`, `kind: "user"`) manual scores on the 1–3 demand-level scale map dynamically onto `z` via `anchor.targets` with precedence `local user > maintainer > measured > estimated`.
- **`same_model` declarations.** A row `accounts.json` `same_model` declares to be another row's underlying model (`dot` -> `gpt-6-astra`, `grok-bot` -> `claude-opus-5-5`) inherits that model's facet curves, `duration_multiplier`, and `usd_per_hour` when listed in `carries` (`divergence`, `coding`, `aesthetics`, `duration`, `usd_per_hour`), while channel-specific `tool_operation` stays `missing`. When a `same_model` row also declares a `capacity_rule`, `axis_scores.five_axis_overlay` resolves the detected target subscription plan's monthly price from `plan-templates.json` at runtime and matches the same-price (or price-ratio-scaled nearest) reference subscription on the capacity ladder.

## Scripts

| Script | What it does |
| :--- | :--- |
| `sources.py fetch` | Downloads LMArena (`lmarena-ai/leaderboard-dataset`), Design Arena (`website`, `3d`, `svg`), Artificial Analysis (`aa-models.html`), OpenRouter models, and the community capacity pages (`community.py`), writing `fetch-manifest.json` (url, fetch time, sha256 per file). `--only FILE` refetches single files. |
| `tiers.py [ids]` | Reads each model's tier off the `arena.ai` Overall board through the six anchor rows; models that board does not list get `tier.aa_index`. |
| `series.py` | Library: same-series siblings and `same_model` links; picks the nearest base on the AA Intelligence Index, shared board readings, or tier. |
| `measurements.py` | Writes `references/public-measurements.json`: panel standard scores (`z` and AA `z_by_effort`), structural and statistical data-hygiene checks, `usd_per_hour`, list prices, image input, and fast-mode multipliers. |
| `five_axis.py [--distrust SOURCE] [--accept-panel-change]` | Writes `references/five-axis-scores.json`: per-effort facet curves, parallel-board link offsets, category scores, `estimate_fits` (`r`, `skill`, `loo_rmse`, `n`), `usd_per_hour`, `duration_multiplier`, community plan capacities, panel `anchor.targets` (`levels` 1..3 with the level-3 `ceiling`, `continuity = t3 - t2`), `anchor.sd_panel` and `anchor.panel_change`, and surfaced data errors. Refuses a panel change unless accepted. |
| `arena_capture.py import CAPTURE.json ...` / `show` | Imports browser-captured `arena.ai` Agent / Code and Alignment boards and preserves their manifest entries across `sources.py fetch`. |
| `community.py` | Reads community real-usage measurements of subscription window capacity, token mix, vendor plan multipliers (each names the windows it covers), and usd-per-hour check rows; `MAINTAINER_READINGS` holds dated maintainer measurements of windows no public source measures, and `UNMEASURED` declares the windows left without any. |
| `refresh.py [--no-fetch] [--report FILE] [--distrust SOURCE] [--accept-panel-change] [--model ID ...]` | Single entry point (`agentcompass data refresh [--no-fetch] [--accept-panel-change]`; `--model ID` merges only that model's rows on the shipped scale, see To add a model): runs fetch (unless `--no-fetch`), `measurements.py`, `five_axis.py`, `capability_catalog.py`, `public_data.py manifest --write`, validates `public-data` checks and `user-scores.json` schema, and prints the source-age report. Exit 0 fresh, 1 stale, 2 missing or invalid, 3 refused with nothing written (a refused panel change or `--model`; the staleness report never exits 3). |

## Refresh

```
agentcompass data refresh                     # fetch, rebuild shipped data + views + manifest, validate, source-age report
agentcompass data refresh --no-fetch          # rebuild and validate from cached downloads without fetching
agentcompass data refresh --no-fetch --accept-panel-change   # write a rebuild whose panel members changed (refused otherwise)
```

Or from this directory: `python -X utf8 refresh.py [--no-fetch] [--data DIR] [--report FILE] [--distrust SOURCE] [--accept-panel-change] [--model ID ...]`.

One command runs the full chain in topological order: `sources.py fetch` (skipped with `--no-fetch`) -> `measurements.py` -> `five_axis.py` (`--distrust <source id>` leaves a source out) -> `capability_catalog.py` -> `public_data.py manifest --write` -> validation (`public-data` checks, `user-scores.json` maintainer schema, and generated capability views) -> source-age report. When refreshing browser-captured `arena.ai` boards, run `python -X utf8 arena_capture.py import <capture>.json ...` first, then `agentcompass data refresh --no-fetch`.

## To add a model or a score

Every per-model fact is reference data under `references/`; adding a model or a score changes no Python and no test.

1. **The row.** In `references/accounts.json`, add `models["<id>"]` (copy a sibling row and edit it): `label` (tray name),
   and when they apply `api_family` (`claude` / `codex`: the native CLI), `context_window`, `image_input_source` or
   `text_only_source` (cited or dated maintainer statement), and `fast_capable: true` only for a verified fast launch
   ([fast-modes.md](../references/fast-modes.md)).
2. **Board names.** `listed_as: {"arena": "...", "aa": "..."}` gives the name arena.ai / LMArena and Artificial Analysis list
   the model under, when the automatic matcher cannot derive it from the id (a dated listing, other wording). Leave out a
   board that lists it under its id or does not list it.
3. **Prices (optional, cited).** `list_price` (`input`, `cache_write_5m`, `cache_write_1h`, `cache_read`, `output`, USD per
   million tokens) with `list_price_source` (`kind: "vendor"`, https `url`, `as_of`) prices local session logs in the tray;
   `vendor_fast_mode` (price and speed multipliers, fast and standard prices, `url`, `pricing_url`, `as_of`, `statement`)
   adds the fast variant's measurement.
4. **Launch and family.** A bundle under `accounts[<account>].bundles` and its efforts under `dispatch_efforts[<harness>]`
   make it launchable (discovery adds auto bundles for what a CLI advertises). When it is the newest model of a family,
   point that family's entry in `families.rows` (ordered `{pattern, models}`; the first matching pattern decides; move
   the declaration's `as_of` to the day of the edit) and the family's short `aliases` at it, and list the older model
   under `superseded`.
5. **Rebuild its row.** Run `agentcompass data refresh --model <id>` (repeatable for several new models). It fetches the
   boards into the data directory, builds the measurements, and writes to `public-measurements.json` only the named
   models' entries, conflicts and checks, every entry the full refresh writes for them: with the panel scale unchanged,
   the model's row and its five-axis block equal those of a full refresh over the same boards. A board z-score whose
   source scale (`sources[<id>].scale`) moved since the shipped fetch is recomputed on the shipped scale,
   `(raw - anchor_raw) / sd_panel` (recorded at 6 significant digits, so such a z can differ in the fourth decimal); the
   `tier` is standardized over the fetched board itself and kept as built. Every other row, the panel and the scales stay
   as shipped. It prints each entry it left out and why (two reasons only: the anchor's reading moved since the shipped
   fetch, or a z-score whose source has no shipped scale) and notes what else it did; a model no board lists, or an entry
   it cannot merge for another reason, stops the run before anything is written (exit 3; a run that writes exits with
   the staleness status 0, 1 or 2). It then validates the catalog
   (`public_data.validate_accounts_provenance`) and rebuilds `five-axis-scores.json`, `model-capabilities.{json,md}` and
   the `public-data.json` manifest. Values fitted over all measured models (the tier fits, the fleet effort ratios) still
   move for other models; the run prints how many moved, by field. A model in `calibration_panel` (or the anchor) is
   refused: it changes the scale, so it needs the full refresh (`agentcompass data refresh`, or `--no-fetch` from cached
   downloads), which rebuilds every row. Commit the changed files under `references/`.

A **maintainer manual score** is `agentcompass score set <model> ... --public`, which writes `references/user-scores.json`
and the manifest; then run the refresh. A **measured score** comes from the boards on the next refresh. The calibration
scale itself (the anchor and the panel's sentinel models) is `calibration_panel` in `accounts.json`, a dated maintainer
declaration; changing it rescales every score. Every written value names its sources and dates.

## Rules the data follows

- The anchor tiers are the user's judgment. The scripts report contradictions with them and never change them.
- Public cited data only. A value without a trusted source is missing, and missing is a surfaced data error, never a default.
- No model names in router code. Everything above is data under `references/`, including each model's board names,
  prices, fast-mode multipliers, family patterns and the calibration panel.
