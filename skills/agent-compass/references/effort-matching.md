# Effort and the decision ledger

The router picks an effort by scoring each effort bundle like any other candidate: the fit of the model's four category scores
(`divergence`, `coding`, `aesthetics`, `tool_operation`) at that effort to the task's demand, minus the quota economy's price for
the spend at that effort. Effort is not a function of reasoning depth, and no gate or floor decides it.

## Model side: per-effort scores and Time/Task

Per-effort capability and duration live in `references/five-axis-scores.json` (`calibration/five_axis.py`):

- **Per-effort board readings.** Artificial Analysis boards (`aa.terminalbench_4`, `aa.frontiercode_2`, `aa.mmmu_pro`, and `aa.ttfa` read as `-ln(seconds)`) publish separate rows per effort (`low`, `medium`, `high`, `xhigh`, `max`). Missing efforts on an AA board are filled from the nearest same-series sibling's step on that board, else the fleet median step (`n >= 3`), else held at the nearest listed effort.
- **Single-variant boards.** Boards that list one variant per model (`arena.agent_code`, `arena.agent_alignment`, `lmarena.*`, `designarena.*`) shift across efforts using the model's (or same-series sibling's) AA curve on the same facet when an AA board measures that facet; when none does, the board stays flat across efforts and its status is marked `measured_partial`.
- **High from one-sided effort data.** A measured facet whose own curve lacks High gets it before PAV pooling, never over a measured value: between its nearest measured efforts below and above High by linear interpolation in effort rank (status `interpolated`), or, measured on one side only, the nearest measured effort plus the panel's median own step to High on that facet (`n >= 2`; status `estimated`, method `panel_effort_step`). `measured_efforts` lists the efforts the boards measured.
- **Manual scores.** A manual score (`user-scores.json`) sets High; the facet's measured efforts keep their values, its other efforts follow its own curve moved to the manual High (missing when it has none, never another facet's curve), and ability facets stay non-decreasing with High held.
- **Time and spend.** The hours a task takes at an effort are the Artificial Analysis Time/Task at that effort (`duration_multiplier.by_effort`, as a multiple of `claude-opus-5-5` at High), and the spend is those hours at the model's `usd_per_hour`. A model AA lacks takes its `arena.ai` Agent Time/Task moved onto that scale by a cited fit (`require_skill=True`); a model with no time of its own takes its nearest same-series measured sibling's per-effort time (`series`); missing efforts interpolate or step via the fleet's cited ratios (`basis_by_effort`). A model or effort with no time is out of automatic routing (a named dispatch still runs).

## The match

- Targets come from the panel (`n = 12` fleet models): level 1 (`t1`) is the panel's weakest (and the zero point of `five_axis_fit`), level 2 (`t2`) is the panel median, and level 3 (`t3`) is the anchor (`0.0`, monotone-raised to `t2` if `t3 < t2`).
- A routine task (`demand = 1`) is met by lower efforts (`responsiveness` is also higher at lower efforts when `tool_operation >= 2`) and pays less quota; a demanding task (`demand = 2..3`) climbs to higher efforts when the extra fit (`min(z, t) - t1`) outweighs the quota cost.
- Expiring quota makes the extra spend cheap (the economy prices it near zero under the window's recovery line), so effort rises.
- `decision.task.max_recommended_effort` is the effort of the chosen bundle.

## Ledger snapshot

Every non-dry-run `plan` line in `router-ledger.jsonl` carries `decision_snapshot` (`allocator.decision_snapshot`): the task
demand, the scoring weights and the ranking. The best 6 eligible rows keep every component, `expected_pct`, `expected_usd`,
capacity summary and notes; the next 8 eligible and 16 excluded rows are compact. A plan line stays under about 24 KB so the
gate's 1 MiB tail read (and `read_ledger`'s 8 MB tail) still reaches recent routes.

The tray replays that snapshot (`router_data.replay`, `theatre_source == "snapshot"`) and never calls `allocator.plan()` for it.
A ledger row without a snapshot is recomputed against current quota (`theatre_source == "recomputed"`) when its task is still
valid, otherwise shown as unreplayable; the UI says which.
