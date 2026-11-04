# Effort matching and the decision ledger

Effort is not a function of reasoning depth. The router matches what the task **asks for** against what a model **delivers at an effort**, and pays for the burn.

## Task side: `required_ability`

`allocator.task_demand` (`decision.task.demand`) is continuous:

```
required = floor + critical_headroom + precision_headroom
         + property_span * (reasoning_depth / 4) * property_load
```

- `floor` is the hard gate for the work (a bundle below `floor + critical_headroom` is excluded). It is `scoring.ability_floor[difficulty]`, unless the route declares its own `ability_floor` for that difficulty in `capability_policy.routes`. Floors are measured, not judged. Each is the tier where expected success reaches 90% of the best model's, from public benchmarks mapped onto the tier scale. `scoring.ability_floor_basis` and each route's `ability_floor_basis` record the sources and dates.
- The floor is only the gate. Graded ability on work that is not minimum-sufficient counts from `scoring.ability_score_origin[difficulty]`, so lowering a measured floor does not flatten the difference between the models above it.
- An advertised model without a measured row takes `unmeasured_models.generic`: the median measured tier, below the hard floor, so it gets routine work only.
- `property_load` is the weighted mean of `code_complexity`, `context_scale`, `divergence`, `hallucination_risk`, `error_cost` (`scoring.demand.property_weights`). Depth multiplies it, so the same heavy properties ask more of a deep task than a shallow one.
- Work with no profile uses the difficulty index as depth and `default_load`.
- Tunables live under `scoring.demand` (`property_span`, `default_load`, `depth_coupling`, `shortfall_weight`, `sprint_effort_weight`, `precision_headroom`, `property_weights`).

## Model side: ability x effort curve

`effort_curves[model][effort]` per model:

| Field | Meaning |
| :--- | :--- |
| `ability` | tier delta from High (additive) |
| `ability_mult` | tier multiplier (replaces `ability` when present) |
| `burn` | burn relative to the 0.7 = High convention |
| `burn_mult` | burn multiplier over High (replaces `burn`) |
| `calibration_source` | where the numbers came from; every shipped curve names one |

Effective ability is `tier` shaped by the curve; quota cost is `unit_rate x burn_scale`. A tier means ability at High. Curves are Artificial Analysis effort variants at 0.0495 tier per index point (`calibration/efforts.py`). Opus Max costs about 2.9x High for about 8% more ability, so it is chosen only when the requirement is not met below it. A measured row whose LMArena listing is a higher-effort variant is moved to High when it is written (`calibration/apply_rows.py`), so the curve is not counted twice. Collect data per model and edit the curve; nothing else needs to change.

## The match

- Effort bundles are never tied to a depth: `when.max_reasoning_depth` and the binary effort lock are gone (a custom `when` rule is still honoured).
- `shortfall` penalises effective ability under `required_ability`; ability above it earns nothing and, past `target + span`, is charged as overqualification.
- `effort_penalty = extra_effort_penalty x (burn_scale - 1) x (1 - sunk_weight)`: near a reset with surplus quota the extra burn is waived, so effort rises (`sprint_quality`).
- Result: light work lands on medium/high, demanding work on xhigh/max, expiring surplus pushes effort up. `decision.task.max_recommended_effort` is the lowest effort that saturates the requirement for the chosen model.

## Ledger snapshot

Every non-dry-run `plan` line in `router-ledger.jsonl` carries `decision_snapshot` (`allocator.decision_snapshot`): the task demand, scoring weights and opportunity-exchange numbers, and the ranking. The best 6 eligible rows keep every component, `expected_pct`, `expected_usd`, capacity summary and notes; the next 8 eligible and 16 excluded rows are compact. A plan line stays under about 24 KB so the gate's 1 MiB tail read (and `read_ledger`'s 8 MB tail) still reaches recent routes.

The tray replays that snapshot (`router_data.replay`, `theatre_source == "snapshot"`) and never calls `allocator.plan()` for it. Only legacy rows without a snapshot are recomputed against current quota (`theatre_source == "recomputed"`), and the UI says so.
