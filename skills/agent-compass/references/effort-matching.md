# Effort and the decision ledger

The router picks an effort by scoring each effort bundle like any other candidate: the fit of the model's four category scores at
that effort to the task's demand, minus the quota economy's price for the spend at that effort. Effort is not a function of
reasoning depth, and no gate or floor decides it.

## Model side: effort curves

`effort_curves[model]` per model, written by `calibration/efforts.py` from the Artificial Analysis effort variants:

| Field | Meaning |
| :--- | :--- |
| `ability_mult` | the model's Intelligence Index at that effort over its index at High; `allocator.five_axis_effort_ratio` scales the thinking categories (divergence, global rigor, module precision) by it, capped at 1 |
| `calibration_source` | where the numbers came from; every shipped curve names one |

A model whose index is not listed by effort has no measured response: its categories are not scaled. Opus Max takes about 2.6x
the time of High (Artificial Analysis Time/Task, 25.3 vs 9.6 min) for about 13% more index points, so it wins only when the
demand is not met below it and the economy lets it. `vision_tools` does not scale with effort.

The time and spend of an effort are not an effort curve: the hours a task takes at an effort are the Artificial Analysis Time/Task
at that effort (`five-axis-scores.json` `duration_multiplier.by_effort`, as a multiple of the anchor's at High), and the spend is
those hours at the model's `usd_per_hour`. A model Artificial Analysis lacks takes its arena.ai Agent Time/Task moved onto that
scale by a cited fit; a model with no time of its own takes its nearest same-series measured sibling's per-effort time
(`series`); an effort between two of the model's own efforts takes the fleet's median position between them on the log-time scale,
t_lower x (t_upper / t_lower)^p (`fleet_effort_position`, over the models that list all three efforts, with its n and IQR; the
model's endpoints are kept, p is held to [0, 1] and marked if that binds); an effort outside its listed range takes the nearest
same-series sibling's ratio, else the fleet's median ratio from the nearest listed effort (`fleet_effort_ratio`, n and IQR cited),
never faster than a lower listed effort (`bounded_by` when that binds). Under 3 fleet models the effort has no time.
`basis_by_effort` names which. A model
with no sibling and no time, or an effort with no time, is out of automatic routing (a named dispatch still runs).

## The match

- A task that demands little of the thinking categories is met at a low effort and pays less quota; a task that demands a lot
  needs the effort whose scaled score reaches the target (`0.8 x demand / 3`) or accepts the shortfall penalty.
- Expiring quota makes the extra spend cheap (the economy prices it near zero close to the reset), so effort rises.
- `decision.task.max_recommended_effort` is the effort of the chosen bundle.

## Ledger snapshot

Every non-dry-run `plan` line in `router-ledger.jsonl` carries `decision_snapshot` (`allocator.decision_snapshot`): the task
demand, the scoring weights and the ranking. The best 6 eligible rows keep every component, `expected_pct`, `expected_usd`,
capacity summary and notes; the next 8 eligible and 16 excluded rows are compact. A plan line stays under about 24 KB so the
gate's 1 MiB tail read (and `read_ledger`'s 8 MB tail) still reaches recent routes.

The tray replays that snapshot (`router_data.replay`, `theatre_source == "snapshot"`) and never calls `allocator.plan()` for it.
A ledger row without a snapshot is recomputed against current quota (`theatre_source == "recomputed"`) when its task is still
valid, otherwise shown as unreplayable; the UI says which.
