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
