# Calibration: refitting model rows, floors and effort curves

These scripts refit the router's measured data from public boards. They are maintainer tools: the router never runs
them, and they need `numpy`, `pandas` and `pyarrow`. Raw downloads stay out of git, in
`~/.cache/agentcompass-calibration` (or `AGENTCOMPASS_CALIBRATION_DATA`, or `--data DIR`).

| Script | What it does |
| :--- | :--- |
| `sources.py fetch` | Downloads LMArena (Hugging Face `lmarena-ai/leaderboard-dataset`, latest split), vals.ai SWE-bench Verified and Terminal-Bench 2.0, and the Artificial Analysis models leaderboard. |
| `tiers.py [ids]` | Fits tiers onto the scale set by the six anchor rows. Primary: LMArena WebDev + Agent. Secondary, for models on neither board: SWE-bench, Terminal-Bench and the AA index, regressed on tier. The anchors are inputs, never outputs. |
| `apply_rows.py [ids]` | Writes measured rows into `references/accounts.json` and the reference fixture. A tier measured at a best variant such as "(Max)" is moved to High with the AA effort curves. Unmeasured models get `unmeasured_models.generic`. |
| `efforts.py [--apply]` | Builds effort curves from the AA effort variants: k tier per index point from the AA-to-tier slope, plus burn from cost per task. |
| `floors.py` | Refits the ability floors (90% of the best model's success, with bootstrap bands), checks the frontier evidence, and reports where public boards disagree with the anchors. |
| `measurements.py` | Writes `references/public-measurements.json`: per model, field and source, every public value with its provenance (source, kind vendor / benchmark / community, as_of, fetched_at, url, method). Fields: tier fits (kept for comparison), usd_per_hour from public sources only (AA cost per task / time, and the same repriced at the OpenRouter output price; shipped local samples and local burn figures are not sources), prices, and the four capability axes of the tray radar, each source relative to the anchor model Opus 5.5 (0.8 x s / s_anchor, Elo boards as twice the win rate against the anchor; a board without the anchor is bridged through anchored boards of the same component or axis, else not used), so a new top model moves no other value. The local `visual` / `text_only` flags are not a source; text-only needs a cited vendor statement (`text_only_source`). The old tier signal lives in the axes: LMArena WebDev in module_precision.coding, Agent in divergence.exploration, Text overall in global_rigor.reasoning. A source that was not fetched or does not list a model adds nothing. |
| `five_axis.py [--distrust SOURCE] [--source-priority SOURCE] [--user-overrides FILE]` | Writes `references/five-axis-scores.json`: axis scores (components averaged; a missing component backfilled from the AA index, else the public tier fit, when the fit has at least 8 measured points and a positive slope, else left missing as a yellow note; an axis with nothing = 0 and a rule-B data error), LMArena components corrected to High with the AA effort ratio, vendor-statement inheritance (cited), merged usd_per_hour (median over the cited sources, or the first trusted source in `--source-priority`), duration multiplier (1.0 when a source includes time), plan capacity (packaged list-price USD, with its hours of the reference model at the cited usd), and the difficulty floors from `five_axis_floors.py`. `--distrust` drops a source from every field. Read by the router only when `scoring.five_axis.enabled` is set (off by default). |
| `five_axis_floors.py` | Calibrates the five-axis difficulty floors as shares of the anchor model's demand-weighted level (values are anchor-relative at the source, so a new top model moves no floor), monotone in difficulty and critical >= non-critical, plus a per-axis floor on strongly demanded axes, so the qualifying set per difficulty stays close to today's tier gate on the classifier calibration cases and `five_axis_tasks.py`. Reports the agreement and every model that enters or leaves a band. |
| `five_axis_tasks.py` | Synthetic task profiles with hand-labelled `axis_demand`, shared by the floor calibration and `audit/five_axis_parity.py`. |

## Refit

```
python -X utf8 sources.py fetch
python -X utf8 floors.py                      # floors stay unless a band excludes the shipped value
python -X utf8 tiers.py                       # inspect fits
python -X utf8 apply_rows.py                  # rows in tiers.NAMES
python -X utf8 efforts.py --apply             # curves for efforts.AA_NAMES
python -X utf8 ../scripts/capability_catalog.py
python -X utf8 ../scripts/public_data.py manifest --write
```

Five-axis data (shadow path, round 047; `sources.py fetch` also writes `fetch-manifest.json` with each file's url,
fetch time and sha256, and records a source it could not fetch as missing):

```
python -X utf8 sources.py fetch
python -X utf8 measurements.py
python -X utf8 five_axis.py                   # --distrust <source id> leaves a source out; floors recalibrate here
python -X utf8 ../../../audit/five_axis_parity.py --md parity.md
```

To add a model, put its source names in `tiers.NAMES`. Add it to `efforts.AA_NAMES` too if AA lists its effort
variants. Then rerun `apply_rows.py` and `efforts.py --apply`. Every written row and curve names its sources and dates in
`basis` / `calibration_source`. Floors, the score origin and the unmeasured default carry their reasoning in
`scoring.*_basis` and `unmeasured_models.basis`.

## Rules the data follows

- The anchor tiers (Opus 5.5 4.6, Astra 4.2, Sonnet 5.5 3.9, Sol 3.6, Grok 4.7 3.4, Flash 3.0) are the user's judgment.
  The scripts report contradictions with them and never change them.
- A floor moves only when its bootstrap band excludes the shipped value. Frontier is the lowest top anchor tier a vendor
  subscription ships (Astra 4.2): no benchmark measures frontier work, and the user wants every vendor's top model to
  qualify. The critical frontier gate stays at 4.9 through `critical_headroom_by_difficulty`.
- No model names in router code. Everything above is data in `accounts.json`.
