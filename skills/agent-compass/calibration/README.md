# Calibration: refitting model rows, floors and effort curves

These scripts refit the router's measured data from public boards. They are maintainer tools: the router never runs
them, and they need `numpy`, `pandas` and `pyarrow`. Raw downloads stay out of git, in
`~/.cache/agentcompass-calibration` (or `AGENTCOMPASS_CALIBRATION_DATA`, or `--data DIR`).

| Script | What it does |
| :--- | :--- |
| `sources.py fetch` | Downloads LMArena (Hugging Face `lmarena-ai/leaderboard-dataset`, latest split), vals.ai SWE-bench Verified and Terminal-Bench 2.0, the Artificial Analysis models leaderboard, the five-axis boards and the community capacity pages (`community.py`). `--only FILE` refetches single files and keeps the other manifest rows. |
| `tiers.py [ids]` | Fits tiers onto the scale set by the six anchor rows. Primary: LMArena WebDev + Agent. Secondary, for models on neither board: SWE-bench, Terminal-Bench and the AA index, regressed on tier. The anchors are inputs, never outputs. |
| `apply_rows.py [ids]` | Writes measured rows into `references/accounts.json` and the reference fixture. A tier measured at a best variant such as "(Max)" is moved to High with the AA effort curves. A model without a tier measurement takes its nearest same-series sibling's tier, else `unmeasured_models.generic`. Then every row's text capabilities (visual and gui included), subjective scores and usd_per_hour without a direct measurement are filled for every model alike: the series conversion from the nearest same-series sibling with a direct value (`series.py`: the router's family resolution plus `superseded`; nearest on the AA index, else on the mean difference over the cited five-axis boards both have, tier only when the two share no reading), moved along the calibration's fitted slope by a cited reading both have (the score's own AA board, else the AA index, unclamped) and kept inside the fit's measured range, else the sibling's value; usd_per_hour = the sibling's x the API list price ratio over the community token mix. Only a model with no sibling gets a public-data estimate, from Artificial Analysis readings where the fit shows out-of-sample skill (leave-one-out errors beating both the sample mean and the default rows by more than one standard error of the paired difference; the skilled candidate with the lowest leave-one-out error; a reading outside the sample clamped). Recorded in the row's `score_estimates`; a value this step did not write (edited or measured) wins; `refresh.py` runs this step. `--estimates-only` re-estimates without refitting tiers. |
| `efforts.py [--apply]` | Builds effort curves from the AA effort variants: k tier per index point from the AA-to-tier slope, plus burn from cost per task. |
| `floors.py` | Refits the ability floors (90% of the best model's success, with bootstrap bands), checks the frontier evidence, and reports where public boards disagree with the anchors. |
| `measurements.py` | Writes `references/public-measurements.json`: per model, field and source, every public value with its provenance (source, kind vendor / benchmark / community, as_of, fetched_at, url, method). Fields: tier fits (kept for comparison), usd_per_hour from public sources only (AA cost per task / time, and the same repriced at the OpenRouter output price; shipped local samples and local burn figures are not sources), prices, and the four capability axes of the tray radar, each source relative to the anchor model Opus 5.5 (0.8 x s / s_anchor, Elo boards as twice the win rate against the anchor; a board without the anchor is bridged through anchored boards of the same component or axis, else not used), so a new top model moves no other value. The local `visual` / `text_only` flags are not a source; text-only needs a cited vendor statement (`text_only_source`). The old tier signal lives in the axes: LMArena WebDev in module_precision.coding, Agent in divergence.exploration, Text overall in global_rigor.reasoning. A source that was not fetched or does not list a model adds nothing. |
| `five_axis.py [--distrust SOURCE] [--source-priority SOURCE] [--user-overrides FILE]` | Writes `references/five-axis-scores.json`: axis scores (components averaged; a counted component a model does not measure converted from its nearest same-series sibling that does (the sibling's value moved along the component's AA-index fit, `converted`), and only for a model with no such sibling backfilled from the AA index (the backfill cites its fit, r, n and input date; a reading above or below the sampled ones is clamped to them, `x_clamped`) when the fit has at least 8 measured points and a positive slope, kept inside the range the measured points reach (round 6; the folded-away tier is no predictor), else left missing and counted as 0 on a partial axis (round 8 pessimistic unknowns); an axis with nothing = 0 and a rule-B data error), LMArena components corrected to High with the AA effort ratio, vendor-statement inheritance (cited), per-model `evidence` (non-AA sources per axis; `aa_only` plus a yellow data error when every axis comes from Artificial Analysis alone), merged usd_per_hour (median over the cited sources, or the first trusted source in `--source-priority`), duration multiplier (1.0 when a source includes time), plan capacity per plan and window down a cited ladder, recording the rung (1 community real-usage measurements: median of the cited values, or a stated ratio such as Fable weekly = half the weekly limit; 2 community time-to-exhaust reports x the reported model's usd; 3 a vendor plan multiplier x the sibling plan, also kept as a cross-check; all empty = a red data error surfaced in routing notes, never a template fallback; a source older than 21 days = yellow), source ages, the community usd-per-hour check rows against the usd basis (`usd_checks`), the usd basis (`--usd-basis community_mix` default: arena real sessions, else the series conversion from a same-series sibling's direct value x the API list price ratio, else option B: AA output speed priced at the community token mix wokey values the capacities at; `benchmark` = AA/OpenRouter for comparison; `community` = measured, else derived from a same-provider measured model x the benchmark ratio), and `anchor` {model, value} (the router's fit targets are the anchor value x demand / 3; round 7 removed the difficulty floors). `--distrust` drops a source from every field. Read by the router only when `scoring.five_axis.enabled` is set (off by default). |
| `arena_capture.py import CAPTURE.json ...` / `show` | Imports the arena.ai boards the user captures from the browser (arena.ai is behind bot protection, so they are not fetched): Agent categories (Overall / Code / Chat / Work) and the Agent Alignment Index, plus WebDev and Text as a cross-check of the Hugging Face dataset. Each page must parse whole; the page date is the data date; written into the data directory with a manifest row (`transcribed_by`, `kind: transcribed`) that `sources.py fetch` keeps. The page text drops minus signs: Net Improvement signs are rebuilt from the rank order (checked against the dataset's signed Agent / Overall). Mapping in `measurements.py` (`ARENA_CAPTURES`); a tab never captured is a missing source. `show` lists what is imported. |
| `community.py` | Reads community real-usage measurements of subscription window capacity (wokey.ai, finopsllm.com, inventivehq.com, the eneasf Antigravity telemetry findings) out of the fetched pages, plus the community token mix, the vendor plan multipliers (Google Ultra 5x / 20x Pro, Anthropic Max 5x Pro, OpenAI Pro $100 5x Plus) and the community usd-per-hour check rows, each value with its sentence, data date and fetch time; a page not fetched or no longer carrying its sentence gives an error and no value. Lists what has no direct community measurement (`NOT_FOUND`) and the time-to-exhaust reports set aside (`EXHAUST_REJECTED`). |
| `refresh.py [--no-fetch] [--report FILE]` | Keeps the data current: fetch, `measurements.py`, `five_axis.py`, then the community $/h ÷ usd basis check rows and a source-age table; exit 0 fresh, 1 stale, 2 missing. Meant for a weekly schedule; installs nothing. |
| `five_axis_tasks.py` | Synthetic task profiles with hand-labelled `axis_demand`, used by `audit/five_axis_parity.py` and `audit/five_axis_pacing_sim.py`. |

## Refit

```
python -X utf8 sources.py fetch
python -X utf8 floors.py                      # floors stay unless a band excludes the shipped value
python -X utf8 tiers.py                       # inspect fits
python -X utf8 apply_rows.py                  # rows in tiers.NAMES, then the estimates
python -X utf8 apply_rows.py --estimates-only # estimates only, on the rows as they are
python -X utf8 efforts.py --apply             # curves for efforts.AA_NAMES
python -X utf8 ../scripts/capability_catalog.py
python -X utf8 ../scripts/public_data.py manifest --write
```

Five-axis data (shadow path, round 047; `sources.py fetch` also writes `fetch-manifest.json` with each file's url,
fetch time and sha256, and records a source it could not fetch as missing):

```
python -X utf8 sources.py fetch
python -X utf8 measurements.py
python -X utf8 arena_capture.py import <capture>.json ...   # arena.ai boards from the user's browser, when refreshed
python -X utf8 five_axis.py                   # --distrust <source id> leaves a source out
python -X utf8 refresh.py                     # the three steps above plus the source-age report
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
