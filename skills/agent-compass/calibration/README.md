# Calibration: the data the router scores with

These scripts rebuild the router's measured data from public, cited boards. They are maintainer tools: the router never runs
them, and they need `numpy`, `pandas` and `pyarrow` (the `calibration` extra). Raw downloads stay out of git, in
`~/.cache/agentcompass-calibration` (or `AGENTCOMPASS_CALIBRATION_DATA`, or `--data DIR`).

## The scoring these files feed

The router scores a model on **four capability categories** (`creativity`, `coding`, `aesthetics`, `tool_operation`) and on
**quota economy**. Nothing else scores a model.

- **Categories and facets.**
  - `creativity` (`creative_writing`: `lmarena.creative_writing`) — cross-domain association, de-novo concept or mechanism invention, creative writing, and open brainstorming.
  - `coding` = `min(execution, faithfulness)` (`execution`: `arena.agent_code`, `aa.terminalbench_4`, `aa.frontiercode_2`; `faithfulness`: `arena.agent_alignment`) — code execution, review/audit, and faithful completion without unrequested edits or deceptive reporting.
  - `aesthetics` — evaluated per demand level: level 1 evaluates `design` (`lmarena.webdev`, `designarena.website`, `designarena.3d`, `designarena.svg`); levels 2 and 3 evaluate `min(design, image_recognition)` (sharing `tool_operation.image_recognition`) — visual creation and fine-grained visual perception.
  - `tool_operation` — evaluated per demand level: level 1 imposes no ability requirement; level 2 evaluates `responsiveness` (`aa.ttfa`, `-ln(seconds)` per effort); level 3 evaluates `min(responsiveness, image_recognition)` (`aa.mmmu_pro`, `lmarena.vision`) plus image-input modality.
- **Panel standard scores (`z`).** Every scoring board is scaled on the panel at High (their number is `anchor.panel_n` in `five-axis-scores.json`): `z = (x - x_anchor) / sd_panel`, putting `claude-opus-5-5` at High at `0.0` with one unit equal to one panel standard deviation. The panel is declared (owner, 2026-11-18): `calibration_panel.members` in `accounts.json`, a dated maintainer list that only the owner changes; the owner changed it on 2026-11-19 (`muse-spark-1.3` replaces `deepseek-v4.1-flash-free`, which no single snapshot lists on every core board), and the owner's hand-written levels (`references/user-scores.json`) were set against the earlier panel, the members of v0.7.0. The core boards are the scoring boards that list every sentinel row (`calibration_panel.models`); every member must be listed on every core board, and a member one does not list stops the run with an error naming both, never a smaller panel. A new row, even one every board lists, is scored against the panel and never joins it, so it moves neither any panel score nor any demand target (it can raise a level-3 ceiling). Everything that reads the panel reads that list: `sd_panel` and the `MIN_PANEL` source check, the board link, the reference board of the effort shifts, the targets, `effort_steps`. A refresh whose panel members differ from the shipped `five-axis-scores.json` prints the added and removed models with the old and new `sd_panel`, `t1`, `t2` and ceiling per category and facet, and writes nothing unless `--accept-panel-change` is passed, which records the change in `anchor.panel_change`. Parallel boards of one facet are centered on the anchor at High, mean-sigma linked (`z_linked = scale * z + offset`) onto the full-panel primary board, averaged, and re-centered on the anchor. Non-AA boards without per-effort rows derive effort shifts from the same model's Artificial Analysis multi-effort curve on the most correlated AA board (`effort_correlations` records `(board, r, n)`); models without an AA multi-effort curve stay `measured_partial` at their single listed effort.
- **Panel targets and fit zero point.** Demand targets come from the panel at High (`anchor.targets` in `five-axis-scores.json`): level 1 (`t1`) is the panel minimum, level 2 (`t2`) is the panel median, and level 3 (`t3`) is the panel maximum, all measured or interpolated High values of the panel (owner, 2026-11-18; the anchor is the origin of `z`, not a level; a facet whose panel does not give `t1 < t2 < t3` is a build error, and a level-3 row names the panel member that holds the maximum). `t1` is the zero point of `axis_scores.five_axis_fit`. Each level-3 row also carries its `ceiling` (value, model, status): the highest measured or interpolated High value among all catalogued models (no estimated, series-converted or manual value). Levels 1 and 2 cap the fit at their target; level 3 caps it at the ceiling, and `t3` is the radar's 1 and the upper end of `continuity` (formula in [`docs/technical-reference.md`](../../../docs/technical-reference.md#1-how-scoring--allocation-work)).
- **Tier, facet estimation, and manual scores.** The **tier** is read only from the `arena.ai` Overall board (`lmarena.text_overall`) through the six anchor rows (else `tier.aa_index`, else the nearest ability relative). The tier is never a score input; its sole capability use is estimating a facet a model has no direct measurement for: the nearest relatives (`series.nearest`; Relatives, below) shifted along `facet~tier` by `b * delta_tier`, else the `facet~tier` line at the model's tier (clamped to the measured range), in the declared order of methods below, recording `r`, `skill`, `loo_rmse` and `n` without gating on `skill`, marked `estimated` (`~` in the catalog and tray), and replaced automatically when a direct board measurement arrives. When a required facet has no measurement, no estimate and no manual score, `coding` never skips it (`value: null`, Mode B excluded whenever `coding >= 1`), and `tool_operation` excludes the model at the demand levels that require that facet. Maintainer (`references/user-scores.json`, `kind: "maintainer"`) and local user (`~/.agent-compass/user-scores.json`, `kind: "user"`) manual scores on the demand-level scale (1 the panel's weakest, 3 its best) map dynamically onto `z` via `anchor.targets` with precedence `local user > maintainer > measured > estimated`.
- **Relatives: maker, product, line and size (owner, 2026-11-19).** Relatives are declared, never read off a name, an alias, a
  `-preview` tag or a `superseded` pair: every `families` row in `accounts.json` declares the `maker`, the `product` (the
  maker's product name the vendor releases its lines under: GPT, gpt-oss, Gemini, Gemma, Claude, ...; round 7), the `line`
  within it and the `size` (`small` or `full`) of the names it decides, and a row takes those of the `families` row that decides its
  bare name (`series.identity`); a `same_model` row takes its declared model's. A row's relatives for an **ability** facet
  (creativity, coding, design, image recognition, and the tier) are the rows of the same maker, product and size, every
  line of the product (an open-weights product of the same maker is another product; version distance counts the
  product's own versions) (`series.groups(acc, 'ability')`); for **responsiveness, Time/Task and cost per hour** those of its own line first
  (`groups(acc, 'line')`; Speed without a reading, below). A line name shared by two `families` rows is
  one line across versions (gpt-5.6 and gpt-6 `sol`, `terra`, `luna`; gpt-5.x and gpt-6 `pro`). A `small` row is never a
  relative of a `full` one, and conversion stays within one size. Relatives are ordered (`series.nearest`): the row the
  `same_model` declaration names first, alone; then by version distance, the rank difference among the distinct versions
  `accounts.json` `series_versions` declares for the row and its relatives (0: the same version); then the own line before
  another line at equal distance; a relative without a declared version comes last. An ability relative of another line
  is used only up to `estimation_order.sibling_first_up_to_distance`. Relatives still tied form one group and contribute
  the median of their estimates: no name decides, so adding a row that is not the nearest relative of X never changes X's
  estimate.
- **Speed without a reading (owner, 2026-11-19).** Responsiveness and Time/Task of a row with no reading of its own, in this
  order, each cell marked estimated with its method and inputs: (1) the vendor's stated ratio (Speed variants, below) or a
  dated maintainer declaration scoped to the row's one launch path: `models.<id>.response_time_source` `min_seconds`, the
  time from the request to the complete answer (gpt-6-pro, ChatGPT Web only: 600 s, owner 2026-11-19), a floor that
  bounds the value the next steps below give, never replaces it: Time/Task at each effort is the longer of
  `min_seconds / 60` minutes (a task is at least one response) and that value, responsiveness the slower of the floor
  read through the transform every measured row goes through on the time-to-first-answer board,
  z = (-ln(seconds) - x_anchor) / sd_panel (a long-thinking answer starts no sooner than its thinking ends), and that
  value; the floor alone where no step gives one; then bounded along effort (method `declared_response_floor`, the cell
  keeping both inputs and which one binds; a declared bound, not a fit: no Pro-line reading exists to score it
  against); (2) a relative of its own line, as above; (3) the maker's other lines, same size, version distance up to
  `estimation_order.sibling_first_up_to_distance`, among relatives read on a board (an estimated value is never another
  row's basis): where the relative's line and the row's own line are both read on at least two same-version pairs, the
  relative's value moved by their median line-to-line step (responsiveness: plus the median difference in panel z;
  Time/Task: times the median ratio at High), n and spread on the cell (method `cross_line_extrapolation`), else the
  median of every such relative at the nearest distance (method `cross_line_median`; `Scorer.cross_line`,
  `cross_line_times`); (4) the fleet
  methods (the responsiveness fit against tier, which `estimation_order.speed_cross_line_before_fit` puts after the other
  lines; Time/Task has none), then a known gap. Cost per hour keeps its own rule (the row's own list price, its own
  line's relatives only; Cost per hour from the fallback source, below). The evidence is
  `estimation_order.evidence` (audit/estimation_loo.py).
- **Downward from the larger model (owner, 2026-11-18).** A second relation, used only when a small row's same-size series,
  the tier fit and its own boards leave a facet without any value: a small `families` row may declare `tier_up`, the
  pattern of the same line's next larger row, with `tier_up_version` `same` (that row's first model is the same version
  as this row's current members) or `nearest` (the nearest version of the line); a small row listed in its row's `models`
  (a current member) takes, as its larger model, the first catalogued model of that row (`series.tier_up`); an older row
  of the line that is no current member takes the larger model and version the row's `tier_up_older` declares for it
  (`claude-haiku-4-5` -> `claude-sonnet-4-6`, `nearest`). With `same`,
  an ability facet is the larger model's curve moved by the gap the two show, `value(e) = large(e) + min(gap, 0)`, gap =
  mean over the facets both measured (else both have any value for, never a derived one: `gap_basis`) of `small - large`
  at High; status `estimated`, method `tier_down` with the base model, gap and the facets it was read on. A row's own gap
  is never carried across versions.
- **Size step (owner, 2026-11-18: "都补上估算值"; round 3: agreement of the pairs).** A facet still without a value on a
  small row with `tier_up` (same or nearest version; responsiveness included) is the larger model's curve moved by the
  fleet's size step for the facet (`five_axis.size_step_table`). Each declared same-version pair that measured the facet at
  High on both sides gives one value, `small - large`; an ability facet with fewer than `MIN_SIZE_PAIRS = 2` such pairs
  takes, per pair, the mean of `small - large` over the ability facets that pair measured (`basis: ability_pooled`, the
  ability facets share the panel-z scale; `differences` counts the readings, `n` the pairs). The step is the median of the
  pair values only when there are at least two pairs and all of them point one way (`five_axis.pair_agreement`);
  otherwise it is zero and the cell and the table say why (`agree: false`, `reason: directions disagree` or `fewer than 2
  pairs`): the derived value is the larger model's. Time/Task follows the same rule on the log ratio
  `time(small) / time(large)` at High (`time_size_step`; ratio 1 where the pairs disagree). The boards do not show a
  smaller model as uniformly faster or slower, so no direction is assumed for the speed facets. An ability value is in
  addition never above the larger model's at the same effort (`min(step, 0)`, and the bound across size below). Status
  `estimated`, method `size_step` with the base model, version, step, `n`, `agree` and the pairs; the table is published
  as `size_steps`.
- **No inversion across size (owner, 2026-11-18; restated 2026-11-19, round 7).** A row is dominated by another when they
  are the same maker and product (`families`), its declared size is strictly smaller (`small` below `full`) and its
  version is not newer: both declare a version (`series_versions`) and its is not above, or the other is its declared
  larger model of the same version (`tier_up_version: same`). Equal size never dominates, whatever the line or version,
  and version alone never does; a pair the data gives no version order is not compared. On every ability facet and
  effort a dominated row's value does not exceed a dominating one's (`Scorer.dominance`, `Scorer.bound_across_size`,
  after every fill). An estimate moves, a measurement does not: a dominating estimate is raised to the lowest value a
  dominated row can take there (its measurement, else the floor its own measured efforts give it, `effort_floor`; the
  cell's `size_order.raised`), a dominated estimate is bounded by the lowest dominating value (`size_order.held`), each
  with the value it had and the row that moved it, and each moved curve is bounded along effort with its measured efforts
  kept (`bound_pinned`; an effort that moves only there is listed under `size_order.along_effort` with its value before). One pass is the fixpoint, in no order a name decides. A model-entry row is bounded by
  release rows and never moves one: an entry dominating a release row is raised to that row's value, measured or
  estimated (round 8). Afterwards the build checks the statement over every dominated pair, facet and effort: what is
  still out of order must be two measurements, kept as read and listed (`measured_size_inversions`, printed by the
  refresh), a reading, not a defect, and no known-gap entry; any other pair stops the build, naming it. A newer smaller
  model above an older larger one is not dominated. The speed facets have no bound across size.
- **Edge efforts (owner, 2026-11-18; `none` round 5; readings and steps round 6).** Every effort of the one order
  (`user_scores.EFFORTS`, `none` and `minimal` included) is read on every board: a listing labelled Minimal is a measured
  reading at `minimal`, one labelled None or with a thinking-off label (`accounts.json` `effort_readings.labels`:
  Non-reasoning, Non-Thinking, Instant) a measured reading at `none` (`measurements.Builder.select_variants`,
  `five_axis.Scorer.aa_rows`). `none` is reasoning switched off, below `minimal` (the vendors' statements are quoted once, in
  `effort_readings`). A reading off the measured ladder enters the facet as listed, its distance from its board's own High
  added to the facet's High (`Scorer.facet_value`), and is used as measured: one board's bound does not move it, the
  facet's bound does, against the facet's own measured readings only, and an estimate next to a measured `none` is bounded
  by it, never the other way round (along effort an estimate moves, a measurement does not, `bound_pinned`). Two
  measurements out of order are the one case where a measurement moves: the anchor rule (High, else the effort nearest
  it, kept and the others bounded from it) moves the one farther from the anchor, and the cell lists it in `bound_moved`
  (gpt-6.1-terra execution: the measured `none`, -1.517, above the measured `low`, -1.808, is moved to it). A curve without an edge effort gets it from its neighbour plus the
  fleet's step at that end (`user_scores.EDGES`): `value(low) - value(medium)` for `minimal`, `value(max) - value(xhigh)`
  for `ultra`, and for `none` `value(none)` minus the lowest listed reasoning effort, anchored at the curve's lowest
  measured reasoning effort (High when nothing below High is measured; `user_scores.edge_anchor`). The step is a property
  of the boards' readings: one difference per listing a board of the facet holds at both efforts, catalog row or not
  (`public-measurements.json` `sources.<board>.edge_pairs`, the raw readings in panel z, `Builder.edge_pairs`), times the
  board's link scale; `minimal` and `ultra` take their median (`n >= 2`; methods `fleet_lowest_step`,
  `fleet_highest_step`), `none` takes it only when there are at least two and all point one way (`five_axis.pair_agreement`),
  else zero with the reason (method `fleet_off_step`); published as `effort_steps.<facet>.low.minimal`,
  `.max.ultra` and `.lowest_reasoning.none` with the boards and their pair counts (`five_axis.edge_step_table`). An ability
  facet no board gives a pair for takes the median of the other ability facets' board steps for that edge (method
  `other_facets_median`, the facets and their steps shown); responsiveness never does. A facet without either gets no
  edge (`effort_fill_refused`). An operating-point row that cannot reason and has no reading of its own reads its estimated
  curve at `none`, the off point of the relative it was estimated from (`flatten_point`). Time/Task:
  the neighbour times the fleet's median ratio at that end over the Artificial Analysis listings with both
  (`effort_steps.time_per_task`, `n >= 3` as every fleet time ratio); for `none` the same pair rule on the log ratio
  `time(none) / time(lowest measured effort)` (ratio 1 where the pairs disagree). An extrapolated Time/Task never leaves the
  range of Time/Task the board measures for the fleet: it is clipped to it, and the cell says so
  (`clipped_to_fleet_range`). A hand-written score fills the edges the same way.
- **Own ability median (owner, 2026-11-18), the last and coarsest method.** An ability facet still without a value (no
  source and no larger model: `glm-5.2` faithfulness and image recognition) is, at each effort, the median of the model's
  own ability facets there (at least `MIN_OWN_FACETS = 2`, all on the panel-z scale); status `estimated`, method
  `own_ability_median` with the facets used. The tier fit would be better founded, but where it applies it has already run:
  this method serves the models it cannot (no tier, or a fit that failed its checks). A `same_model` row copies what its
  model gains this way, for the categories it carries.
- **Labels (round 5).** A board's reasoning label reads as `accounts.json` `effort_readings.labels` declares, one rule for
  every model: "Non-reasoning" is `none`, whatever effort the tag names besides; "Reasoning" without an effort is a
  listing without an effort label. A tag part ending in "Fallback" is Artificial Analysis's note of the model its harness
  falls back to on a refusal, not an effort.
- **One operating point (round 5).** A model whose vendor documents no reasoning control (`accounts.json`
  `models.<id>.operating_point`, cited, with `reasoning` true or false) has one configuration: its one board listing is
  its only reading, and every effort reads that value with no effort step (method `operating_point`).
- **Identity of a listing (round 5).** A dated listing ("Qwen-Max (0919)") names that snapshot: the date stays part of its
  base name (`sources.base_name`), and a row reads it only when the row's own name carries the date. A row whose
  `listed_as` names a board listing other than its own spelling needs `listed_as_source` with the public statement that
  the listed name is that model (kind vendor, community or benchmark, https url, date, statement;
  `measurements.listed_as_identity`); a dated maintainer declaration is not one.
- **`default` (owner, 2026-11-18).** The boards list some models without an effort label. The measurements store such a
  listing under `default` (`calibration/measurements.py`); it is the model's one configuration, and the calibration keys it
  at the effort the dated declaration `accounts.json` `effort_readings.reads` names, High (`five_axis.aa_rows`,
  `five_axis.time_rows`). A launch at `default`, and a bundle that names no effort, read the values stored there
  (`five-axis-scores.json` `effort_readings`, `axis_scores.reading_effort`); `default` is not a point on the effort scale.
- **Order of methods (round 5: by measured error).** For each cell, the first that applies: a measurement; a hand-written
  score (run time, it wins over every estimate below); own-curve interpolation; the panel effort step; the same-size
  series conversion from relatives up to version distance 1; the tier fit; the series conversion from farther relatives;
  the downward derivation with the row's own gap (same version only); the size step from the same version; for `none`,
  `minimal` and `ultra`, the fleet's edge step; the own ability median; last, the size step from the nearest version. The
  order is declared once, with its leave-one-out evidence, in `accounts.json` `estimation_order` (`five_axis.ESTIMATION_ORDER`
  refuses another), and the refresh never reorders it by itself: before a release, re-measure each method's error by
  leaving every measured High cell out in turn, `python -X utf8 audit/estimation_loo.py` (read-only), and change the
  order only by editing the declaration and the code together. Every estimate is then bounded along effort and,
  for an ability facet derived from a larger model, across size; at run time a derived facet follows its larger model's
  value after the hand-written scores, shipped and local (`user_scores._follow_effective_sources`: moved by the source's
  change at each effort, never above it; a hand-written score on the derived model itself wins).
- **Every effort has a value (owner, 2026-11-18).** After measurement and estimation, every facet that has a value at some
  effort gets one at every effort of `low..max` (`five_axis.fill_efforts`): between two of its own efforts, linear in effort
  rank (`own_effort_interpolation`, the model's effort ladder); outside them, its nearest own effort plus the panel's median
  own step between the two efforts (`panel_effort_step`, over the panel models measured at both, `n >= 2`; the table is
  published as `effort_steps`). Filled efforts are listed in the facet's `filled_efforts` with their method; the efforts
  that hold a board reading in `measured_efforts`. An effort the panel gives no step for stays empty and is listed in
  `effort_fill_refused`. High of a measured facet is filled the same way before the fits (`fill_high`); targets read only
  measured and `interpolated` High values. A hand-written score fills the same way from its High
  (`user_scores._manual_by_effort` with `effort_steps`). `none`, `minimal` and `ultra`: the edge efforts above.
- **No inversion along effort (owner, 2026-11-18).** Along the efforts of one model an ability facet never decreases as effort
  rises, `tool_operation.responsiveness` never rises, and Time/Task never shortens. One rule, `user_scores.bound_along_effort`
  (direction `user_scores.rises_with_effort`), bounds every curve, board readings, facets, estimates, filled values, manual
  curves and Time/Task: High is the anchor and never moves (a curve without High is anchored at its effort nearest High); a
  lower effort is capped by the next higher one, a higher effort floored by the next lower one. A filled or followed value is
  bounded with the facet's own readings pinned, so it never moves one. A reading the rule moves is no longer a measurement
  (`bound_moved`; a Time/Task listing it moves becomes method `effort_bound`).
- **`same_model` declarations.** A row `accounts.json` `same_model` declares to be another row's underlying model (`dot` -> `gpt-6-astra`, `grok-bot` -> `claude-opus-5-5`) inherits that model's facet curves, `duration_multiplier`, and `usd_per_hour` when listed in `carries` (`creativity`, `coding`, `aesthetics`, `duration`, `usd_per_hour`), while channel-specific `tool_operation` stays `missing`. When a `same_model` row also declares a `capacity_rule`, `axis_scores.five_axis_overlay` resolves the detected target subscription plan's monthly price from `plan-templates.json` at runtime and matches the same-price (or price-ratio-scaled nearest) reference subscription on the capacity ladder.
- **One operating point on its model (owner, 2026-11-19).** A `same_model` row that also declares `at_effort` (an effort
  of the scale with its dated citation) is its model's weights at that one effort, with no effort inside it (ChatGPT
  Web's GPT-6 Pro: GPT-6 Astra at `ultra`, the Help Center's "Pro effort remains GPT-6 Pro, powered by GPT-6 Astra" and
  the owner's placement directly above `max`): every effort of the row reads the model's cell at that effort, facets and
  Time/Task, whatever method gave the model that cell (method `same_model`, `at_effort` and the model cell's method on the
  cell); a declared response floor then bounds it (Speed without a reading, below). A shipped maintainer score never
  reads a row's responsiveness faster than its declared floor: the refresh refuses one, naming both, under whatever
  spelling the score's key resolves from (`reference_provenance.validate_scores_against_floors`). On every ability facet
  the model's cell at the point is a floor under a shipped maintainer score of the row (owner, 2026-11-19, round 8):
  routing reads max(score, model cell) at each effort, applied with the maintainer overlay
  (`user_scores.operating_point_floor`, which records both inputs and which binds); a local user's score is the user's
  and is neither bounded nor checked.
- **Speed variants (owner, 2026-11-19).** A `same_model` row that also declares `speed` is a speed variant of its
  model: the same weights served faster (the relation is the cited declaration, never the name). Its ability facets are
  the model's (`carries`). Its responsiveness, Time/Task and cost per hour are its own: a board reading of the row where
  one lists it; else the model's value moved by the vendor's stated speed ratio `r` (`speed.ratio`, cited on the row; a
  stated range `[lo, hi]` read as its geometric mean): the ratio divides the time to the first answer token and the
  Time/Task, so responsiveness moves by `ln(r) / sd_panel` of `aa.ttfa` and Time/Task is divided by `r` (method
  `speed_ratio`, the base value and the ratio on the cell); else, when the vendor states no ratio (`ratio: null`, a
  ceiling such as "up to 20x" is not one), the model's value as a stated lower bound (method `speed_lower_bound`: at
  least as fast as its model). Cost per hour only at the row's own list price (converted from its model by the list price
  ratio and times `r`, as the row reads `r` times the model's tokens an hour), never the model's price: without its own
  price the row takes the unpriced path.
- **Between two rows (owner, 2026-11-20).** A row `accounts.json` `between` declares to be two models at once, a planner
  and an implementer (Antigravity's Orchestrator options: the planner in the option's name plans and reviews, Gemini 4
  Hydrogen implements, one fixed configuration; the vendor page cited on the declaration, the CLI's id in its dated
  `id_declaration`), is read at the declared `share` of the way from its planner row toward its implementer row (`share`
  with its dated `share_declaration`, a number in (0, 1); changing it is that one field and `agentcompass data refresh
  --no-fetch`). The share is the owner's (0.25, confirmed 2026-11-20): no public source measures an Orchestrator option.
  It is interpolated rather than carried from the planner because carried values would tie each option with its planner
  on every quantity (owner: "如果不是插值 那不是有很多同分吗"). Both bases are read at the effort `effort_readings.reads.default` names (High) and the one value is held
  at every effort, as an operating point's. Every facet is `planner + share * (implementer - planner)` on the panel-z
  scale (responsiveness is z of -ln(seconds), so its linear position is the geometric position in seconds), and so is
  the tier; Time/Task and cost per hour, ratio quantities that this module combines only by ratios, take the same
  position on the log scale, `exp(ln p + share * (ln i - ln p))` (`Scorer.interpolate_between`, `between_minutes`,
  `between_usd`; status `estimated`, method `between`, both bases with their value, status and method and the share on
  the cell; the category is the minimum of its facets as for every row). A quantity a base lacks stays missing and is
  surfaced as a data error, never the other base alone; a reading of the row's own stands. At run time each facet
  follows its bases as they are effective (local user > maintainer > measured > estimated): it is recomputed at the
  share from the two effective curves at High and held at every effort (`user_scores._follow_effective_bases`), so a
  hand-written score on a base moves the option and one on the option itself wins. The tier, Time/Task and cost per
  hour have no hand-written layer and stay as built. The row is no one's base:
  it takes no identity in `families` whatever pattern its name matches (`series.identity`), is never a relative (a
  `same_model` or `between` declaration naming it as its model or a base is refused), and is
  outside every fit, step, link, target, ceiling and the panel (`Scorer.entries`), a row of the release or not; so a base
  may be a model entry (the Neon option rests on `gemini-4-neon`), and adding the row changes no other row. It is computed
  last, from the bases' final values. Facts the vendor does not state for the option (image input, context window) are
  not copied from its bases.
- **Cost per hour from the fallback source (owner, 2026-11-19).** `usd_per_hour` is read from arena sessions
  (`arena.agent_sessions`); a row arena does not price takes option B (`derived.community_mix`: Artificial Analysis output
  speed priced at the community token mix), which runs high against arena sessions on the rows that have both. A row
  whose value still rests on option B alone (no series or size conversion from a relative with a direct value) is put on
  the arena-sessions scale: its option-B value divided by the fleet's median ratio option B / arena over the rows that
  have both (at least three), the ratio, its `n`, its spread (`min`, `p10`, `p90`, `max`), the rows it rests on and the
  option-B value stated on the cell (`on_primary_scale`), marked `estimated` (`five_axis.scale_fallback_usd`).

## Scripts

| Script | What it does |
| :--- | :--- |
| `sources.py fetch` | Downloads LMArena (`lmarena-ai/leaderboard-dataset`), Design Arena (`website`, `3d`, `svg`), Artificial Analysis (`aa-models.html`), OpenRouter models, and the community capacity pages (`community.py`), writing `fetch-manifest.json` (url, fetch time, sha256 per file). `--only FILE` refetches single files. The Artificial Analysis leaderboard page embeds records only for releases it does not mark deprecated; `refresh.py` also fetches (`sources.fetch_releases`), one page each as `aa-release-<slug>.html`, the release page of every catalogue model that no leaderboard record matches but a deprecated release is listed under (`measurements.aa_release_plan`, derived from the fetched page and the `listed_as` names, never a list of models; the newest release of the base name a model is listed under, so every row of a base name reads the same release; releases whose newest cannot be told from their `releaseDate` are a missing source). `sources.aa_release_record` reads such a page's records into the leaderboard's record shape; each reading made from one cites that page's url. A page is read only when the plan chooses it, in every mode, and only for the base names the plan chose it for; a row the matcher resolves to another base name is a missing source. A page that cannot be fetched, whose file is gone from the data directory, or whose record has a shape the reader does not know is a missing source, never an older copy; a page that lists the release without any figure is a source not used (its model is estimated by the declared methods). |
| `tiers.py [ids]` | Reads each model's tier off the `arena.ai` Overall board through the six anchor rows; models that board does not list get `tier.aa_index`. |
| `series.py` | Library: relatives by the declared maker, product, line and size of each `families` row (`identity`, `groups`), `same_model` links, the downward `tier_up` relation, and `nearest`, a row's relatives ordered by the declared version distance, then line (Relatives: maker, product, line and size, below). |
| `measurements.py` | Writes `references/public-measurements.json`: panel standard scores (`z` and AA `z_by_effort`), structural and statistical data-hygiene checks, `usd_per_hour`, list prices, image input, and fast-mode multipliers. |
| `five_axis.py [--distrust SOURCE] [--accept-panel-change]` | Writes `references/five-axis-scores.json`: per-effort facet curves, parallel-board link offsets, category scores, `estimate_fits` (`r`, `skill`, `loo_rmse`, `n`), `usd_per_hour`, `duration_multiplier`, community plan capacities, panel `anchor.targets` (`levels` 1..3 with the level-3 `ceiling`, `continuity = t3 - t2`), `anchor.sd_panel` and `anchor.panel_change`, and surfaced data errors. Refuses a panel change unless accepted. |
| `arena_capture.py import CAPTURE.json ...` / `show` | Imports browser-captured `arena.ai` Agent / Code and Alignment boards and preserves their manifest entries across `sources.py fetch`. |
| `community.py` | Reads community real-usage measurements of subscription window capacity, token mix, vendor plan multipliers (each names the windows it covers), and usd-per-hour check rows; `MAINTAINER_READINGS` holds dated maintainer measurements of windows no public source measures, and `UNMEASURED` declares the windows left without any. |
| `refresh.py [--no-fetch] [--report FILE] [--distrust SOURCE] [--accept-panel-change] [--model ID ...]` | Single entry point (`agentcompass data refresh [--no-fetch] [--accept-panel-change]`; `--model ID` fetches into the model's own cache entry and merges only its rows on the shipped scale, see To add a model): runs fetch and the release-page fetch (unless `--no-fetch`; a cache without the release pages the plan chooses reports each as missing, exit 2, so refresh once without `--no-fetch` after the leaderboard page changes), `measurements.py`, `five_axis.py`, `capability_catalog.py`, validates the provenance checks (`reference_provenance.py`) and `user-scores.json` schema, and prints the source-age report. Exit 0 fresh, 1 stale, 2 missing or invalid, 3 refused with nothing written (a refused panel change or `--model`; the staleness report never exits 3). |

## Refresh

```
agentcompass data refresh                     # fetch, rebuild shipped data + views, validate, source-age report
agentcompass data refresh --no-fetch          # rebuild and validate from cached downloads without fetching (the Artificial Analysis release pages must be cached too: each one missing is reported missing)
agentcompass data refresh --no-fetch --accept-panel-change   # write a rebuild whose panel members changed (refused otherwise)
```

The fetching refresh runs once per release, as a step of preparing it; nothing reads the boards between releases. What moves between releases is only what the maintainer publishes on purpose (below).

Or from this directory: `python -X utf8 refresh.py [--no-fetch] [--data DIR] [--report FILE] [--distrust SOURCE] [--accept-panel-change] [--model ID ...]`.

One command runs the full chain in topological order: `sources.py fetch` plus the Artificial Analysis release pages (both skipped with `--no-fetch`, which reads the release pages already in the data directory that the fetched leaderboard page and the catalogue ask for) -> `measurements.py` -> `five_axis.py` (`--distrust <source id>` leaves a source out) -> `capability_catalog.py` -> validation (provenance checks, `user-scores.json` maintainer schema, and generated capability views) -> source-age report. When refreshing browser-captured `arena.ai` boards, run `python -X utf8 arena_capture.py import <capture>.json ...` first, then `agentcompass data refresh --no-fetch`.

## To add a model or a score

Every per-model fact is reference data under `references/`; adding a model or a score changes no Python and no test.

1. **The row.** In `references/accounts.json`, add `models["<id>"]` (copy a sibling row and edit it): `label` (tray name),
   and when they apply `api_family` (`claude` / `codex`: the native CLI), `context_window`, `image_input_source` or
   `text_only_source` (cited or dated maintainer statement), and `fast_capable: true` only for a verified fast launch
   ([fast-modes.md](../references/fast-modes.md)).
2. **Board names.** `listed_as: {"arena": "...", "aa": "..."}` gives the name arena.ai / LMArena and Artificial Analysis list
   the model under, when the automatic matcher cannot derive it from the id (a dated listing, other wording). Leave out a
   board that lists it under its id or does not list it. On Artificial Analysis a name is a base name: an effort tag is not
   part of it, a date tag is (`"Qwen3.8 Max"` names the listings `Qwen3.8 Max (High)` and `Qwen3.8 Max`, never
   `Qwen3.8 Max (0902)`, another snapshot), and when it lists several deprecated releases under one base name the newest
   release is read. On arena.ai a full listing name matches as well as its base name. A name that is not the row's own
   spelling needs its cited identity statement in `listed_as_source` (Identity of a listing, above).
3. **Prices (optional, cited).** `list_price` (`input`, `cache_write_5m`, `cache_write_1h`, `cache_read`, `output`, USD per
   million tokens) with `list_price_source` (`kind: "vendor"`, https `url`, `as_of`) prices local session logs in the tray;
   `vendor_fast_mode` (price and speed multipliers, fast and standard prices, `url`, `pricing_url`, `as_of`, `statement`)
   adds the fast variant's measurement.
4. **Launch and family.** A bundle under `accounts[<account>].bundles` and its efforts under `dispatch_efforts[<harness>]`
   make it launchable (discovery adds auto bundles for what a CLI advertises); an effort list there cites the vendor's
   statement in `dispatch_efforts_sources` where one exists. When it is the newest model of a family, point that family's
   entry in `families.rows` (ordered `{pattern, models, size, tier_up?, tier_up_version?, tier_up_older?}`; the first
   matching pattern decides; `size` is `small` or `full`; `tier_up_version` `same` or `nearest` goes with `tier_up`;
   `tier_up_older` maps an older row of the line to `{model, version}`; move the declaration's `as_of` to the day of the edit) and the family's short `aliases` at it, and
   list the older model under `superseded`. A name the vendor states is served by another row is an alias, not a row:
   every alias carries its dated statement in `alias_sources`, name by name (the maker's word, or models.dev's
   `canonical_model_id` with the snapshot's date, an https page), and an alias without one is refused
   (`scripts/reference_provenance.py`); a dated maintainer declaration stands only for the maintainer's own shorthand
   (`shorthand: true`, "short name for <row>"). Before a release, `audit/gateway_coverage.py` also refuses an alias whose
   two names models.dev lists with different canonical ids (unless the maker states they are one model) or at different
   prices from one provider, and a shorthand that is a row or a name a saved catalog offers. A name with `-pro`, a speed variant (`-highspeed`, `-ultraspeed`) or another marker
   of a different model is a row of its own, never an alias. A board that lists the row under another name gets it in
   `models.<id>.listed_as` (`arena`, `aa`).
5. **Rebuild its row.** Run `agentcompass data refresh --model <id>` (repeatable for several new models).
   - **Its own entry in the cache.** It fetches what the full refresh fetches into the model's entry,
     `DIR/models/<id>/` (the pages, each with url, fetched_at and sha256 in the entry's `fetch-manifest.json`, and
     `entry.json`, schema `agent-compass.calibration-model-entry/1`, naming the model and the fetch time), never into
     the release snapshot (the pages at the top of `DIR`), which it does not touch. Every rebuild, the one it runs and
     any later `--no-fetch`, builds the measurements from the release snapshot and takes each model that has an entry,
     in catalog order, from its own entry's pages, so `--no-fetch` after `--model` writes byte for byte the same files
     (on the same day: the source ages count days). The full refresh, a new release snapshot that reads every row,
     retires the entries. Without a release snapshot in `DIR` (no `fetch-manifest.json`) it merges into the shipped
     measurements.
   - **Only the model's rows.** It writes to `public-measurements.json` only the named models' entries, conflicts and
     checks, every entry the full refresh writes for them: with the panel scale unchanged, the model's measurement row
     equals that of a full refresh over the same boards. It never changes another row's measured value: when the release
     snapshot and the other entries do not give every other row and the panel exactly as shipped (a cache that is not
     this release's, or a catalog edit that moved another row) it stops with nothing written and no entry stored.
   - **Fleet fits are frozen per release (owner, 2026-11-19).** Everything fitted over the fleet is fitted on the release
     snapshot's rows only: the tier scale (anchor rating and sd) and the index -> tier map, the tier fits, the effort,
     edge and size steps, the effort correlations and links, the Time/Task fits and ratios, the usd cross-source ratio
     and the fallback scale, the level targets, the level-3 ceilings and the panel. The rows of the release are a
     recorded fact of its snapshot: the fetching refresh writes them once into the snapshot's `fetch-manifest.json`
     (`release_rows`, the catalogue rows it was read for), every rebuild, `--no-fetch` and `--model` alike, carries them
     unchanged into `public-measurements.json` `release_rows`, and they are published with the set (`refresh.release_rows_of`).
     A row that is not a row of the release (one added to `accounts.json` later, whatever the snapshot reads for it) and
     a row a model entry gave (`model_entries`) never enter a fit, a target, a ceiling or the panel, and are never a
     relative's base; they are scored by the release's fits, and may themselves be estimated from release relatives
     (`five_axis.without_entries`, `release_same_model`, `release_tier_up`). The declared structure (version ladders and
     distances, the relatives a record lists) stays the release's: a row of the release keeps its place in it when an entry
     gives it readings, and any other row takes none. A row of the release its snapshot reads is refused (`--model` stops
     with nothing written and no entry stored): its readings belong to the release's fits and change with the next
     release. So adding a row and rebuilding with `--no-fetch` before or after `--model` writes the same bytes. Its tier is its board reading put
     on the release snapshot's tier scale (`sources.tier.arena_overall.tier_scale`) or through its index map
     (`sources.tier.aa_index.tier_map`), after the board's drift as below. So adding a model changes only that model's
     rows: every other row and every fit, step, link and target of `five-axis-scores.json` stays byte for byte; only the
     per-model lists gain its records and the tier-source counts count it. The next release's full refresh reads every
     row from the boards, fits over all of them and retires the entries.
   - **A reading taken after the release snapshot (owner, 2026-11-19).** A board z-score is put on the shipped scale by
     the board's own drift, measured on the declared panel: drift = the median, over the panel members the board lists
     in both the release snapshot and this fetch, of (raw in this fetch - raw in the snapshot); `n` is how many members
     it used. The model's raw readings are corrected by the drift and converted with the shipped anchor and sd_panel
     like any reading, `(raw - drift - anchor_raw) / sd_panel` (the scale recorded at 6 significant digits, so such a z
     can differ in the fourth decimal). The correction assumes the board moved by one common shift between the two
     dates, so it is applied only where the members show one (owner, 2026-11-19): the drift is larger in size than the
     members' median absolute deviation around it (`common_shift`). Otherwise the board shows no common shift and the
     reading is merged plain, `(raw - anchor_raw) / sd_panel`, with the spread recorded on the cell. Either way the
     entry is still measured, and carries `drift_correction` {`drift`, `n`, `members`, `spread` {`min`, `max`, `mad`},
     `common_shift`, `snapshot_as_of`, `fetched_as_of`} in `public-measurements.json` and in its five-axis cell source
     (`scripts/reference_provenance.py` checks the shape): the range of the members' own changes and their median
     absolute deviation from the drift say how uncertain the reading is. With fewer than two members in both, a board
     that moved leaves the entry out (the pair is a known gap of kind `anchor_reading_moved` until the next release's
     fetch). Where the board did not move (every member's raw the same) the merge is the plain one: the entry as built
     on the same scale, else recomputed on the shipped scale, and left out when the anchor's reading moved; a z-score
     whose source has no shipped scale is left out. The run prints each entry it left out and why, each corrected board
     with its drift and spread, and what else it did; a model no board lists, or an entry it cannot merge for another
     reason, stops the run before anything is written (exit 3; a run that writes exits with the staleness status 0, 1
     or 2).
   - It then validates the catalog (`reference_provenance.validate_accounts_provenance`) and rebuilds
     `five-axis-scores.json` and `model-capabilities.{json,md}`, and prints how many values of other models moved (none,
     by the rule above). A model in
     `calibration_panel` (or the anchor) is refused: it changes the scale, so it needs the full refresh (`agentcompass
     data refresh`, or `--no-fetch` from cached downloads), which rebuilds every row. Then publish and push (Publishing a model
     between releases).

A **maintainer manual score** is `agentcompass score set <model> ... --public`, which writes `references/user-scores.json`;
then run `agentcompass data refresh --no-fetch` and `agentcompass data publish`. A **measured score** comes from the boards on the next refresh. The calibration
scale itself (the anchor, the sentinel models and the panel members) is `calibration_panel` in `accounts.json`, a dated
maintainer declaration; changing it rescales every score. Every written value names its sources and dates.

## A name's row on a provider (`provider_names`)

`accounts.json` `provider_names` says which row a served name is when that is not the row of the same name (rule:
[router-internals.md](../references/router-internals.md), One resolver). Add or correct a declaration as data, citing its
source like an alias (`kind`, `url`, `as_of`, `statement`); no name or date rule exists in code.

- **The maker moved a name to a newer dated snapshot** (DeepSeek's `deepseek-v4-pro` now serves `DeepSeek-V4-Pro-0813`):
  `defaults["<name>"] = {"model": "<snapshot row>", "kind": "vendor", ...}` citing the maker's page. Where the maker's page
  describes the model but names no API id of that name (DeepSeek's `deepseek-v4.1-flash`), the declaration is the owner's
  rule applied by the maintainer: `"kind": "maintainer"`, dated, its statement citing the maker's line it rests on. The
  name must be a row (the undated id it repeats), which stays a row with its own readings; only what the served name
  resolves to changes. The snapshot needs a row (step 1 above).
- **A provider's listing names a different dated snapshot** (models.dev's `canonical_model_id` for that provider is
  neither the default's row nor the moved name's own undated id): `rows["<provider>"]["<name>"]` with the row it is
  (`kind: "community"` citing models.dev, or the maker), or `"model": null, "status": "other_weights"` when no row exists
  (OpenCode's and Vivgrid's `deepseek-v4-flash`, listed as `deepseek-v4-flash-0731`). A `-preview` name is the released row
  of that name (`model_resolution.without_preview`, owner 2026-11-20), read by the release check from that same owner in
  both directions (`hy3-preview` listed as `tencent/hy3-preview` is `hy3`; `qwen3.8-max` listed as
  `alibaba/qwen3.8-max-preview` is `qwen3.8-max`); a provider that retired the preview endpoint declares `status: "retired"`
  (Google's `gemini-3.1-flash-lite-preview`). Where Magpie's preset id differs from the models.dev catalog id(s) it serves (`opencode-zen`
  serving `opencode`, `tencent-cloud` serving `tencent-tokenhub`), `provider_names.host_providers.magpie` maps that
  Magpie preset id to those catalog ids (`audit/magpie_catalog.py` `preset_providers`), applied only to Magpie preset
  ids and never duplicating `rows`. A listing that gives the moved name's
  own undated id only repeats the name: it gets no declaration and reads the default. Where a listing spells the row's
  model another way, `canonical_id` gives that spelling (Mistral lists `ministral-14b-2512` as
  `mistral/ministral-3-14b-instruct-2512`).

- **A full id names fixed weights** (a maker's repository id: `deepseek-ai/deepseek-v4-pro`, DeepSeek's Hugging Face
  repository of the April preview weights, beside the separate `deepseek-ai/DeepSeek-V4-Pro-0813`): `defaults["<owner>/<name>"]`
  with the row of those weights, citing the repository page. It is read before the last segment's default, on any provider.
  A gateway's own id for the maker's API name (`deepseek/deepseek-v4-pro` on OpenRouter) is not a repository id and gets
  none.

Known cost: a host that serves the earlier open weights under the undated name is read as the new snapshot until its
listing names that snapshot or the maintainer declares it. The maintainer can declare it three ways, each accepted by the
provenance check and the release check: the host and the name (`rows["<host>"]["<name>"]`, kind `maintainer` or the
maker, with a statement of its own; a `community` declaration resting on a listing that only repeats the name is refused
by the release check), a full id on any provider (`defaults["<owner>/<name>"]`), or a full id on one host
(`rows["<host>"]["<owner>/<name>"]`).
- **The maker shut the name down on its own endpoint**: `rows["<maker>"]["<name>"] = {"model": null, "status": "retired",
  "kind": "vendor", ...}` with the maker's notice, and the name's entry in `audit/gateway-out-of-scope.json`.

A moving name changes when the maker moves it: between releases the maintainer corrects the declaration and publishes it
(`agentcompass data publish`, below), as for any launch row. `audit/gateway_coverage.py` fails while the saved models.dev
listings (`audit/models-dev-names.json`) disagree with a declaration, or list a default's name on a provider that declares
nothing with a canonical id that is neither the default's row nor the moved name's own undated id; it prints how many
listings fall under each. A default also
moves where a score keyed by the name lands (router-internals.md, Score keys): rebuild and check what moved.

## Publishing a model between releases

Installs read a set the maintainer published on `main` without a product update
([router-internals.md](../references/router-internals.md#5-reference-data-shipped-and-published)). For a new model:

1. Edit `references/accounts.json` (steps 1 to 4 above).
2. `agentcompass data refresh --model <id>` (step 5).
3. `agentcompass data publish`: accepts the set (this code's readers load it and an offline plan completes with it) and
   writes it to `published/<schema>/` and its manifest to `references/public-data.json`. Editing `references/` alone
   publishes nothing.
4. The checks: `agentcompass data publish --check` and `agentcompass data publish --check-released` (every released
   version of the same schema accepts it), `pytest skills/agent-compass/scripts/test_public_data.py
   skills/agent-compass/scripts/test_reference_provenance.py`.
5. Commit the changed files under `references/` and `published/` and push to `main`, once.

Nothing else: installs pick the set up at their next check (at most 6 hours). A corrected hand-written score or launch
row is the same procedure with its own edit in place of steps 1 and 2. A model whose launch needs code (a new harness or
provider, a new account family, a new launch field) cannot be published this way: the acceptance refuses a harness or
family this code does not launch, and such a model ships with a release. A bundle's effort is checked only against the
set's own `dispatch_efforts` row for its harness, which the same publication may extend; whether the CLI accepts that
effort is not checked, so add one only when the CLI documents it.

When to bump `SCHEMA_VERSION` (`scripts/public_data.py`): with any change after which a released reader of the current
schema would misread data written for the new code: a new required field, a renamed or removed key, a changed meaning or
unit. `--check-released` catches a publication an older reader refuses; it cannot catch a field an older reader reads and
interprets differently, so that judgement is the maintainer's. After a bump, `data publish` writes `published/<new>/`;
installs of the older schema keep reading `published/<old>/`. A release prepared on `main` under a new schema publishes
its own set first, so `--check --release` holds when it is tagged.

## Rules the data follows

- The anchor tiers are the user's judgment. The scripts report contradictions with them and never change them.
- **Dates, not ages (owner, 2026-11-19).** The generated files hold facts with their dates (`as_of`; a capacity row the
  newest date its value rests on, a derived entry `rests_on_as_of`, the oldest of its inputs), never an age, a `stale`
  flag or a warning computed from the day of the build: a rebuild on another day from the same inputs writes the same
  bytes, so a published set does not change with the calendar. A generated file's own `generated_at` is the latest date
  of a reading it was built from (`measurements.inputs_date`; five-axis-scores.json takes the measurements'), never the
  time of the run, and `measurements_sha256` hashes that content: adding a model entry and retiring it gives back every
  byte (round 7). How old a date is and whether that is stale
  (`STALE_DAYS`) is computed where it is read, by `scripts/data_age.py`: the refresh's printed report and its exit
  status, and any reader that shows or warns on an age.
- Public cited data only. A value without a trusted source is missing, and missing is a surfaced data error, never a default.
- No model names in router code. Everything above is data under `references/`, including each model's board names,
  prices, fast-mode multipliers, family patterns and the calibration panel.
