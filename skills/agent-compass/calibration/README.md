# Calibration: the data the router scores with

These scripts rebuild the router's measured data from public, cited boards. They are maintainer tools: the router never runs
them, and they need `numpy`, `pandas` and `pyarrow`. Raw downloads stay out of git, in
`~/.cache/agentcompass-calibration` (or `AGENTCOMPASS_CALIBRATION_DATA`, or `--data DIR`).

## The scoring these files feed

The router scores a model on **four categories** (divergence, global rigor, module precision, vision & tools) and on
**quota economy**. Nothing else scores a model.

- Every board gives a **type score** (a component such as `module_precision.coding`). Each type score belongs to one of the
  four categories; a category is the mean of its type scores. Every source is converted the same way, relative to the anchor
  model Opus 5.5 (`0.8 x s / s_anchor`), so a new top model moves no other value.
- The **tier** is the model's overall level: its position on the arena.ai Overall board (`lmarena.text_overall`), mapped
  onto the user's tier scale through six anchor rows (Opus 5.5 4.6, Astra 4.2, Sonnet 5.5 3.9, Sol 3.6, Grok 4.7 3.4,
  Flash 3.0; the anchors are the user's judgment, inputs and never outputs). A model arena Overall does not list takes its
  relative position on a non-arena overall board (the Artificial Analysis Intelligence Index, interpolated through the models
  both list); a model on no overall board takes its nearest same-series sibling's tier; with none it has no tier.
- The tier has **one use**: a type score a model lacks is estimated. With a same-series sibling that measures it, the estimate
  is the sibling's value moved along the fitted line of that type score on tier by the two tiers' difference; without one, the
  fit at the model's tier. A fit must have out-of-sample skill and enough points, and its value stays inside the measured
  range. The record keeps the fit, the tier and its source, the base and the shift, and the score is marked `estimated`
  (`~` in the catalog and the tray). A direct measurement replaces the estimate at the next refresh. With no tier or no fit
  with skill the type score stays missing. Where the fit has no skill the tier does not predict that type score, so the shift is
  0 and the sibling's value stands (`shift 0: fit rejected, ...`). A category with some type scores missing is yellow (the missing
  ones count as 0, the model stays in routing); a category with none leaves automatic routing for a task that demands it (an
  explicit `--model` still dispatches it). No default value stands in.
- A row `accounts.json` `same_model` declares to be the model another row names (a vendor statement with its url, or a dated
  user statement, kind `user`; today Dot is GPT-6 Astra, OpenAI's launch post, and grok-bot is Claude Opus 5.5, the user
  2026-11-11) takes that model's type scores as they stand, measured or estimated, for the categories the row `carries`, and
  its Time/Task; the channel's own categories (vision and GUI of both text-only channels) stay missing.

## Scripts

| Script | What it does |
| :--- | :--- |
| `sources.py fetch` | Downloads LMArena (Hugging Face `lmarena-ai/leaderboard-dataset`), vals.ai, the Artificial Analysis models leaderboard, the category boards and the community capacity pages (`community.py`), and writes `fetch-manifest.json` (url, fetch time, sha256 per file). A source it cannot fetch is recorded missing. `--only FILE` refetches single files. |
| `tiers.py [ids]` | Reads each model's tier off the arena.ai Overall board through the six anchor rows; models that board does not list get the non-arena overall board reading (`tier.aa_index`). Prints the readings with their neighbours and overlap. |
| `series.py` | Library: same-series siblings (the router's own family resolution plus the public data's `superseded` pairs) and the base a converted model takes: nearest on the AA Intelligence Index, else on the shared cited board readings, else on tier. |
| `efforts.py [--apply]` | Effort curves from the AA effort variants: `ability_mult` (index at the effort over the index at High). The router scales a model's thinking categories by `ability_mult`. The time and spend of an effort come from `five_axis.py` (below), not from a curve. |
| `measurements.py` | Writes `references/public-measurements.json`: per model, field and source, every public value with its provenance (source, kind vendor / benchmark / community, as_of, fetched_at, url, method): the type scores, usd_per_hour (arena.agent_sessions, derived.community_mix), prices, image input, fast-mode pricing. Arena.ai captures enter through `arena_capture.py`. |
| `five_axis.py [--distrust SOURCE] [--user-overrides FILE]` | Writes `references/five-axis-scores.json`: type scores and categories per model (measured or estimated), the tier and its source, the fitted lines (`estimate_fits`, with the fits it rejected and why), usd_per_hour (arena.ai Agent sessions; else the labelled option B, AA throughput at the community token mix and list prices; else a same-series sibling's converted), the duration (`duration_multiplier`: per effort the Artificial Analysis Time/Task as a multiple of the anchor's at High, with each effort's basis; an arena.ai Agent time moved onto that scale by a fit with its r and n, a series conversion of the model's time or effort ratio, the fleet's median effort position or ratio with its n and IQR, or a `same_model` row's declared model), plan capacity per plan and window down a cited ladder, per-model `evidence` and data errors, and the anchor the router's fit targets use (`anchor value x demand / 3`). The only model scoring the router reads. |
| `arena_capture.py import CAPTURE.json ...` / `show` | Imports the arena.ai boards the user captures from the browser (arena.ai is behind bot protection, so they are not fetched). Each page must parse whole; the page date is the data date; each import writes a manifest row that `sources.py fetch` keeps. |
| `community.py` | Reads community real-usage measurements of subscription window capacity (wokey.ai, finopsllm.com, inventivehq.com, the eneasf Antigravity telemetry findings), the community token mix, the vendor plan multipliers and the community usd-per-hour check rows, each value with its sentence, data date and fetch time. A page not fetched or no longer carrying its sentence gives an error and no value. |
| `refresh.py [--no-fetch] [--report FILE]` | Keeps the data current: fetch, `measurements.py`, `five_axis.py`, then the source-age report. Exit 0 fresh, 1 stale, 2 missing. Meant for a weekly schedule; installs nothing. |

## Refresh

```
python -X utf8 refresh.py                     # fetch, measurements, five-axis scores, source-age report
python -X utf8 efforts.py --apply             # effort curves for efforts.AA_NAMES
python -X utf8 ../scripts/capability_catalog.py
python -X utf8 ../scripts/public_data.py manifest --write
```

Step by step: `sources.py fetch`, `measurements.py`, `arena_capture.py import <capture>.json ...` (when the arena.ai boards
are refreshed), `five_axis.py` (`--distrust <source id>` leaves a source out), then the last two lines above.

To add a model, put its source names in `tiers.NAMES` (and `efforts.AA_NAMES` when the Artificial Analysis lists its effort
variants), then refresh. Every written value names its sources and dates.

## Rules the data follows

- The anchor tiers are the user's judgment. The scripts report contradictions with them and never change them.
- Public cited data only. A value without a trusted source is missing, and missing is a surfaced data error, never a default.
- No model names in router code. Everything above is data under `references/`.
