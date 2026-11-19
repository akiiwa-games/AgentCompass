"""Category scores, usd_per_hour, duration and plan capacity from `references/public-measurements.json`.

`python -X utf8 five_axis.py [--distrust SOURCE ...] [--measurements PATH] [--out PATH] [--accept-panel-change]`

Writes `references/five-axis-scores.json`, the only model scoring the router reads (axis_scores.five_axis_overlay). The
user's taxonomy (2026-11-11): four categories, each made of facets, each facet read from public boards; the router
scores on the four categories and the quota economy together. Nothing else scores a model: the tier is not a score.

| category | facet <- boards | combination |
|---|---|---|
| creativity | creative_writing <- lmarena.creative_writing | the facet |
| coding | execution <- arena.agent_code, aa.terminalbench_4, aa.frontiercode_2; faithfulness <- arena.agent_alignment | min |
| aesthetics | design <- lmarena.webdev, designarena.website, designarena.3d, designarena.svg; image_recognition (shared) | per level |
| tool_operation | responsiveness <- aa.ttfa (-ln seconds); image_recognition <- aa.mmmu_pro, lmarena.vision | per level |

- Scale (measurements.py): every board is a panel standard score, z = (x - x_anchor) / sd_panel, the anchor (Opus 5.5)
  at High 0 and one unit one standard deviation over the panel (the fleet models on every scoring board that lists all six
  anchor rows; its n is written to `anchor.panel_n`). No bridging, no kind conversion, no cap.
- Linking and effort shifting (`board_curves`, `link`): Artificial Analysis capability boards are linked onto the panel
  scale first and kept strictly to each model's own listed efforts (never imputed from siblings or fleet medians). Each
  non-AA board chooses one fixed AA reference board by Pearson correlation over the panel, links its High values across
  parallel boards (full-panel SD at High = 1, anchor at High = 0; partial-panel boards mean-sigma linked onto the full
  panel core), and then expands other efforts using the model's own linked AA deltas without scale distortion.
- No cross-board effort stitching (`facet_value`): multi-board facets combine common efforts shared by every board of
  the facet; when no effort is common to all boards, at most one effort (High if present) is kept and marked
  `measured_partial`.
- No inversion along effort (`bound_curve`, owner 2026-11-18): every curve of one model, board reading, facet, estimate,
  derived or filled value and Time/Task, is bounded by `user_scores.bound_along_effort` in its direction
  (`user_scores.rises_with_effort`: an ability facet never decreases as effort rises, `tool_operation.responsiveness` never
  rises, Time/Task never shortens). High is the anchor and never moves (a curve without High is anchored at its effort
  nearest High); a lower effort is capped by the next higher one, a higher effort floored by the next lower one. A board
  reading the bound moves is no longer a measurement: the facet lists the efforts that still hold one in
  `measured_efforts`.
- High from one-sided effort data (`fill_high`, user 2026-11-12), after linking and before the facet's bound, never over
  a measured value: measured on both sides of High, High is the linear interpolation in effort rank (the model's own
  ladder from accounts.json, else low < medium < high < xhigh < max) between the nearest measured efforts, status
  `interpolated`; measured on one side only, High is the nearest measured effort plus the panel's median own step between
  the two efforts on that facet (`effort_steps`, n >= 2), status `estimated`, method `panel_effort_step`. Targets read
  measured and `interpolated` High rows only; no estimate enters them.
- Every other effort (`fill_efforts`, owner 2026-11-18: no missing value), last, on every facet that has a value at some
  effort (measured, estimated, derived): the same two methods, own-curve interpolation between the facet's own efforts
  (`own_effort_interpolation`) and, outside them, its nearest own effort plus the panel's median own step
  (`panel_effort_step`), then the bound with the facet's own efforts pinned, so a filled value never moves one that was
  there. Each filled effort is listed in the facet's `filled_efforts` with its method; an effort the panel gives no step
  for (fewer than two panel models measured at both efforts) stays empty and is listed in `effort_fill_refused`.
  `effort_steps` is published so a hand-written score fills the same way (user_scores._manual_by_effort).
- Per-level facet categories (`LEVEL_FACETS`): `aesthetics` asks `design` at level 1 and `min(design, image_recognition)`
  at levels 2 and 3 (sharing `tool_operation.image_recognition`); `tool_operation` asks no ability at level 1 (its
  image-input modality check sits at level 3), `responsiveness` at level 2, and `min(responsiveness, image_recognition)`
  at level 3.
- A facet a model lacks is estimated: the nearest relatives' value (series.groups: for an ability facet the maker's
  same-size rows, for responsiveness its own line; series.nearest, the median of a tied group) moved along the facet's fit on the tier by the tier difference, else the fit at the model's tier, in the order
  `estimation_order` declares (calibration/README.md). An estimate fit is accepted only when
  its slope is positive (`b > 0`) and its leave-one-out skill beats the panel mean (`skill > 0`); failing fits go to
  `rejected_fits`, leaving models without a measured sibling `missing` (mode B) and setting sibling shift to `0`.
- Downward from the larger model (`tier_down`, owner 2026-11-18): an ability facet a small row still lacks after that
  (no measurement, no same-size sibling, no fit) is its larger model's (series.tier_up: the same version one size up,
  declared in `families`) moved by the gap the two show on the facets both have, gap = mean over those facets of
  (small - large) at High over the facets both measured or interpolated (else, `gap_basis: estimated`, over the facets
  both have any value for, never a `tier_down` one), and never above the larger model's value at any effort:
  value(e) = large(e) + min(gap, 0). No shared facet: no value. Status `estimated`, method `tier_down`. Only for
  `tier_up_version: same`: a row's own gap is not carried across versions. Ability facets only.
- Size step (`size_step`, owner 2026-11-18): a facet a small row with `tier_up` (same or nearest version) still lacks is its
  larger model's moved by the fleet's size step (`size_step_table`): one value per declared same-version pair both
  measured at High (small - large; an ability facet with fewer than MIN_SIZE_PAIRS pairs: per pair, the mean over the
  ability facets that pair measured, `ability_pooled`), and the step is their median only when there are at least
  MIN_SIZE_PAIRS pairs and they all point one way (`pair_agreement`); otherwise it is zero and the cell says why
  (`directions disagree`, `fewer than 2 pairs`). Time/Task the same on the log ratio time(small) / time(large) at High
  (`time_size_step`). An ability value is then never above the larger model's (`min(step, 0)`, `bound_across_size`).
- Edge efforts (`fill_edge`, `edge_step_table`, `durations`; `EDGES`, user_scores): `minimal` and `ultra` from the
  boards' step next to them, `none` from the boards' own pairs at none (`fleet_off_step`), every listing a board holds at
  both efforts counted (`edge_pairs`), an ability facet without a pair the other ability facets' median, Time/Task clipped to the fleet's
  measured range; the rule is stated in calibration/README.md (Edge efforts).
- One operating point (`operating_points`, `flatten_point`): calibration/README.md.
- Own ability median (`own_ability_median`, owner 2026-11-18), last and coarsest: an ability facet still without a value is,
  at each effort, the median of the model's own ability facets there (at least MIN_OWN_FACETS of them, all on the panel-z
  scale); status `estimated`, with the facets used.
- `default` (accounts.json `effort_readings`): an effort-less board listing is the model's one configuration, stored at the
  effort the declaration names (High), and a launch at `default` reads that value (`effort_readings` in the output).
- Order of methods: calibration/README.md (one statement).
- Level scale and targets (`anchor.targets`, `targets`; owner 2026-11-18): a facet's three reference points all come from
  the panel's measured and interpolated High values: level 1 `t1` the panel's weakest, level 2 `t2` its median, level 3
  `t3` its maximum (the row names the panel member that holds it). The anchor is the origin of `z` (0) and is level 3 only
  where it is the panel's best; it is not part of the definition. A model above the panel maximum has a level above 3
  (user_scores.level_to_z extrapolates by `t3 - t2`). A category value (creativity, coding) has its own triad on the same
  definition. `zero` is the level-1 target, the fit's zero point. A scale needs `t1 < t2 < t3`: a facet whose panel gives
  none is a build error (`check_scales`: `degenerate level scale`), not something to accept. `aesthetics` and `tool_operation` ask per
  facet (`by_facet`): their rows carry the same targets, except that `tool_operation.responsiveness` at level 3 asks what it
  asks at level 2, the panel median, because a level-3 task adds the image-recognition requirement to the responsiveness
  one (`LEVEL3_ASKS_LEVEL2`; a statement about what tasks demand, so the facet's own scale still ends at its panel
  maximum). `continuity` is t3 - t2 (per-facet categories: the largest positive t3 - t2 across level-3 facets). Each
  level-3 row (category, facet, and `by_facet` entries) carries its `ceiling`: the highest measured or interpolated High
  value among the catalogued models, with the model that sets it and its status (owner, 2026-11-13). A level-3 demand is
  capped there instead of at t3; levels 1 and 2 stay capped at their target.
- Panel guard (`panel_change`): the declared panel (accounts.json `calibration_panel.members`, measurements.py) is
  compared with the panel of the five-axis-scores.json being replaced, so only an edit of the declaration changes it. When the members differ, the run prints the added and removed models and the old and new `sd_panel` per
  board, t1, t2, t3 and ceiling per category and facet, and writes nothing (`PanelChangeRefused`) unless the change is
  accepted (`--accept-panel-change`); an accepted change is recorded in `anchor.panel_change` and carried forward.
- Checks: structural ones drop a listing (a variant tag that is not an effort, a raw value out of range, one listing
  matched to two models, a duplicate board); statistical ones only note (`checks`).
- The tier (calibration/tiers.py) is the model's overall level at High relative to the anchor (`tier.arena_overall`,
  else `tier.aa_index`, else the nearest ability relative). Its one use is estimating a facet a model lacks.
- A row that `accounts.json` `same_model` declares to be the very model another row names (a cited vendor statement with
  its `url`, or a dated `kind: "maintainer"` declaration) takes that model's facets as they stand, measured or
  estimated (method `same_model`), for the categories the row `carries`, plus its Time/Task (`duration`) and
  `usd_per_hour` when carried. One that also declares `speed` is a speed variant (`speed_declaration`,
  `speed_variants`): calibration/README.md.
- usd_per_hour has one basis (rounds 8-9, user 2026-11-09 "全部都用arena"): `arena.agent_sessions` (arena.ai Agent / Overall
  real sessions, Cost/Task P50 / Time/Task P50 x 60, kind `community`), each source's value at High (else its effort-less
  listing, else the median of its listed efforts). For a model arena does not list with cost and time, the labelled
  option B (`derived.community_mix`, `fallback: True`: AA's measured output speed per hour priced at the community token
  mix at list prices); a model with a same-line relative that has a direct value takes that relative's times the ratio
  of the two cited API list prices over the full community token mix, both read on one price list where one lists both
  (`pair_prices`; `convert_usd`, `converted`), else, the same way,
  its larger model (series.tier_up) when that has a direct value (`relation: size`). A value still resting on option B
  alone is put on the arena-sessions scale by the fleet's median ratio of the two sources over the rows that have both
  (`scale_fallback_usd`, `on_primary_scale` with n and spread, `estimated`).
- Plan capacity comes down a ladder (rounds 4-5, user 2026-11-09: "都用社区真实使用量 且保持最新"; capacity is never
  left unknown), per window of each plan in plan-templates.json, recording the rung.
- Each window's reference-model hours are its capacity over that model's usd_per_hour.
- economy is priced by the router at run time from usd_per_hour, the duration and the window's capacity and recovery.
"""
import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import measurements
import series
import sources

sys.path.insert(0, str(sources.REPO / 'scripts'))
import data_age  # noqa: E402
import user_scores  # noqa: E402

OUT = sources.REPO / 'references' / 'five-axis-scores.json'
PLANS = sources.REPO / 'references' / 'plan-templates.json'
# /3 (2026-11-13): every level-3 row carries `ceiling`, the anchor block `sd_panel` and `panel_change`.
# /4 (2026-11-17): `aesthetics` evaluates per-level facets (`design` at L1; `min(design, image_recognition)` at L2/L3).
# /5 (2026-11-17): renames the creative category from `divergence` to `creativity`.
# /6 (2026-11-18): level 3 of every scale is the panel maximum, no longer the anchor (`statistic`, `model` of a level-3
# row; no `raised_to_level_below`), so a hand-written level means another value than under /5.
# /7 (2026-11-18): every facet has a value at every effort the panel steps reach (`filled_efforts`, `measured_efforts`,
# top-level `effort_steps` and `effort_fill_refused`, replacing `high_fill_refused`), curves bounded with High as the
# anchor instead of pooled, the `tier_down` estimate. Levels mean what they meant under /6.
# /8 (2026-11-18): the `minimal` effort (Low plus the fleet's step below it, `effort_steps.<facet>.low.minimal`, and
# `effort_steps.time_per_task`), the `size_step` estimate and the top-level `size_steps` table. Levels unchanged.
# /9 (2026-11-18): `ultra`, the declared `effort_readings` and size steps per pair. Levels unchanged.
# /10 (2026-11-18): the `none` effort, one more edge step below `minimal` (`effort_steps.<facet>.minimal.none`,
# `effort_steps.time_per_task.minimal.none`). Levels unchanged.
# /11 (2026-11-19): measured `none` readings; `none` from the fleet's own pairs (`effort_steps.<facet>.lowest_reasoning.none`,
# method `fleet_off_step`), operating-point rows, `clipped_to_fleet_range`, fit `residual_sd` / `loo_rmse`, relatives by
# declared version distance. Levels unchanged.
# /12 (2026-11-19): facts with their dates only, so a rebuild on another day writes the same file: `source_dates` replaces
# `source_ages` (no `age_days` / `stale`), capacity rows carry `as_of` (derived entries `rests_on_as_of`) in place of
# `age_days`, no `stale` status, no `stale_source` errors and no `capacity_stale_days`; age and staleness are the
# reader's (scripts/data_age.py). Within /12 before its first release (round 6): every effort read on every board, edge
# steps from the boards' listings (`sample`, `boards`; method `other_facets_median`), speed variants, relatives by maker,
# line and size, fleet fits frozen per release (a model entry's rows, `public-measurements.json` `model_entries`, scored by
# the release's fits and in none), speed without a reading (methods `declared_response_floor`, `cross_line_extrapolation`,
# `cross_line_median`; `estimation_order.speed_cross_line_before_fit`). Levels unchanged.
SCHEMA = 'agent-compass.five-axis-scores/12'
AXES = measurements.AXES
EFFORTS = user_scores.MEASURED_EFFORTS                   # the efforts boards measure and the panel steps reach
LADDER = user_scores.EFFORTS                              # every effort a curve carries: the edge efforts too (fill_edge)
EDGES = user_scores.EDGES     # {edge effort: (its neighbour toward the measured ones, (measured end, next measured))}
MIN_SIZE_PAIRS = 2      # same-version (small, larger) pairs a size step needs, as the panel effort step (n >= 2)
MIN_OWN_FACETS = 2      # ability facets a model's own median needs (a median of one value is that value, not a median)
ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
AESTHETICS_LEVEL_FACETS = {1: ('design',), 2: ('design', 'image_recognition'), 3: ('design', 'image_recognition')}
# The facets each tool_operation level asks for (user, 2026-11-11): level 1 no ability beyond the image-input check of
# level 3, level 2 responsiveness, level 3 responsiveness and image recognition; their `min`.
TOOL_LEVEL_FACETS = {1: (), 2: ('responsiveness',), 3: ('responsiveness', 'image_recognition')}
LEVEL_FACETS = {'aesthetics': AESTHETICS_LEVEL_FACETS, 'tool_operation': TOOL_LEVEL_FACETS}
# Facets whose level-3 task demand is the level-2 one (owner 2026-11-18): a tool_operation level-3 task adds image
# recognition to the responsiveness a level-2 task asks, not more responsiveness. A statement about demand; the facet's
# own level scale (`anchor.targets.<category>.facets`) is the panel's (min, median, maximum) like every other.
LEVEL3_ASKS_LEVEL2 = (('tool_operation', 'responsiveness'),)
FACET_OWNER = {
    'design': ('aesthetics', 'design'),
    'responsiveness': ('tool_operation', 'responsiveness'),
    'image_recognition': ('tool_operation', 'image_recognition'),
}
# Window -> the model whose hours count the window's capacity (the plan's own flagship on that pool).
REFERENCE_MODELS = {
    'claude': {'five_hour': 'claude-opus-5-5', 'seven_day': 'claude-opus-5-5', 'weekly_fable': 'claude-fable-5-1'},
    'codex': {'usage': 'gpt-6.1-sol', 'five_hour': 'gpt-6.1-sol'},
    'pi': {'usage': 'gpt-6.1-sol'},
    'antigravity': {'gemini-weekly': 'gemini-4-argon-high', 'gemini-5h': 'gemini-4-argon-high',
                    '3p-weekly': 'claude-opus-5-5', '3p-5h': 'claude-opus-5-5'},
}
TIER_DIRECT = ('tier.arena_overall',)
TIER_OTHER = ('tier.aa_index',)     # a non-arena overall board interpolated onto that scale; no other source is a tier
# The statuses whose High value enters the panel targets (user 2026-11-12): measured rows and own-curve interpolation.
TARGET_STATUSES = ('measured', 'measured_partial', 'interpolated')
MIN_FIT_POINTS = 8      # models a type-score-on-tier fit needs (review F1: one public point is not a fit)


def mean(xs):
    return round(float(np.mean(xs)), 4) if xs else None


def rises(key):
    """True when facet `key` ((category, facet)) never decreases as effort rises (user_scores.rises_with_effort)."""
    return user_scores.rises_with_effort(*key)


def edge_method(edge):
    """The method name of an edge effort's fill: reasoning off (`none`, from the fleet's own readings at none)
    `fleet_off_step`, otherwise below the measured ladder `fleet_lowest_step`, above it `fleet_highest_step`."""
    if EDGES[edge][1][0] == edge:
        return 'fleet_off_step'
    return 'fleet_lowest_step' if LADDER.index(edge) < LADDER.index(EFFORTS[0]) else 'fleet_highest_step'


ESTIMATION_ORDER = ('measured', 'panel_effort_step', 'sibling_shift up to the declared version distance', 'tier_fit',
                    'sibling_shift beyond it', 'tier_down', 'size_step same version', 'fleet edge step',
                    'own_ability_median', 'size_step nearest version')


def estimation_order(acc):
    """accounts.json `estimation_order`: the order of the estimation methods (the code's `ESTIMATION_ORDER`, refused when
    the data declares another) and `sibling_first_up_to_distance`, the version distance up to which a relative's estimate
    comes before the tier fit; its evidence is the leave-one-out table in the declaration (audit/estimation_loo.py)."""
    doc = acc.get('estimation_order')
    if (not isinstance(doc, dict) or tuple(doc.get('order') or ()) != ESTIMATION_ORDER
            or not isinstance(doc.get('sibling_first_up_to_distance'), int) or not str(doc.get('evidence') or '').strip()
            or not isinstance(doc.get('speed_cross_line_before_fit'), bool)):
        raise ValueError('accounts.json estimation_order must declare the order ' + ', '.join(ESTIMATION_ORDER)
                         + ' with sibling_first_up_to_distance, speed_cross_line_before_fit and its evidence')
    return doc


def same_model_point(entry):
    """The effort of a `same_model` row's declared operating point on its model (`at_effort`, owner 2026-11-19: a ChatGPT
    effort that is the model at one level, with no effort inside it), or None. The declaration names the effort and is
    cited (dated maintainer or https url); an effort off the scale refuses the build."""
    point = (entry or {}).get('at_effort')
    if point is None:
        return None
    if not isinstance(point, dict) or point.get('effort') not in LADDER or not valid_public_declaration(point):
        raise ValueError(f"same_model at_effort {point!r}: an effort of {', '.join(LADDER)} with a citation (https url or "
                         'dated maintainer)')
    return point['effort']


def operating_points(acc):
    """{row: its `operating_point` declaration}: a model whose vendor documents no reasoning control (accounts.json
    `models.<id>.operating_point`, cited; `reasoning` says whether its one configuration reasons). Its one board listing
    is its only reading and every effort reads it, with no effort step."""
    out = {}
    for m, row in (acc.get('models') or {}).items():
        point = row.get('operating_point') if isinstance(row, dict) else None
        if point is None:
            continue
        if not valid_public_declaration(point) or not isinstance(point.get('reasoning'), bool):
            raise ValueError(f'models[{m!r}].operating_point needs a citation (https url or dated maintainer) and '
                             '`reasoning` true or false')
        out[m] = point
    return out


def point_listing(z, point, reads):
    """The key of `z` ({listed effort: value}) that is an operating point's one reading: the reasoning-off listing for a
    model that cannot reason, the listing without an effort for one that always reasons (the effort `default` reads,
    when only that is listed), else the model's only listing; None when the listings leave it open."""
    want = ('none',) if not point['reasoning'] else ('default', reads['default'])
    hit = next((k for k in want if k in z), None)
    return hit or (next(iter(z)) if len(z) == 1 else None)


def effort_readings(acc):
    """{effort name off the ladder: the measured effort it reads}, the accounts.json `effort_readings.reads` declaration
    (owner 2026-11-18): a board listing without an effort label is stored at that effort, and a launch at that name reads
    it. A build without the declaration stops."""
    reads = ((acc or {}).get('effort_readings') or {}).get('reads')
    if not isinstance(reads, dict) or not reads or not all(v in EFFORTS and k not in LADDER for k, v in reads.items()):
        raise ValueError('accounts.json effort_readings.reads must map each effort name off the ladder (`default`) to the '
                         f'measured effort it reads ({", ".join(EFFORTS)})')
    return dict(reads)


def pair_agreement(values):
    """The size step from its per-pair values (owner 2026-11-18): {'step', 'median', 'n', 'agree', 'reason'?}. The step is
    the median when there are at least MIN_SIZE_PAIRS pairs and they all point one way (none above zero, or none below);
    otherwise it is zero, with the reason."""
    med = round(float(np.median(values)), 4) if values else None
    if len(values) < MIN_SIZE_PAIRS:
        return {'step': 0.0, 'median': med, 'n': len(values), 'agree': False, 'reason': f'fewer than {MIN_SIZE_PAIRS} pairs'}
    if min(values) < 0 < max(values):
        return {'step': 0.0, 'median': med, 'n': len(values), 'agree': False, 'reason': 'directions disagree'}
    return {'step': med, 'median': med, 'n': len(values), 'agree': True}


def bound_curve(by_effort, up=True, moved=None):
    """`by_effort` bounded along effort (user_scores.bound_along_effort, High the anchor), rounded to 4 decimals; `up` is
    the curve's direction. The efforts whose value the bound changed are added to the set `moved` when one is given."""
    out = {e: round(float(v), 4) for e, v in user_scores.bound_along_effort(by_effort, up).items()}
    if moved is not None:
        moved.update(e for e in out if out[e] != round(float(by_effort[e]), 4))
    return out


def bound_pinned(by_effort, pins, up=True):
    """`by_effort` bounded along effort with the efforts `pins` (the measured ones) kept as they are when they are in
    order (an estimate moves, a measurement does not); two measurements out of order fall back to the anchor rule
    (bound_curve: High, else the effort nearest it, kept and the others bounded from it)."""
    try:
        return {e: round(float(v), 4) for e, v in user_scores.bound_along_effort(by_effort, up, pins).items()}
    except ValueError:
        return bound_curve(by_effort, up)


def effort_floor(rec, e):
    """The lowest value the record's rising curve can take at effort `e` without moving a measurement: its highest
    measured value at `e` or below (None when it has none)."""
    by, i = rec.get('by_effort') or {}, LADDER.index(e)
    lo = [by[x] for x in rec.get('measured_efforts') or () if x in by and LADDER.index(x) <= i]
    return max(lo) if lo else None

SPEED_KEY = ('tool_operation', 'responsiveness')


def speed_declaration(entry):
    """The `speed` of a `same_model` entry (owner 2026-11-19, round 6 item 1): the row is a speed variant of its declared
    model (same weights, its own speed and price). {'ratio': the vendor's stated speed ratio (a stated range [lo, hi] read
    as its geometric mean), or None when the vendor states none; 'stated': the ratio as declared; the citation}. None for
    an entry without `speed`; a malformed one stops the build."""
    sp = (entry or {}).get('speed')
    if sp is None:
        return None
    stated = sp.get('ratio') if isinstance(sp, dict) else None
    ok_range = (isinstance(stated, list) and len(stated) == 2 and all(isinstance(x, (int, float)) for x in stated)
                and 1 < stated[0] <= stated[1])
    ok_one = isinstance(stated, (int, float)) and not isinstance(stated, bool) and stated > 1
    if not isinstance(sp, dict) or not valid_public_declaration(sp) or not str(sp.get('statement') or '').strip() \
            or not (stated is None or ok_range or ok_one):
        raise ValueError('same_model speed needs a citation (https url or dated maintainer), a statement and `ratio`: '
                         'a number above 1, a stated range [lo, hi] above 1, or null when the vendor states none')
    ratio = math.sqrt(stated[0] * stated[1]) if ok_range else stated
    return {'ratio': round(float(ratio), 4) if ratio is not None else None, 'stated': stated,
            **{k: sp[k] for k in ('kind', 'url', 'as_of', 'statement') if sp.get(k)}}


def valid_public_declaration(entry):
    """True when `entry` carries an https URL or is a dated `kind: "maintainer"` declaration."""
    if not isinstance(entry, dict):
        return False
    url = entry.get('url')
    if isinstance(url, str) and url.startswith('https://'):
        return True
    return entry.get('kind') == 'maintainer' and bool(ISO_DATE.match(str(entry.get('as_of') or '')))


def source_value(entries):
    """One source's figure: High, else its effort-less listing, else the median of its listed efforts."""
    by_effort = {e.get('effort') or 'default': e['value'] for e in entries}
    if 'high' in by_effort:
        return by_effort['high'], 'High listing'
    if 'default' in by_effort:
        return by_effort['default'], 'the only listing (no effort named)'
    listed = {k: v for k, v in by_effort.items() if k in EFFORTS} or by_effort
    return round(statistics.median(listed.values()), 3), f'median of the listed efforts {sorted(listed)}'


def pick_usd(entries):
    """usd_per_hour from one source's listings (`entries`, all of the first entry's source): its value at High, else its
    effort-less listing, else the median of its listed efforts."""
    if not entries:
        return {'value': None, 'status': 'missing', 'sources': {}, 'merge': None}
    src = entries[0]['source']
    rows = [e for e in entries if e['source'] == src]
    value, how = source_value(rows)
    cited = {'value': value, 'how': how, 'kind': rows[0].get('kind'), 'as_of': rows[0].get('as_of'),
             'url': rows[0].get('url'), 'method': rows[0].get('method'),
             'by_effort': {r.get('effort') or 'default': r['value'] for r in rows}}
    if rows[0].get('sessions') is not None:
        cited['sessions'] = rows[0]['sessions']
    return {'value': value, 'status': 'ok', 'merge': f'{src}: {how}', 'used': [src], 'sources': {src: cited}}


def pick_image_input(entries):
    """Image-input modality from cited public sources or dated maintainer declarations: a public vision benchmark listing
    wins first, then an explicit maintainer/vendor modality declaration, then OpenRouter modality metadata; without any
    source, status is `unknown` (mode B when tool_operation is 3)."""
    if not entries:
        return {'value': None, 'status': 'unknown', 'source': None, 'kind': None, 'sources': [],
                'reason': 'no cited public modality source'}
    bench_yes = [e for e in entries if e.get('kind') == 'benchmark' and e.get('value') is True]
    if bench_yes:
        e0 = bench_yes[0]
        return {'value': True, 'status': 'yes', 'source': e0['source'], 'kind': 'benchmark',
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'method': e0.get('method'),
                'sources': [e['source'] for e in bench_yes]}
    declared = [e for e in entries if e.get('source') in (
        'maintainer.image_input', 'vendor.image_input', 'maintainer.text_only', 'vendor.text_only'
    ) and isinstance(e.get('value'), bool)]
    if declared:
        e0 = declared[0]
        val = bool(e0['value'])
        return {'value': val, 'status': 'yes' if val else 'no', 'source': e0['source'], 'kind': e0.get('kind'),
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'statement': e0.get('statement'),
                'method': e0.get('method'), 'sources': [e['source'] for e in declared]}
    yes = [e for e in entries if e.get('value') is True]
    if yes:
        e0 = yes[0]
        return {'value': True, 'status': 'yes', 'source': e0['source'], 'kind': e0.get('kind'),
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'method': e0.get('method'),
                'sources': [e['source'] for e in yes]}
    no = [e for e in entries if e.get('value') is False]
    if no:
        e0 = no[0]
        return {'value': False, 'status': 'no', 'source': e0['source'], 'kind': e0.get('kind'),
                'as_of': e0.get('as_of'), 'url': e0.get('url'), 'method': e0.get('method'),
                'sources': [e['source'] for e in no]}
    return {'value': None, 'status': 'unknown', 'source': None, 'kind': None, 'sources': [],
            'reason': 'no cited public modality source'}


def pick_fast_mode(model, entries, models=None):
    """Public fast-mode price and speed multipliers (round 11, user 2026-11-10)."""
    if entries:
        e0 = entries[0]
        val = e0.get('value') or {}
        return {'status': 'ok', 'price_multiplier': val.get('price_multiplier'),
                'speed_multiplier': val.get('speed_multiplier'), 'source': e0['source'],
                'kind': e0.get('kind', 'vendor'), 'as_of': e0.get('as_of'), 'url': e0.get('url'),
                'pricing_url': e0.get('pricing_url'), 'fast_price': e0.get('fast_price'),
                'standard_price': e0.get('standard_price'), 'method': e0.get('method')}
    if bool(((models or {}).get(model) or {}).get('fast_capable')):
        return {'status': 'missing', 'price_multiplier': None, 'speed_multiplier': None, 'source': None,
                'reason': 'provider does not publish a fast-mode burn or speed multiplier'}
    return {'status': 'unsupported', 'price_multiplier': None, 'speed_multiplier': None, 'source': None}


PRICE_SOURCES = ('aa.price', 'openrouter.price', 'vendor.price')      # the order a list price is read in


def pick_price(entries):
    """Public list price (USD per million tokens) for converting local token counts (e.g. opencode.db) into USD."""
    for src in PRICE_SOURCES:
        for e in entries or ():
            if e.get('source') == src and isinstance(e.get('value'), dict):
                v = e['value']
                if (v.get('output') or 0) > 0 or (v.get('input') or 0) > 0:
                    return {'value': v, 'source': src, 'kind': e.get('kind', 'vendor'),
                            'as_of': e.get('as_of'), 'url': e.get('url'), 'listed_as': e.get('listed_as')}
    return None


def without_entries(groups, entries):
    """`groups` ({row: [relative, ...]}) with every model-entry row taken out of the relative lists (owner 2026-11-19,
    round 6 item 8): a row rests only on the release snapshot's rows, so adding a model changes no other row."""
    return {k: [j for j in v if j not in entries] for k, v in groups.items()}


def release_same_model(acc, entries):
    """The valid `same_model` declarations whose declared model is a release snapshot row (round 6 item 8)."""
    return {k: v for k, v in series.same_model(acc).items() if valid_public_declaration(v) and v.get('model') not in entries}


def release_tier_up(acc, entries):
    """series.tier_up without a larger model that is a model-entry row (round 6 item 8)."""
    return {m: r for m, r in series.tier_up(acc).items() if r['model'] not in entries}


class Scorer:
    def __init__(self, meas, acc, distrust=()):
        self.meas, self.acc = meas, acc
        self.distrust = set(distrust)
        # the rows a model entry gave (refresh --model, merge_model_rows): scored by the release's fits, never in one
        self.entries = frozenset(meas.get('model_entries') or ())
        self.readings = effort_readings(acc)
        self.models = {k: v for k, v in acc['models'].items() if isinstance(v, dict)}
        # declared response floors (`response_time_source`): a row's one launch path, round 6
        self.measured_inversions = []      # bound_across_size: two measurements out of the size order
        self.response_floor = {m: spec['response_time_source'] for m, spec in self.models.items()
                               if isinstance(spec.get('response_time_source'), dict)}
        self.points = operating_points(acc)

    def trusted(self, entries):
        return [e for e in entries or () if e['source'] not in self.distrust]

    def tier_records(self, rows):
        """{model: {'value', 'method', 'sources', ...}}: arena Overall at High, else non-arena overall board aligned onto
        that scale at High, else the nearest same-series sibling's tier, else value None."""
        out = {}
        for m, r in rows.items():
            entries = self.trusted(r['fields'].get('tier'))
            direct = [e for e in entries if e['source'] in TIER_DIRECT]
            other = [e for e in entries if e['source'] in TIER_OTHER]
            chosen, method = (direct, 'arena_overall') if direct else (other, 'other_overall')
            if chosen:
                cite = [{k: e.get(k) for k in ('source', 'value', 'as_of', 'url', 'listed_as', 'neighbours', 'n', 'fit',
                                               'residual_sd', 'loo_rmse') if e.get(k) is not None}
                        for e in chosen]
                out[m] = {'value': round(float(np.mean([e['value'] for e in chosen])), 4), 'method': method, 'sources': cite,
                          **({'status': 'estimated'} if method == 'other_overall' else {})}
        direct_tier = {m: t['value'] for m, t in out.items()}
        sibs = without_entries(series.groups(self.acc, 'ability'), self.entries)
        same = release_same_model(self.acc, self.entries)
        for m in rows:
            if m in out:
                continue
            groups = series.nearest(m, [b for b in sibs.get(m, ()) if b in direct_tier], sibs.get(m, ()), self.versions,
                                    self.small, same, self.line_of, self.order['sibling_first_up_to_distance'])
            if groups:
                group = groups[0][1]
                out[m] = {'value': round(float(np.median([direct_tier[b] for b in group])), 4), 'method': 'sibling',
                          'base_model': group[0] if len(group) == 1 else group, 'version_distance': groups[0][0],
                          'sources': []}
            else:
                out[m] = {'value': None, 'method': 'none', 'sources': []}
        return out

    @staticmethod
    def fit(points, require_skill=False):
        """Linear fit of y on x with its leave-one-out record, or (None, why) under MIN_FIT_POINTS models, with one x
        for all, or (when `require_skill` is true) without a positive slope (`b > 0`) and positive leave-one-out skill
        (`skill > 0`, beating the leave-one-out mean predictor)."""
        if len(points) < MIN_FIT_POINTS:
            return None, f'{len(points)} models with both (a fit needs {MIN_FIT_POINTS})'
        x, y = np.array([p[0] for p in points]), np.array([p[1] for p in points])
        if float(np.ptp(x)) == 0.0:
            return None, 'every model has the same tier'
        b, a = np.polyfit(x, y, 1)
        if require_skill and b <= 0:
            return None, f'slope {b:.3f} is not positive'
        n = len(x)
        err = base = 0.0
        for i in range(n):
            keep = np.arange(n) != i
            if float(np.ptp(x[keep])) == 0.0:
                return None, 'every other model has the same tier'
            bi, ai = np.polyfit(x[keep], y[keep], 1)
            err += (y[i] - (ai + bi * x[i])) ** 2
            base += (y[i] - y[keep].mean()) ** 2
        skill = float(1.0 - err / base) if base > 0 else 0.0
        if require_skill and skill <= 0:
            return None, f"out-of-sample skill {skill:.3f} (leave-one-out error not below the mean predictor's)"
        return {'a': float(a), 'b': float(b), 'r': float(np.corrcoef(x, y)[0, 1]),
                'skill': skill, 'loo_rmse': float(np.sqrt(err / n)), 'n': n,
                'x_min': float(x.min()), 'x_max': float(x.max()), 'y_min': float(y.min()), 'y_max': float(y.max())}, None

    @staticmethod
    def predict(f, x):
        """The fit at tier x, x clamped to the tiers the fit saw and the value kept inside the range its measured points
        reach (round 6, round 058 review F2: a tier above the best sampled model is not extrapolated)."""
        x = float(np.clip(x, f['x_min'], f['x_max']))
        return round(float(np.clip(f['a'] + f['b'] * x, f['y_min'], f['y_max'])), 4)

    @staticmethod
    def fit_record(fid, f):
        return {'fit': fid, 'r': round(f['r'], 4), 'skill': round(f['skill'], 4), 'loo_rmse': round(f['loo_rmse'], 4),
                'n': f['n'], 'slope': round(f['b'], 4)}

    # --- boards -> facets ---------------------------------------------------------------------------------------
    def board_entries(self, fields):
        """{(category, facet): {source: entry}} of the trusted capability entries."""
        out = {}
        for a in AXES:
            for e in self.trusted(fields.get(a)):
                if e.get('facet') and isinstance(e.get('value'), (int, float)):
                    out.setdefault((a, e['facet']), {}).setdefault(e['source'], e)
        return out

    def aa_rows(self, entry, m=None):
        """{effort: z} of an Artificial Analysis entry of model `m`; an effort-less listing is the model's one
        configuration, stored at the effort `effort_readings` names (unless that effort is listed too); a reasoning-off
        listing is the reading at `none` (accounts.json effort_readings.labels). An operating point row (`operating_points`)
        reads its one listing (`point_listing`), keyed at the effort `default` reads."""
        z = entry.get('z_by_effort') or {}
        if m in self.points:
            k = point_listing(z, self.points[m], self.readings)
            return {self.readings['default']: z[k]} if k is not None else {}
        rows = {k: v for k, v in z.items() if k in LADDER or k in self.readings}
        for name, reads in self.readings.items():
            if name in rows:
                rows.setdefault(reads, rows.pop(name))
        return {k: v for k, v in rows.items() if k in LADDER}     # every effort of the one order (round 6 item 5)

    @staticmethod
    def _has_effort_curve(aa_map):
        return bool(aa_map and len(aa_map) >= 2 and float(np.ptp(list(aa_map.values()))) > 0)

    def _linked_aa_capability_boards(self, rows, panel):
        """({aa_source: {model: {effort: z_linked}}}, {aa_source: {model: high_z}}) linked on `panel` BEFORE shifting any
        non-AA board (R2-F5: strictly each model's own listed efforts; R2-F10: link before shifting). No pooling or monotone
        fit here: the bound along effort comes later, once, on the facet (`bound_curve`)."""
        anchor = measurements.ANCHOR
        meas_sources = self.meas.get('sources') or {}
        aa_raw = {}
        for m, r in rows.items():
            for e in self.trusted(r['fields'].get('intelligence_index')):
                if e.get('source') == 'aa.index' and isinstance(e.get('z_by_effort'), dict):
                    mapped = self.aa_rows(e, m)
                    if mapped:
                        aa_raw.setdefault('aa.index', {})[m] = mapped
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if src.startswith('aa.') and src != measurements.AA_TTFA:
                        mapped = self.aa_rows(e, m)
                        if mapped:
                            aa_raw.setdefault(src, {})[m] = mapped
                        elif isinstance(e.get('value'), (int, float)) and e.get('listed_effort') in (None, 'high'):
                            aa_raw.setdefault(src, {})[m] = {'high': float(e['value'])}
        # Center on anchor @ High and scale each AA board to panel SD = 1 (or shared-panel core SD for partial AA boards).
        aa_centered = {}
        for src, per_m in aa_raw.items():
            a0 = (per_m.get(anchor) or {}).get('high')
            aa_centered[src] = {
                m: {e: (v - a0 if a0 is not None else v) for e, v in curve.items()}
                for m, curve in per_m.items()
            }
        # Group AA capability boards by facet (or standalone for aa.index) so partial-panel AA boards get their exact link_scale.
        aa_scales = {}
        for src, per_m in aa_centered.items():
            high = {m: curve['high'] for m, curve in per_m.items() if 'high' in curve}
            if len(panel) >= 2 and all(m in high for m in panel) and (src in meas_sources or src == 'aa.index'):
                sd_h = statistics.stdev(high[m] for m in panel)
                aa_scales[src] = (1.0 / sd_h) if sd_h > 0 else 1.0
            else:
                # Partial-panel AA board (e.g. aa.mmmu_pro): scale against native full-panel sibling board in same facet if present.
                key = measurements.CAPABILITY_SOURCES.get(src)
                sib_vals = {}
                if key:
                    for m, r in rows.items():
                        for s2, e2 in (r['boards'].get(key) or {}).items():
                            if s2 != src and isinstance(e2.get('value'), (int, float)):
                                sib_vals.setdefault(s2, {})[m] = float(e2['value'])
                full_sibs = [s2 for s2, hm in sib_vals.items() if len(panel) >= 2 and all(m in hm for m in panel)]
                shared = [m for m in panel if m in high]
                if full_sibs and len(shared) >= 2:
                    core = {m: float(np.mean([sib_vals[s2][m] for s2 in full_sibs])) for m in shared}
                    sd_c = statistics.stdev(core[m] for m in shared)
                    sd_s = statistics.stdev(high[m] for m in shared)
                    aa_scales[src] = (sd_c / sd_s) if sd_s > 0 else 1.0
                elif len(shared) >= 2 and (src in meas_sources or src == 'aa.index'):
                    sd_s = statistics.stdev(high[m] for m in shared)
                    aa_scales[src] = (1.0 / sd_s) if sd_s > 0 else 1.0
                else:
                    aa_scales[src] = 1.0
        aa_linked, aa_high = {}, {}
        for src, per_m in aa_centered.items():
            sc = aa_scales[src]
            aa_linked[src] = {}
            for m, curve in per_m.items():
                scaled = {e: round(v * sc, 4) for e, v in curve.items()}
                bounded = bound_curve(scaled)
                aa_linked[src][m] = bounded
                if 'high' in bounded:
                    aa_high.setdefault(src, {})[m] = bounded['high']
        return aa_linked, aa_high

    def compute_effort_correlations(self, rows, aa_high, panel):
        """{non_aa_source: {'board', 'r', 'n', 'panel_n', 'candidates'}} choosing ONE fixed AA reference board per
        non-AA board by Pearson r over the panel (full-panel AA boards preferred over partial-panel subsets)."""
        non_aa = {}
        for m, r in rows.items():
            if m in self.entries:
                continue        # a fleet quantity: the release snapshot's rows only (round 6 item 8)
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if not src.startswith('aa.') and isinstance(e.get('value'), (int, float)):
                        non_aa.setdefault(src, {})[m] = float(e['value'])
        corrs = {}
        for src, vals in sorted(non_aa.items()):
            board_panel = [m for m in panel if m in vals]
            use_panel = len(board_panel) >= MIN_FIT_POINTS
            pool = board_panel if use_panel else list(vals)
            raw_cands = []
            for aa_src, avals in sorted(aa_high.items()):
                shared = [m for m in pool if m in avals]
                if len(shared) < MIN_FIT_POINTS:
                    continue
                x = np.array([vals[m] for m in shared], dtype=float)
                y = np.array([avals[m] for m in shared], dtype=float)
                if float(np.ptp(x)) <= 0 or float(np.ptp(y)) <= 0:
                    continue
                r_val = float(np.corrcoef(x, y)[0, 1])
                if r_val > 0:
                    raw_cands.append({
                        'board': aa_src,
                        'r': round(r_val, 4),
                        'n': len(shared),
                        'full_panel': use_panel and len(shared) == len(board_panel),
                    })
            if any(c['full_panel'] for c in raw_cands):
                cands = [c for c in raw_cands if c['full_panel']]
            else:
                cands = raw_cands
            cands.sort(key=lambda c: (-c['r'], -c['n'], c['board']))
            if cands:
                corrs[src] = {
                    'board': cands[0]['board'],
                    'r': cands[0]['r'],
                    'n': cands[0]['n'],
                    'panel_n': len(board_panel),
                    'candidates': cands,
                }
        return corrs

    def board_curves_and_link(self, rows, panel):
        """Link AA capability boards first, pick one fixed AA reference board per non-AA board over the panel, link all
        boards at High (full-panel SD at High = 1, anchor at High = 0), and shift non-AA efforts using the linked AA
        deltas without scale distortion."""
        aa_linked, aa_high = self._linked_aa_capability_boards(rows, panel)
        self.effort_correlations = self.compute_effort_correlations(rows, aa_high, panel)
        self._aa_cap_linked = aa_linked

        curves = {}
        pending_shifts = []
        for m, r in rows.items():
            out = curves.setdefault(m, {})
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if src.startswith('aa.'):
                        own_aa = self.aa_rows(e, m)
                        basis = {k: {'method': 'listed'} for k in own_aa}
                        has_multi = len(own_aa) >= 2 and float(np.ptp(list(own_aa.values()))) > 0
                        out.setdefault((a, f), {})[src] = {
                            'by_effort': dict(own_aa),
                            'effort_basis': basis,
                            'effort_shift': 'measured' if has_multi else 'single_aa',
                            'listed_as': e.get('listed_as'),
                        }
                        continue
                    listed = e.get('listed_effort')
                    native = self.aa_rows(e) if isinstance(e.get('z_by_effort'), dict) else {}
                    ref_eff = listed if listed in EFFORTS else 'high'
                    if not native:
                        native = {ref_eff: round(float(e['value']), 4)}
                    ref_info = self._fixed_reference_board(a, f, src)
                    own_ref = (aa_linked.get(ref_info['board']) or {}).get(m) if ref_info else None
                    shared_anchors = [k for k in native if own_ref and k in own_ref]
                    if ref_info and self._has_effort_curve(own_ref) and shared_anchors:
                        pre_by = dict(native)
                        if 'high' not in pre_by and 'high' in own_ref:
                            e0 = min(shared_anchors, key=lambda k: (abs(EFFORTS.index(k) - EFFORTS.index('high')), EFFORTS.index(k)))
                            pre_by['high'] = round(native[e0] + (own_ref['high'] - own_ref[e0]), 4)
                        how = {
                            'board': ref_info['board'],
                            'r': ref_info['r'],
                            'n': ref_info['n'],
                            'model': m,
                            'from_effort': ref_eff,
                            **({'same_facet': True} if ref_info.get('same_facet') else {}),
                        }
                        rec = {
                            'by_effort': pre_by,
                            'effort_shift': 'aa',
                            'effort_shift_basis': how,
                            'listed_as': e.get('listed_as'),
                            'listed_effort': listed,
                        }
                        out.setdefault((a, f), {})[src] = rec
                        pending_shifts.append((rec, dict(native), own_ref, shared_anchors, (a, f)))
                    else:
                        if not ref_info:
                            why = f'no correlated Artificial Analysis board with >= {MIN_FIT_POINTS} shared models'
                        elif not self._has_effort_curve(own_ref):
                            why = f"model {m} has no multi-effort Artificial Analysis curve on {ref_info['board']}"
                        else:
                            why = f"model {m} AA curve on {ref_info['board']} lacks reference effort {ref_eff}"
                        out.setdefault((a, f), {})[src] = {
                            'by_effort': dict(native),
                            'effort_shift': 'none',
                            'effort_shift_basis': why,
                            'listed_as': e.get('listed_as'),
                            'listed_effort': listed,
                        }

        for m, r in rows.items():       # a reading corrected by its board's drift says so in the cell (refresh.py)
            for (a, f), srcs in r['boards'].items():
                for src, e in srcs.items():
                    if 'drift_correction' in e and src in curves.get(m, {}).get((a, f), {}):
                        curves[m][(a, f)][src]['drift_correction'] = e['drift_correction']
        links = self.link(curves, panel)

        # Expand non-AA efforts from the linked anchor effort using uncompressed linked AA deltas, then bound every board's
        # curve in its facet's direction; a reading the bound moves is no longer a measurement (`bound_moved`).
        for rec, native, own_ref, shared_anchors, key in pending_shifts:
            by = dict(rec['by_effort'])
            base_effs = [k for k in ('high', *shared_anchors) if k in by and k in own_ref]
            if base_effs:
                for x in EFFORTS:
                    if x in own_ref and x not in native and x != 'high':
                        e0 = 'high' if 'high' in base_effs else min(
                            base_effs, key=lambda k: (abs(EFFORTS.index(k) - EFFORTS.index(x)), EFFORTS.index(k))
                        )
                        by[x] = round(by[e0] + (own_ref[x] - own_ref[e0]), 4)
            rec['by_effort'] = by

        for c in curves.values():
            for key, srcs in c.items():
                for rec in srcs.values():
                    moved = set()
                    # a reading off the measured ladder (none, minimal) as listed: the facet bounds it against the
                    # facet's own readings (facet_value), never against one board's alone
                    rec['off_ladder'] = {e: v for e, v in rec['by_effort'].items() if e not in EFFORTS}
                    rec['by_effort'] = bound_curve(rec['by_effort'], rises(key), moved)
                    rec['bound_moved'] = sorted(moved, key=LADDER.index)
        return curves, links

    def _fixed_reference_board(self, a, f, src):
        corr_entry = self.effort_correlations.get(src)
        if corr_entry:
            return corr_entry
        same_facet = [s for s in sorted(measurements.CAPABILITY_SOURCES)
                      if s.startswith('aa.') and s != measurements.AA_TTFA and measurements.CAPABILITY_SOURCES[s] == (a, f)
                      and s in (getattr(self, '_aa_cap_linked', None) or {})]
        if same_facet:
            s0 = same_facet[0]
            return {'board': s0, 'r': None, 'n': len(self._aa_cap_linked.get(s0) or {}), 'same_facet': True}
        return None

    def link(self, curves, panel):
        """Standardise full-panel boards on the panel's High rows (anchor @ High = 0, sd_panel @ High = 1), mean-mean link
        parallel full-panel boards, mean-sigma link secondary (partial-panel) boards onto the core over shared panel models,
        then re-center the facet on the anchor at High."""
        anchor = measurements.ANCHOR
        facets = sorted({key for c in curves.values() for key in c})
        links = {}
        for key in facets:
            srcs = sorted({src for c in curves.values() for src in c.get(key, {})})
            a0 = {src: ((curves.get(anchor) or {}).get(key) or {}).get(src, {}).get('by_effort', {}).get('high') for src in srcs}
            for c in curves.values():
                for src, rec in (c.get(key) or {}).items():
                    if a0.get(src) is not None:
                        rec['by_effort'] = {e: v - a0[src] for e, v in rec['by_effort'].items()}
            high = {src: {m: c[key][src]['by_effort']['high'] for m, c in curves.items()
                          if 'high' in (c.get(key) or {}).get(src, {}).get('by_effort', {})} for src in srcs}
            full = [src for src in srcs if len(panel) >= 2 and all(m in high[src] for m in panel)]
            meas_sources = self.meas.get('sources') or {}
            scales, offsets = {}, {}
            for src in full:
                if src in meas_sources:
                    sd_h = statistics.stdev(high[src][m] for m in panel)
                    sc = (1.0 / sd_h) if sd_h > 0 else 1.0
                    scales[src] = sc
                    if sc != 1.0:
                        for c in curves.values():
                            rec = (c.get(key) or {}).get(src)
                            if rec is not None:
                                rec['by_effort'] = {e: v * sc for e, v in rec['by_effort'].items()}
                        high[src] = {m: v * sc for m, v in high[src].items()}
                else:
                    scales[src] = 1.0
            if len(srcs) > 1 and full:
                means = {src: float(np.mean([high[src][m] for m in panel])) for src in full}
                grand = float(np.mean(list(means.values())))
                offsets = {src: grand - means[src] for src in full}
                core = {m: float(np.mean([high[src][m] + offsets[src] for src in full])) for m in panel}
                for src in srcs:
                    if src not in full:
                        shared = [m for m in panel if m in high[src]]
                        if len(shared) >= 2:
                            mu_c = float(np.mean([core[m] for m in shared]))
                            sd_c = statistics.stdev(core[m] for m in shared)
                            mu_s = float(np.mean([high[src][m] for m in shared]))
                            sd_s = statistics.stdev(high[src][m] for m in shared)
                            sc = (sd_c / sd_s) if sd_s > 0 else 1.0
                            off = mu_c - sc * mu_s
                            scales[src], offsets[src] = sc, off
                            for c in curves.values():
                                rec = (c.get(key) or {}).get(src)
                                if rec is not None:
                                    rec['by_effort'] = {e: v * sc for e, v in rec['by_effort'].items()}
                            high[src] = {m: v * sc for m, v in high[src].items()}
                        elif shared:
                            scales[src] = 1.0
                            offsets[src] = float(np.mean([core[m] - high[src][m] for m in shared]))
            else:
                for src in srcs:
                    scales.setdefault(src, 1.0)
                    offsets.setdefault(src, 0.0)
            own = [src for src in ((curves.get(anchor) or {}).get(key) or {}) if anchor in high.get(src, {})]
            center = float(np.mean([high[src][anchor] + offsets.get(src, 0.0) for src in own])) if own else 0.0
            for c in curves.values():
                for src, rec in (c.get(key) or {}).items():
                    off = offsets.get(src, 0.0) - center
                    rec['link_scale'] = round(scales.get(src, 1.0), 4)
                    rec['link_offset'] = round(off, 4)
                    rec['by_effort'] = {e: round(v + off, 4) for e, v in rec['by_effort'].items()}
            links['.'.join(key)] = {
                'boards': srcs,
                'scales': {k: round(v, 4) for k, v in scales.items()},
                'offsets': {k: round(offsets.get(k, 0.0) - center, 4) for k in srcs},
                'sd_at_high': {src: round(statistics.stdev(c[key][src]['by_effort']['high'] for m in panel for c in [curves[m]]), 4)
                               for src in full},
                'full_panel_boards': full,
                'panel_n': len(panel),
            }
        return links

    def facet_value(self, key, recs):
        common = [e for e in EFFORTS if all(e in r['by_effort'] for r in recs.values())]
        if common:
            effs = common
        elif any('high' in r['by_effort'] for r in recs.values()):
            effs = ['high']
        else:
            present_effs = [e for e in EFFORTS if any(e in r['by_effort'] for r in recs.values())]
            effs = [max(
                present_effs,
                key=lambda e: (sum(1 for r in recs.values() if e in r['by_effort']), -abs(EFFORTS.index(e) - EFFORTS.index('high'))),
            )] if present_effs else []
        by = {e: round(float(np.mean([r['by_effort'][e] for r in recs.values() if e in r['by_effort']])), 4) for e in effs}
        # A reading off the measured ladder (`none`, `minimal`, `ultra`) is a measurement: each listing source's distance
        # of its reading as listed from its own High, added to the facet's High (only its value when no source has a High).
        # One board's bound does not move it; the facet's bound does, against the facet's measured readings (score()).
        for e in LADDER:
            off = [r for r in recs.values() if e in (r.get('off_ladder') or {})]
            if e in EFFORTS or not off:
                continue
            moved_off = [r['off_ladder'][e] - r['by_effort']['high'] for r in off if 'high' in r['by_effort']]
            by[e] = (round(by['high'] + float(np.mean(moved_off)), 4) if moved_off and 'high' in by
                     else round(float(np.mean([r['off_ladder'][e] for r in off])), 4))
            effs = [*effs, e]
        by = {e: by[e] for e in LADDER if e in by}
        effs = [e for e in LADDER if e in effs]
        moved = sorted({e for r in recs.values() for e in r.get('bound_moved') or () if e in by and e in EFFORTS},
                       key=LADDER.index)
        partial_shift = [src for src, r in recs.items() if r.get('effort_shift') in ('none', 'single_aa')]
        disjoint = [src for src, r in recs.items() if not common and effs and effs[0] not in r['by_effort']]
        is_partial = bool(partial_shift or disjoint or by.get('high') is None)
        out = {
            'value': by.get('high'),
            'by_effort': by,
            'status': 'measured_partial' if is_partial else 'measured',
            'sources': {src: {'by_effort': r['by_effort'], 'listed_as': r.get('listed_as'),
                              'effort_shift': r.get('effort_shift'),
                              'link_scale': r.get('link_scale', 1.0), 'link_offset': r.get('link_offset'),
                              **({'effort_shift_basis': r['effort_shift_basis']} if r.get('effort_shift_basis') else {}),
                              **({'effort_basis': r['effort_basis']} if r.get('effort_basis') else {}),
                              **({'bound_moved': r['bound_moved']} if r.get('bound_moved') else {}),
                              **({'drift_correction': r['drift_correction']} if r.get('drift_correction') else {})}
                        for src, r in recs.items()},
            'measured_efforts': [e for e in effs if e not in moved],
        }
        if moved:
            out['bound_moved'] = moved
        if is_partial:
            detail = {}
            if partial_shift:
                detail['no_effort_shift'] = partial_shift
            if disjoint:
                detail['disjoint_effort_boards'] = disjoint
            out['measured_partial'] = detail
        if by.get('high') is None:
            out['flag'] = 'partial'
        return out

    def effort_ladder(self, m):
        """The efforts `m` runs at, in EFFORTS order: its bundles' efforts and its `dispatch_efforts` rows in
        accounts.json; EFFORTS when accounts.json names none."""
        named = {b.get('effort') for spec in (self.acc.get('accounts') or {}).values() if isinstance(spec, dict)
                 for b in (spec.get('bundles') or {}).values() if isinstance(b, dict) and b.get('model') == m}
        named |= {e for table in (self.acc.get('dispatch_efforts') or {}).values() if isinstance(table, dict)
                  for e in (table.get(m) or ())}
        ladder = tuple(e for e in EFFORTS if e in named)
        return ladder or EFFORTS

    def effort_steps(self, facets, panel):
        """{facet id: {from effort: {to effort: {'step', 'n'}}}}: the panel's median own step between two efforts of a facet
        (value at `to` minus value at `from`), over the panel models measured at both (n >= 2), read from the facets as
        measured, before any fill."""
        out = {}
        for key in sorted({k for p in panel for k in facets.get(p, {})}):
            for a in EFFORTS:
                for b in EFFORTS:
                    pts = [facets[p][key]['by_effort'] for p in panel if key in facets.get(p, {})]
                    xs = [by[b] - by[a] for by in pts if a != b and a in by and b in by]
                    if len(xs) >= 2:
                        out.setdefault('.'.join(key), {}).setdefault(a, {})[b] = {'step': round(float(np.median(xs)), 4),
                                                                                   'n': len(xs)}
        return out

    def fill_one(self, m, key, by, effort, known):
        """(value, how) for `effort` of model `m`'s facet `key` from its own efforts `known` ({effort: value}): bracketed,
        linear in effort rank on the model's `effort_ladder` (else EFFORTS when the ladder lacks either endpoint) between
        the nearest own efforts below and above; one-sided, the nearest own effort plus the panel's median own step from
        it (`effort_steps`, n >= 2). (None, refusal) when the panel gives no step."""
        at = EFFORTS.index(effort)
        below = [e for e in EFFORTS[:at] if e in known]
        above = [e for e in EFFORTS[at + 1:] if e in known]
        if below and above:
            lo, up = below[-1], above[0]
            ladder = self.effort_ladder(m)
            if not all(e in ladder for e in (lo, effort, up)):
                ladder = EFFORTS
            w = (ladder.index(effort) - ladder.index(lo)) / (ladder.index(up) - ladder.index(lo))
            return round(known[lo] + w * (known[up] - known[lo]), 4), {
                'method': 'own_effort_interpolation', 'ladder': list(ladder), 'from': {lo: known[lo]}, 'to': {up: known[up]},
                'weight': round(w, 4)}
        near = below[-1] if below else above[0]
        step = ((self.steps.get('.'.join(key)) or {}).get(near) or {}).get(effort)
        if step is None:
            return None, {'model': m, 'facet': '.'.join(key), 'effort': effort, 'from': near,
                          'reason': 'fewer than 2 panel models measured at both efforts'}
        return round(known[near] + step['step'], 4), {'method': 'panel_effort_step', 'from_effort': near,
                                                      'from_value': known[near], 'step': step['step'], 'n': step['n']}

    def fill_high(self, facets):
        """Fill High on a measured facet whose own curve lacks it (user 2026-11-12, `fill_one`), bounded with the measured
        efforts pinned so it never moves one (`bounded_from` when the bound held it): bracketed, status `interpolated`;
        one-sided, status `estimated` (method `panel_effort_step`). Returns the refusals."""
        refused = []
        for m, fs in facets.items():
            for key, rec in fs.items():
                by = dict(rec['by_effort'])
                ladder = {e: v for e, v in by.items() if e in EFFORTS}    # a reading off the ladder is not on the steps
                if 'high' in by or not ladder or not rec.get('status', '').startswith('measured'):
                    continue
                value, how = self.fill_one(m, key, ladder, 'high', ladder)
                if value is None:
                    refused.append(how)
                    continue
                held = round(float(user_scores.bound_along_effort({**by, 'high': value}, rises(key), by)['high']), 4)
                if held != value:
                    how['bounded_from'] = value
                if how['method'] == 'own_effort_interpolation':
                    rec['status'], rec['interpolation'] = 'interpolated', how
                else:
                    rec['status'], rec['estimate'] = 'estimated', how
                rec['by_effort'] = {e: (held if e == 'high' else by[e]) for e in LADDER if e in by or e == 'high'}
                rec.pop('flag', None)
        return refused

    def fill_efforts(self, m, key, rec):
        """Every effort of EFFORTS facet record `rec` of model `m` lacks (owner 2026-11-18), from its own efforts
        (`fill_one`), then the bound with its own efforts pinned: a filled value never moves one that was there. Lists each
        filled effort in `filled_efforts`; returns the refusals."""
        known = dict(rec['by_effort'])
        if not known:
            return []
        got, how, refused = dict(known), {}, []
        ladder = {k: v for k, v in known.items() if k in EFFORTS}      # a reasoning-off reading is not on the panel's steps
        for e in EFFORTS:
            if e not in known and ladder:
                value, why = self.fill_one(m, key, ladder, e, ladder)
                if value is None:
                    refused.append(why)
                else:
                    got[e], how[e] = value, why
        if how:
            bounded = user_scores.bound_along_effort({e: got[e] for e in LADDER if e in got}, rises(key), known)
            for e in how:
                if round(float(bounded[e]), 4) != got[e]:
                    how[e]['bounded_from'] = got[e]
            rec['by_effort'] = {e: round(float(bounded[e]), 4) for e in LADDER if e in bounded}
            rec['filled_efforts'] = {**(rec.get('filled_efforts') or {}), **how}
            rec['value'] = rec['by_effort'].get('high')
            rec.pop('flag', None)
        return refused

    def _model_aa_curve_for_tier_fit(self, m):
        """Return `(aa_board, {effort: z_linked})` for `m` when `m` has its own multi-effort AA curve containing High."""
        aa_linked = getattr(self, '_aa_cap_linked', None) or {}
        for aa_src in ('aa.index', 'aa.terminalbench_4', 'aa.frontiercode_2', 'aa.mmmu_pro'):
            own = (aa_linked.get(aa_src) or {}).get(m)
            if own and 'high' in own and self._has_effort_curve(own):
                return aa_src, own
        return None

    def estimate(self, m, base, key, facets, fits, rejected, tier):
        """Facet `key` of model `m` (module docstring): the nearest same-series sibling's curve moved along the facet's
        validated tier fit by the two tiers' difference (shift 0 when the fit was rejected), else the validated fit at
        the model's tier shifted only where `m` has its own AA curve."""
        fid = f'{key[0]}.{key[1]}~tier'
        f = fits.get(key)
        t = tier[m]['value']
        is_resp = not rises(key)
        if base is not None:
            vb = facets[base][key]
            shift = f['b'] * (t - tier[base]['value']) if f and tier[base]['value'] is not None else 0.0
            by = {e: round(v + shift, 4) for e, v in vb['by_effort'].items()}
            est = {'method': 'sibling_shift', 'base_model': base, 'base_value': vb['value'], 'base_tier': tier[base]['value'],
                   'tier': t, 'tier_source': tier[m]['method'], 'shift': round(shift, 4),
                   **(self.fit_record(fid, f) if f else {'shift_basis': f"shift 0: no fit, {rejected.get(fid)}"})}
            if not is_resp and len(by) == 1 and 'high' in by:
                aa_pair = self._model_aa_curve_for_tier_fit(m)
                if aa_pair is not None:
                    aa_src, own = aa_pair
                    v_high = by['high']
                    by = {e: round(v_high + (own[e] - own['high']), 4) for e in EFFORTS if e in own}
                    est['effort'] = f'shifted from High along {m} on {aa_src}'
        else:
            v = self.predict(f, t)
            aa_pair = None if is_resp else self._model_aa_curve_for_tier_fit(m)
            if aa_pair is not None:
                aa_src, own = aa_pair
                by = {e: round(v + (own[e] - own['high']), 4) for e in EFFORTS if e in own}
                effort_note = f'shifted from High along {m} on {aa_src}'
            else:
                by = {'high': v}
                effort_note = 'High only: no multi-effort Artificial Analysis curve for this model'
            est = {'method': 'tier_fit', 'tier': t, 'tier_source': tier[m]['method'], **self.fit_record(fid, f),
                   'effort': effort_note}
            if not f['x_min'] <= t <= f['x_max']:
                est['tier_clamped_to'] = f['x_min'] if t < f['x_min'] else f['x_max']
        by = bound_curve(by, rises(key))
        return {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {}, 'estimate': est,
                'measured_efforts': []}

    def group_estimate(self, m, group, d, key, facets, fits, rejected, tier):
        """The sibling estimate from a group of tied relatives (series.nearest): each relative's estimate, then per effort
        the median over the relatives that have it; one relative is its own estimate."""
        each = [self.estimate(m, b, key, facets, fits, rejected, tier) for b in group]
        if len(each) == 1:
            each[0]['estimate']['version_distance'] = d
            return each[0]
        efforts = [e for e in LADDER if all(e in x['by_effort'] for x in each)]
        by = bound_curve({e: round(float(np.median([x['by_effort'][e] for x in each])), 4) for e in efforts}, rises(key))
        est = {**each[0]['estimate'], 'base_model': group, 'base_value': [x['estimate']['base_value'] for x in each],
               'base_tier': [x['estimate']['base_tier'] for x in each], 'shift': [x['estimate']['shift'] for x in each],
               'version_distance': d, 'tie': 'median of the tied relatives'}
        return {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {}, 'estimate': est,
                'measured_efforts': []}

    def cross_line(self, m, key, facets, fits, rejected, tier, without=None):
        """A speed facet's value from the maker's other lines (owner 2026-11-19): the relatives of another line, same maker
        and size (series.groups 'ability'), measured, at the nearest version distance up to the declared one. Where each
        relative's line and the row's own line are both measured on at least two same-version pairs, the relative's curve
        plus the median line-to-line difference over those pairs (method `cross_line_extrapolation`, n and spread shown;
        z is additive on this facet's log scale), the median over the relatives; else the median of the relatives'
        estimates (method `cross_line_median`). None without such a relative. `without`: a row left out (leave-one-out)."""
        mine, pool = self.line_of.get(m), self.sibs.get(m, ())

        def measured(b):
            rec = facets.get(b, {}).get(key) or {}
            return (b != without and b != m and rec.get('status', '').startswith('measured') and rec.get('value') is not None
                    and tier[b]['value'] is not None)
        cands = [b for b in pool if self.line_of.get(b) != mine and measured(b)]
        groups = series.nearest(m, cands, pool, self.versions, self.small, None, self.line_of,
                                self.order['sibling_first_up_to_distance'])
        if not groups or groups[0][0] is None:
            return None
        d, group = groups[0]
        lines = {}
        for b in group:
            lb = self.line_of.get(b)
            ours = [x for x in pool if self.line_of.get(x) == mine and measured(x)]
            theirs = [y for y in pool if self.line_of.get(y) == lb and measured(y)]
            pairs = sorted((x, y) for x in ours for y in theirs
                           if series.distance(x, y, self.sibs.get(x, ()), self.versions) == 0)
            diffs = [facets[x][key]['value'] - facets[y][key]['value'] for x, y in pairs]
            lines[b] = {'line': lb, 'n': len(pairs), 'pairs': [list(q) for q in pairs],
                        **({'shift': round(float(np.median(diffs)), 4),
                            'spread': [round(min(diffs), 4), round(max(diffs), 4)]} if diffs else {})}
        if all(r['n'] >= MIN_SIZE_PAIRS for r in lines.values()):
            curves = [{e: v + lines[b]['shift'] for e, v in facets[b][key]['by_effort'].items()} for b in group]
            efforts = [e for e in LADDER if all(e in c for c in curves)]
            by = bound_curve({e: round(float(np.median([c[e] for c in curves])), 4) for e in efforts}, rises(key))
            return {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {}, 'measured_efforts': [],
                    'estimate': {'method': 'cross_line_extrapolation', 'base_model': group, 'version_distance': d,
                                 'base_value': [facets[b][key]['value'] for b in group], 'lines': lines}}
        est = self.group_estimate(m, group, d, key, facets, fits, rejected, tier)
        est['estimate'] = {**est['estimate'], 'method': 'cross_line_median', 'lines': lines}
        return est

    @staticmethod
    def category(a, facets):
        """{'value', 'by_effort', 'status', ...} of category `a` from its facet records. Never silently skips a missing
        required facet (review F1): `coding` requires both facets, and `tool_operation` requires each level's facets."""
        names = AXES[a]
        present = [f for f in names if f in facets]
        missing = [f for f in names if f not in facets]
        if not present:
            return {'value': None, 'by_effort': {}, 'status': 'missing', 'facets': {}, 'missing_facets': missing,
                    'flag': 'missing', 'reason': 'no trusted source, no sibling and no fit for any of its facets'}

        def combine(selected):
            miss = [f for f in selected if f not in facets]
            if miss:
                return {}, [f for f in selected if f in facets], miss
            effs = [e for e in LADDER if all(e in facets[f]['by_effort'] for f in selected)]
            return ({e: round(min(facets[f]['by_effort'][e] for f in selected), 4) for e in effs},
                    list(selected), [])

        out = {'facets': {f: facets[f] for f in present}, 'missing_facets': missing}
        if a == 'tool_operation':
            levels = {}
            for level, selected in TOOL_LEVEL_FACETS.items():
                if selected:
                    by_l, used, miss_l = combine(selected)
                    levels[str(level)] = {'by_effort': by_l, 'facets': used,
                                          **({'missing_facets': miss_l} if miss_l else {})}
            out['by_level'] = levels
            by = levels['3']['by_effort']
        else:
            by, _, _ = combine(names)       # a min of curves that never decrease never decreases
        statuses = {facets[f]['status'] for f in present}
        val_high = by.get('high')
        if missing:
            status = 'partial'
            out['flag'] = 'partial'
        elif val_high is None:
            status = 'measured_partial' if all(s.startswith('measured') for s in statuses) else 'partial'
            out['flag'] = 'partial'
        elif 'estimated' in statuses:
            status = 'estimated'
        elif 'interpolated' in statuses:
            status = 'interpolated'
        elif 'measured_partial' in statuses:
            status = 'measured_partial'
        else:
            status = 'measured'
        out.update(value=val_high, by_effort=by, status=status)
        return out

    def score(self):
        rows = {}
        for m in self.models:
            fields = self.meas['models'].get(m) or {}
            rows[m] = {'fields': fields, 'boards': self.board_entries(fields)}
        raw_same = series.same_model(self.acc)
        self.same = {k: v for k, v in raw_same.items() if valid_public_declaration(v) and v.get('model') not in self.entries}
        self.speed = {m: sp for m, e in self.same.items() if (sp := speed_declaration(e)) is not None}
        self.sibs = without_entries(series.groups({**self.acc, 'same_model': self.same}, 'ability'), self.entries)
        self.line_sibs = without_entries(series.groups({**self.acc, 'same_model': self.same}, 'line'), self.entries)
        self.versions, self.small, self.line_of = series.versions(self.acc), series.small_rows(self.acc), series.lines(self.acc)
        self.order = estimation_order(self.acc)
        tier = self.tier_records(rows)
        self.direct_tier = {m: t['value'] for m, t in tier.items() if t['method'] in ('arena_overall', 'other_overall')}
        panel = [m for m in (self.meas.get('panel') or {}).get('models') or () if m in rows]
        self.effort_correlations = {}
        self._aa_cap_linked = {}
        curves, links = self.board_curves_and_link(rows, panel)
        self.links = links
        facets = {m: {key: self.facet_value(key, recs) for key, recs in c.items()} for m, c in curves.items()}
        self.steps = self.effort_steps(facets, panel)
        self.fill_refused = self.fill_high(facets)
        for fs in facets.values():
            for key, rec in fs.items():
                # along effort an estimate moves, a measurement does not; two measurements out of order: the anchor rule
                pins = [x for x in rec['measured_efforts'] if x in rec['by_effort']]
                bounded = bound_pinned(rec['by_effort'], pins, rises(key)) if pins else bound_curve(rec['by_effort'], rises(key))
                moved = {x for x in bounded if bounded[x] != round(float(rec['by_effort'][x]), 4)}
                rec['by_effort'] = bounded
                if moved:
                    rec['bound_moved'] = sorted(set(rec.get('bound_moved') or ()) | moved, key=LADDER.index)
                rec['measured_efforts'] = [e for e in rec['measured_efforts'] if e not in moved]
                rec['value'] = rec['by_effort'].get('high')
        keys = [(a, f) for a, fs in AXES.items() for f in fs]
        fits, rejected = {}, {}
        for key in keys:
            pts = [(self.direct_tier[m], facets[m][key]['value']) for m in rows
                   if key in facets[m] and facets[m][key].get('value') is not None and m in self.direct_tier
                   and facets[m][key]['status'] != 'estimated' and m not in self.entries]
            fits[key], why = self.fit(pts, require_skill=True)
            if why:
                rejected[f'{key[0]}.{key[1]}~tier'] = why
        declared = [m for m in rows if (self.same.get(m) or {}).get('model') in rows]
        for m in rows:
            if tier[m]['value'] is None:
                continue
            for key in keys:
                if m in declared and not self.own_estimated(m, key):
                    continue
                kept = facets[m].get(key)
                if kept is not None and kept.get('value') is not None:
                    continue
                if m in self.speed and key == SPEED_KEY:
                    continue        # a speed variant's speed rests on its base
                # an ability facet reads the maker's same-size rows of every line; a speed facet its own line only
                sibs = (self.sibs if rises(key) else self.line_sibs).get(m, ())
                usable = [b for b in sibs
                          if (facets[b].get(key) or {}).get('status', '').startswith('measured')
                          and (facets[b].get(key) or {}).get('value') is not None and tier[b]['value'] is not None]
                groups = series.nearest(m, usable, sibs, self.versions, self.small, self.same, self.line_of,
                                        self.order['sibling_first_up_to_distance'] if rises(key) else None)
                d, group = groups[0] if groups else (None, [])
                # the declared order (accounts.json estimation_order): a relative up to the declared version distance
                # first; from there on, or without a declared distance, the tier fit first; a speed facet without a near
                # relative of its own line reads the maker's other lines (cross_line) before or after the fit, as declared
                near = bool(group) and d is not None and d <= self.order['sibling_first_up_to_distance']
                cross = None if rises(key) or near else self.cross_line(m, key, facets, fits, rejected, tier)
                if group and near:
                    facets[m][key] = self.group_estimate(m, group, d, key, facets, fits, rejected, tier)
                elif cross and self.order['speed_cross_line_before_fit']:
                    facets[m][key] = cross
                elif fits.get(key):
                    facets[m][key] = self.estimate(m, None, key, facets, fits, rejected, tier)
                elif cross:
                    facets[m][key] = cross
                elif group:
                    facets[m][key] = self.group_estimate(m, group, d, key, facets, fits, rejected, tier)
                if group or fits.get(key) or cross:
                    if kept is not None and 'none' in kept['by_effort']:      # its measured reasoning-off reading stays
                        # and the estimate moves, not the measurement: bounded with the measured none pinned
                        est = facets[m][key]
                        held = {**est['by_effort'], 'none': kept['by_effort']['none']}
                        est['by_effort'] = {e: round(float(v), 4) for e, v in
                                            user_scores.bound_along_effort(held, rises(key), ['none']).items()}
                        est['value'] = est['by_effort'].get('high')
                        est['measured_efforts'] = ['none']
        self.copy_same_model(facets, declared)
        self.size_of = {m: rel for m, rel in release_tier_up(self.acc, self.entries).items()
                        if m in rows and rel['model'] in rows and m not in declared}
        self.complete(facets, keys, rows, declared)
        for m, r in rows.items():
            r['axes'] = {}
            for a in AXES:
                own = {f: facets[m][(a, f)] for f in AXES[a] if (a, f) in facets[m]}
                if m in declared:
                    own = {f: rec for f, rec in own.items() if rec.get('status', '').startswith('measured')
                           or self.carried(m, (a, f)) or self.own_estimated(m, (a, f))}
                r['axes'][a] = self.category(a, own)
        fit_doc = {f'{a}.{c}~tier': {k: round(v, 4) for k, v in f.items()} for (a, c), f in fits.items() if f}
        return rows, tier, fit_doc, rejected, links, self.targets(rows, panel)

    def carried(self, m, key):
        """True when the `same_model` row `m` carries facet `key` of its model: `carries` names its category, or the
        facet as `category.facet` (a speed variant shares the abilities, not the speed)."""
        carries = (self.same.get(m) or {}).get('carries')
        return m in self.same and (carries is None or key[0] in carries or '.'.join(key) in carries)

    def own_estimated(self, m, key):
        """True when the `same_model` row `m` declares facet `key` its own (`own_estimated`: a speed variant's speed),
        estimated by the ordinary methods rather than copied or left missing."""
        return '.'.join(key) in ((self.same.get(m) or {}).get('own_estimated') or ())

    def copy_same_model(self, facets, declared):
        """A `same_model` row is the very model it declares: it takes that model's facets as they stand, for the categories
        or facets it carries, and with them the efforts its model's facet was refused (`fill_refused`, `same_model` naming
        the model). A facet it already has (its own measurement, or one copied before) stands."""
        for m in declared:
            entry = self.same[m]
            at = same_model_point(entry)
            for key, rec in facets[entry['model']].items():
                if key in facets[m] or not self.carried(m, key):
                    continue
                if at is not None and rec['by_effort'].get(at) is None:
                    continue        # the model has no value at the declared point: the facet stays missing
                by = dict.fromkeys(LADDER, rec['by_effort'][at]) if at is not None else dict(rec['by_effort'])
                point = ({'at_effort': at, 'base_effort_method': ('measured' if at in (rec.get('measured_efforts') or ()) else
                                                                  ((rec.get('filled_efforts') or {}).get(at) or {}).get('method')
                                                                  or rec['status']),
                          'at_effort_declaration': {k: v for k, v in entry['at_effort'].items() if k != 'effort'}}
                         if at is not None else {})
                facets[m][key] = {'value': by.get('high'), 'by_effort': by, 'sources': {}, 'status': 'estimated',
                                  'estimate': {'method': 'same_model', 'base_model': entry['model'], 'base_status': rec['status'],
                                               **point,
                                               **{k: entry[k] for k in ('kind', 'statement', 'url', 'as_of') if entry.get(k)}},
                                  'measured_efforts': []}
                facet = '.'.join(key)
                self.fill_refused += [{**r, 'model': m, 'same_model': entry['model']} for r in list(self.fill_refused)
                                      if r['model'] == entry['model'] and r['facet'] == facet and r['effort'] not in rec['by_effort']]

    def complete(self, facets, keys, rows, declared=()):
        """Every method after measurement, series and fit, in the order calibration/README.md states: the downward
        derivation with the row's own gap (`tier_down`), the size step (`size_down`), every measured effort a facet lacks
        (`fill_efforts`), the edge efforts (`fill_edge`), the bound across size (`bound_across_size`), the model's own ability
        median (`own_ability_median`), and the `same_model` copy of what these added. Reads `size_of` and `steps`, writes
        `size_steps` and adds to `fill_refused`."""
        self.size_steps = self.size_step_table(facets, keys)
        self.tier_down(facets, keys)
        self.size_down(facets, keys, 'same')
        for m in rows:
            for key, rec in facets[m].items():
                self.fill_refused += self.fill_efforts(m, key, rec)
        for key, by_edge in self.edge_step_table(keys).items():
            for edge, step in by_edge.items():
                self.steps.setdefault(key, {}).setdefault(EDGES[edge][0], {})[edge] = step
        for m in rows:
            for key, rec in facets[m].items():
                for edge in EDGES:
                    self.fill_refused += self.fill_edge(m, key, rec, edge)
        self.flatten_points(facets, rows)   # an operating point's one value, before the own median reads its facets
        self.bound_across_size(facets)
        for m in rows:
            if m not in declared:
                self.own_ability_median(facets[m], keys)
        # the size step from the nearest version comes after the own ability median (estimation_order, review D)
        self.size_down(facets, keys, 'nearest')
        self.bound_across_size(facets)
        self.speed_variants(facets)
        self.flatten_points(facets, rows)
        self.copy_same_model(facets, declared)
        self.response_floors(facets)

    def flatten_points(self, facets, rows):
        for m in self.points:
            if m in rows:
                for rec in facets[m].values():
                    self.flatten_point(m, rec)

    def speed_variants(self, facets):
        """A speed variant (`same_model` with `speed`, speed_declaration) without its own responsiveness reading: its base's
        curve moved by the vendor's stated speed ratio r, ln(r) / sd_panel of the time-to-first-answer board (a speed ratio
        r divides the time to the first answer token by r; method `speed_ratio`, the base value and the ratio shown), else,
        when the vendor states no ratio, the base's curve as a stated lower bound (method `speed_lower_bound`: the variant
        is at least as fast as its base). Never the base's value silently."""
        for m, sp in sorted(self.speed.items()):
            sd = ((((self.meas.get('sources') or {}).get(measurements.AA_TTFA) or {}).get('scale') or {}).get('sd_panel'))
            base = self.same[m]['model']
            rec = (facets.get(base) or {}).get(SPEED_KEY)
            if m not in facets or SPEED_KEY in facets[m] or not (rec or {}).get('by_effort'):
                continue
            r = sp['ratio']
            if r and not sd:
                raise ValueError(f'{measurements.AA_TTFA} has no sd_panel: the speed ratio of {m} cannot be applied')
            shift = round(math.log(r) / sd, 4) if r else 0.0
            by = {e: round(v + shift, 4) for e, v in rec['by_effort'].items()}
            cite = {k: v for k, v in sp.items() if k not in ('ratio', 'stated')}
            how = ({'method': 'speed_ratio', 'ratio': r, 'stated_ratio': sp['stated'], 'shift': shift,
                    'shift_basis': f'ln(ratio) / sd_panel of {measurements.AA_TTFA} ({sd}): the ratio divides the time to '
                                   'the first answer token'} if r else
                   {'method': 'speed_lower_bound', 'bound': 'lower: at least as fast as its base (no ratio stated)'})
            facets[m][SPEED_KEY] = {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {},
                                    'measured_efforts': [],
                                    'estimate': {**how, 'base_model': base, 'base_value': rec['value'],
                                                 'base_status': rec['status'], 'speed': cite}}

    def response_floors(self, facets):
        """A row with a declared floor of its response time (`response_time_source`, `min_seconds`) and no responsiveness
        reading of its own: the floor read through the transform every measured row goes through on the time-to-first-answer
        board, x = -ln(seconds), z = (x - x_anchor) / sd_panel, bounds the value the next method of the order gives at each
        effort: the slower of the two (z at most the floor's), the floor alone where no method gives one, then bounded along
        effort (method `declared_response_floor`, both inputs and which one binds on the cell). A response that takes at
        least that long reaches its first answer token no sooner than its thinking ends, which for a long-thinking response
        is most of that time: a declared bound, not a fit."""
        scale = (((self.meas.get('sources') or {}).get(measurements.AA_TTFA) or {}).get('scale') or {})
        for m, decl in sorted(self.response_floor.items()):
            rec = (facets.get(m) or {}).get(SPEED_KEY)
            if m not in facets or (rec or {}).get('status', '').startswith('measured'):
                continue        # its own reading stands
            if not scale.get('sd_panel'):
                raise ValueError(f'{measurements.AA_TTFA} has no scale: the declared response floor of {m} cannot be read')
            x = -math.log(decl['min_seconds'])
            z = round((x - scale['anchor_raw']) / scale['sd_panel'], 4)
            given = (rec or {}).get('by_effort') or {}
            binds = {e: 'estimate' if given.get(e) is not None and given[e] <= z else 'floor' for e in LADDER}
            moved = set()
            by = bound_curve({e: given[e] if binds[e] == 'estimate' else z for e in LADDER}, rises(SPEED_KEY), moved)
            facets[m][SPEED_KEY] = {
                'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {}, 'measured_efforts': [],
                'estimate': {'method': 'declared_response_floor', 'min_seconds': decl['min_seconds'], 'floor_value': z,
                             'binds_by_effort': binds,
                             **({'bounded_along_effort': sorted(moved, key=LADDER.index)} if moved else {}),
                             'estimate_input': ({k: rec[k] for k in ('value', 'by_effort', 'status', 'estimate') if k in rec}
                                                if given else None),
                             'bound': 'upper: no faster than the declared floor',
                             'transform': (f"x = -ln({decl['min_seconds']} s) = {x:.4f}; z = (x - "
                                           f"{scale['anchor_raw']:.6g}) / {scale['sd_panel']:.6g} "
                                           f"({measurements.AA_TTFA} scale)"),
                             'declaration': {k: v for k, v in decl.items() if k != 'min_seconds'}}}

    def flatten_point(self, m, rec):
        """An operating point row (`operating_points`): every effort reads its one value, the one at the effort `default`
        reads; no effort step (method `operating_point`)."""
        at = self.readings['default']
        if not self.points[m]['reasoning'] and not rec.get('measured_efforts') and 'none' in rec['by_effort']:
            at = 'none'     # no reading of its own, estimated from a relative that reasons: that relative's none point
        v = rec['by_effort'].get(at)
        if v is None:
            return
        rec['by_effort'] = {e: v for e in LADDER}
        # the citation is the row's accounts.json `operating_point`, stated once there
        rec['filled_efforts'] = {e: {'method': 'operating_point', 'from_effort': at} for e in LADDER if e != at}
        rec['measured_efforts'] = [e for e in rec.get('measured_efforts') or () if e == at]
        rec['value'] = v

    def dominance(self, rows):
        """{row A: [rows B dominating A]} (owner 2026-11-19, round 7, as corrected): A is dominated by B when they are the
        same maker and product (series.identity), A's declared size is strictly smaller than B's (`small` below `full`)
        and A's version is not newer than B's: both declare a version (`series_versions`) and A's is not above B's, or B is
        A's declared larger model of the same version (series.tier_up, `version: same`). Equal size never dominates and
        version alone never does; a pair whose versions the data does not order is not compared."""
        ident = series.identity({**self.acc, 'same_model': self.same})
        up = series.tier_up(self.acc)
        rows = [m for m in rows if m in ident]

        def not_newer(a, b):
            if a in self.versions and b in self.versions:
                return self.versions[a] <= self.versions[b]
            return up.get(a) == {'model': b, 'version': 'same'}
        return {a: sorted(b for b in rows if ident[a]['size'] == 'small' and ident[b]['size'] == 'full'
                          and ident[b]['maker'] == ident[a]['maker'] and ident[b]['product'] == ident[a]['product']
                          and not_newer(a, b)) for a in rows}

    def bound_across_size(self, facets):
        """No inversion across size (owner 2026-11-18, restated 2026-11-19 round 7): on every ability facet and effort, a
        row's value does not exceed that of any row dominating it (`dominance`). An estimate moves, a measurement does
        not, along effort as across size. A dominating estimate is raised to the lowest value a dominated row can take at
        that effort (its measurement, else the floor its own measured efforts give it, `effort_floor`;
        `size_order.raised`); a dominated estimate is bounded by the lowest dominating value (`size_order.held`); each
        moved record is then bounded along effort with its measured efforts kept (`bound_pinned`; an effort that moves
        only there `size_order.along_effort`), which also keeps a held value at its floor and a raised one under its
        ceiling. Each moved effort keeps the value it had (`from`). The curves come in bounded along effort,
        and raising a curve to a rising floor under its ceiling, or holding one at another rising curve above its floor,
        keeps it in order, so one pass is the fixpoint, in no order a name decides (the raise reads no held value).
        Whatever is still out of order is kept as read and listed (`self.measured_inversions`, the refresh report), each
        pair with which of its values is measured. A model-entry row is bounded by release rows and never moves one.
        The speed facets have no bound across size."""
        dom = {a: bs for a, bs in self.dominance(list(facets)).items() if bs}
        release = {m for m in facets if m not in self.entries}
        below = {}
        for a, bs in dom.items():
            for b in bs:
                below.setdefault(b, []).append(a)
        keys = sorted({k for fs in facets.values() for k in fs if rises(k)})
        moved_recs = []
        for key in keys:
            for e in LADDER:
                val, meas, floor = {}, {}, {}
                for m in set(dom) | set(below):
                    rec = facets[m].get(key)
                    v = ((rec or {}).get('by_effort') or {}).get(e)
                    if v is not None:
                        val[m], meas[m], floor[m] = v, e in (rec.get('measured_efforts') or ()), effort_floor(rec, e)
                for m in sorted(below):    # raise: a dominating estimate is at least what a dominated row can be
                    if m not in val or meas[m]:
                        continue
                    lows = [(val[a] if meas[a] else floor[a], a) for a in below[m]
                            if a in val and (a in release or m not in release)]
                    lows = [(v, a) for v, a in lows if v is not None]
                    if not lows:
                        continue
                    v, a = max(lows)
                    if v > val[m] + 1e-9:
                        self._move(facets[m][key], e, v, 'raised', a, moved_recs)
                        val[m] = round(v, 4)
                for m in sorted(dom):      # bound: a dominated estimate is at most every dominating value
                    ups = [(val[b], b) for b in dom[m] if b in val and (b in release or m not in release)]
                    if m not in val or meas[m] or not ups:
                        continue
                    v, b = min(ups)
                    if v < val[m] - 1e-9:
                        self._move(facets[m][key], e, v, 'held', b, moved_recs)
                        val[m] = round(v, 4)
        for rec in moved_recs:             # along effort: the measured efforts stay, the estimates are bounded
            pins = [x for x in rec.get('measured_efforts') or () if x in rec['by_effort']]
            old = rec['by_effort']
            rec['by_effort'] = bound_pinned(old, pins) if pins else bound_curve(old, True)
            for e, v in rec['by_effort'].items():
                if v != round(float(old[e]), 4) and not any(e in d for d in rec['size_order'].values()):
                    rec['size_order'].setdefault('along_effort', {})[e] = {'from': round(float(old[e]), 4)}
            rec['value'] = rec['by_effort'].get('high')
        self.measured_inversions = []
        for a in sorted(dom):
            for b in dom[a]:
                if not (b in release or a not in release):
                    continue
                for key in keys:
                    ra, rb = facets[a].get(key), facets[b].get(key)
                    for e in LADDER:
                        va, vb = ((ra or {}).get('by_effort') or {}).get(e), ((rb or {}).get('by_effort') or {}).get(e)
                        if va is not None and vb is not None and va > vb + 1e-9:
                            self.measured_inversions.append({
                                'model': a, 'dominated_by': b, 'facet': '.'.join(key), 'effort': e, 'value': va,
                                'dominating_value': vb, 'measured': e in (ra.get('measured_efforts') or ()),
                                'dominating_measured': e in (rb.get('measured_efforts') or ())})

    @staticmethod
    def _move(rec, e, v, how, by, moved_recs):
        rec['size_order'] = rec.get('size_order') or {}
        rec['size_order'].setdefault(how, {})[e] = {'model': by, 'from': round(rec['by_effort'][e], 4)}
        rec['by_effort'][e] = round(v, 4)
        rec['value'] = rec['by_effort'].get('high')
        if not any(r is rec for r in moved_recs):
            moved_recs.append(rec)

    def own_ability_median(self, own, keys):
        """The last, coarsest method (owner 2026-11-18): an ability facet the model still lacks (`own`: its facet records)
        is, at each effort, the median of the ability facets it has there, when it has at least MIN_OWN_FACETS of them (all
        on the panel-z scale); status `estimated`, method `own_ability_median` with the facets used."""
        have = [k for k in keys if rises(k) and (own.get(k) or {}).get('by_effort')]
        for key in keys:
            if not rises(key) or key in own:
                continue
            by = {e: float(np.median([own[k]['by_effort'][e] for k in have if e in own[k]['by_effort']])) for e in LADDER
                  if sum(e in own[k]['by_effort'] for k in have) >= MIN_OWN_FACETS}
            if 'high' not in by:
                continue
            by = bound_curve(by, True)
            own[key] = {'value': by['high'], 'by_effort': by, 'status': 'estimated', 'sources': {}, 'measured_efforts': [],
                        'estimate': {'method': 'own_ability_median', 'facets': ['.'.join(k) for k in have], 'n': len(have)}}

    def tier_down(self, facets, keys):
        """An ability facet a small row lacks after measurement and estimation, from its larger model of the same version
        (module docstring): large(e) + min(gap, 0), gap the mean of (small - large) at High over the facets both measured
        (`gap_basis` `measured`), else over the facets both have any value for, never a `tier_down` one (`estimated`)."""
        for m, rel in sorted(self.size_of.items()):
            if rel['version'] != 'same':
                continue        # a row's own gap is not carried across versions: the size step (size_down) serves it
            big = rel['model']
            own, theirs = facets[m], facets[big]

            def direct(rec):
                return (rec or {}).get('status') in TARGET_STATUSES and (rec or {}).get('value') is not None

            def held(rec):
                return (rec or {}).get('value') is not None and ((rec or {}).get('estimate') or {}).get('method') != 'tier_down'
            basis = 'measured'
            shared = [k for k in keys if rises(k) and direct(own.get(k)) and direct(theirs.get(k))]
            if not shared:
                basis = 'estimated'
                shared = [k for k in keys if rises(k) and held(own.get(k)) and held(theirs.get(k))]
            if not shared:
                continue
            gap = float(np.mean([own[k]['value'] - theirs[k]['value'] for k in shared]))
            drop = min(gap, 0.0)
            for key in keys:
                base = theirs.get(key)
                if key in own or not rises(key) or not (base or {}).get('by_effort'):
                    continue
                by = {e: round(v + drop, 4) for e, v in base['by_effort'].items()}
                own[key] = {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {},
                            'measured_efforts': [],
                            'estimate': {'method': 'tier_down', 'base_model': big, 'base_status': base['status'],
                                         'gap': round(gap, 4), 'drop': round(drop, 4), 'gap_facets': ['.'.join(k) for k in shared], 'gap_basis': basis,
                                         'n': len(shared)}}

    def size_step_table(self, facets, keys):
        """{facet id: {'step', 'median', 'n', 'agree', 'reason'?, 'basis', 'pairs', 'differences'?}} (module docstring, size
        step): one value per declared same-version pair (small row, its larger model) that both measured the facet at High,
        small - large (`basis` `facet`); an ability facet with fewer than MIN_SIZE_PAIRS such pairs takes, per pair, the mean
        over the ability facets that pair measured (`ability_pooled`: they share the panel-z scale; `differences` counts
        the facet readings). `n` counts pairs; the step follows `pair_agreement`. Read from the measured efforts only, so no
        estimate enters it."""
        def at_high(m, key):
            rec = facets[m].get(key) or {}
            return rec['by_effort'].get('high') if 'high' in (rec.get('measured_efforts') or ()) else None

        def reading(small, large, ks):
            got = [(k, at_high(small, k) - at_high(large, k)) for k in ks
                   if at_high(small, k) is not None and at_high(large, k) is not None]
            if not got:
                return None
            return {'small': small, 'large': large, 'facets': ['.'.join(k) for k, _ in got],
                    'differences': [round(d, 4) for _, d in got], 'value': float(np.mean([d for _, d in got]))}
        same = sorted((m, rel['model']) for m, rel in self.size_of.items() if rel['version'] == 'same'
                      and m not in self.entries)
        ability = [k for k in keys if rises(k)]
        out = {}
        for key in keys:
            pairs, basis = [p for s, l in same if (p := reading(s, l, [key]))], 'facet'
            if len(pairs) < MIN_SIZE_PAIRS and rises(key):
                pairs, basis = [p for s, l in same if (p := reading(s, l, ability))], 'ability_pooled'
            rec = pair_agreement([p['value'] for p in pairs])
            out['.'.join(key)] = {**rec, 'basis': basis,
                                  'pairs': [{**{k: v for k, v in p.items() if k != 'value'}, 'difference': round(p['value'], 4)}
                                            for p in pairs],
                                  **({'differences': sum(len(p['facets']) for p in pairs)} if basis == 'ability_pooled' else {})}
        return out

    def size_down(self, facets, keys, version):
        """A facet a small row still lacks (no measurement, sibling, fit or own-gap derivation), from its larger model, same
        or nearest version, moved by the fleet's size step (module docstring): value(e) = large(e) + move, move the step
        (zero where the pairs do not agree, `pair_agreement`), and for an ability facet never up: min(step, 0). Status
        `estimated`, method `size_step`, with the pairs and, for a zero step, the reason."""
        for m, rel in sorted(self.size_of.items()):
            if rel['version'] != version:
                continue
            big = rel['model']
            own, theirs = facets[m], facets[big]
            for key in keys:
                base, step = theirs.get(key), self.size_steps.get('.'.join(key))
                if key in own or not (base or {}).get('by_effort') or step is None:
                    continue
                move = min(step['step'], 0.0) if rises(key) else step['step']
                by = {e: round(v + move, 4) for e, v in base['by_effort'].items()}
                own[key] = {'value': by.get('high'), 'by_effort': by, 'status': 'estimated', 'sources': {},
                            'measured_efforts': [],
                            'estimate': {'method': 'size_step', 'base_model': big, 'version': rel['version'],
                                         'base_status': base['status'], 'step': step['step'], 'move': round(move, 4),
                                         'n': step['n'], 'agree': step['agree'],
                                         **({'reason': step['reason']} if 'reason' in step else {}), 'basis': step['basis'],
                                         'pairs': [[p['small'], p['large']] for p in step['pairs']]}}

    def edge_step_table(self, keys):
        """{facet id: {edge effort: {'step', 'n', 'sample', 'method', ...}}} (calibration/README.md, Edge efforts; round 6
        item 5): for each edge effort (`EDGES`), the pairs of every listing a board of the facet holds at both efforts of
        the edge's pair, catalog row or not (`public-measurements.json` `sources.<board>.edge_pairs`, the board's own readings
        before any bound), each difference on the facet's linked scale (the board's link scale). `none`: `pair_agreement`
        (n >= MIN_SIZE_PAIRS, one direction, else zero); the others: the median, n >= 2. An ability facet no board gives a
        pair for takes the median of the other ability facets' steps for that edge (method `other_facets_median`, the
        facets shown); a facet without either has no step (`effort_fill_refused`)."""
        meas_sources = self.meas.get('sources') or {}
        out = {}
        for key in keys:
            fid = '.'.join(key)
            link = self.links.get(fid) or {}
            boards = [src for src in link.get('boards') or () if src not in self.distrust
                      and measurements.CAPABILITY_SOURCES.get(src) == key]
            for edge in EDGES:
                pairs, used = [], {}
                for src in boards:
                    got = ((meas_sources.get(src) or {}).get('edge_pairs') or {}).get(edge) or ()
                    scale = (link.get('scales') or {}).get(src, 1.0)
                    for pr in got:
                        pairs.append(pr['difference'] * scale)
                    if got:
                        used[src] = len(got)
                if edge_method(edge) == 'fleet_off_step':
                    if pairs:
                        out.setdefault(fid, {})[edge] = {**pair_agreement(pairs), 'sample': 'board listings',
                                                         'boards': used, 'method': edge_method(edge)}
                elif len(pairs) >= 2:
                    out.setdefault(fid, {})[edge] = {'step': round(float(np.median(pairs)), 4), 'n': len(pairs),
                                                     'sample': 'board listings', 'boards': used, 'method': edge_method(edge)}
        for key in keys:
            fid = '.'.join(key)
            if not rises(key):
                continue
            for edge in EDGES:
                if edge in out.get(fid, {}):
                    continue
                others = {'.'.join(k): out['.'.join(k)][edge]['step'] for k in keys
                          if k != key and rises(k) and (out.get('.'.join(k)) or {}).get(edge, {}).get('sample') == 'board listings'}
                if others:
                    out.setdefault(fid, {})[edge] = {'step': round(float(np.median(list(others.values()))), 4),
                                                     'n': len(others), 'sample': 'other ability facets',
                                                     'facets': others, 'method': 'other_facets_median',
                                                     'edge_method': edge_method(edge)}
        return out

    def fill_edge(self, m, key, rec, edge):
        """Edge effort `edge` of facet record `rec`: its neighbour plus the fleet's edge step, then the bound with every other
        effort pinned, so it never moves one. Returns the refusals."""
        anchor, (end, nxt) = EDGES[edge]
        by = dict(rec.get('by_effort') or {})
        near = user_scores.edge_anchor(edge, by, rec.get('measured_efforts') or ())
        if edge in by or near is None:
            return []
        step = ((self.steps.get('.'.join(key)) or {}).get(anchor) or {}).get(edge)
        if step is None:
            return [{'model': m, 'facet': '.'.join(key), 'effort': edge, 'from': near,
                     'reason': (f'fewer than 2 board listings at {end} and {nxt}' if edge_method(edge) != 'fleet_off_step'
                                else 'no board listing at none and at a reasoning effort')}]
        value = round(by[near] + step['step'], 4)
        held = round(float(user_scores.bound_along_effort({**by, edge: value}, rises(key), list(by))[edge]), 4)
        how = {'method': step['method'], 'from_effort': near, 'from_value': by[near], 'step': step['step'], 'n': step['n']}
        if held != value:
            how['bounded_from'] = value
        rec['by_effort'] = {e: (held if e == edge else by[e]) for e in LADDER if e in by or e == edge}
        rec['filled_efforts'] = {**(rec.get('filled_efforts') or {}), edge: how}
        return []

    def targets(self, rows, panel):
        """{category: {'levels': {level: ...}, 'continuity'}} from the panel's values at High (module docstring): level 1
        the panel's weakest, level 2 its median, level 3 its maximum (the anchor is the origin of z, not a level); the fit's
        zero is the panel weakest. creativity and coding read the category value. aesthetics and tool_operation read their
        facets one by one (`by_facet`): each level asks for the facets LEVEL_FACETS selects, tool_operation level 1 for
        nothing, and a level-3 task asks of `LEVEL3_ASKS_LEVEL2` facets what a level-2 task asks. `continuity` is t3 - t2
        (tool_operation: the largest positive t3 - t2 across its level-3 facets).
        Only measured or own-curve interpolated High values enter (user 2026-11-12, `TARGET_STATUSES`): an own-curve interpolation lies between two
        of the model's measurements and counts; an estimate (panel effort step, sibling, tier fit, same_model) is derived
        from the panel and would move it, so it never does. Every level-3 row also carries its `ceiling` (owner,
        2026-11-13): the highest such High value among all catalogued models, the cap a level-3 demand reads instead of
        t3; it is not a panel statistic, so a model outside the panel can raise it."""
        def measured(cell):
            cell = cell or {}
            return cell.get('value') if cell.get('status') in TARGET_STATUSES else None

        def stats(pick):
            found = [(v, m) for m in panel if (v := measured(pick(rows[m]['axes']))) is not None]
            if not found:
                return None
            vals = [v for v, _ in found]
            top_value, top_model = max(found, key=lambda t: (t[0], t[1]))
            return {'min': round(float(min(vals)), 4), 'median': round(float(np.median(vals)), 4),
                    'max': round(float(top_value), 4), 'max_model': top_model, 'n': len(vals), 'ceiling': ceiling(pick)}

        def ceiling(pick):
            """The level-3 cap: the highest measured High value among every catalogued model (not only the panel), the
            model that sets it and its status; the same row kinds the panel targets read, so no estimate sets it."""
            found = [(v, m, pick(rows[m]['axes'])['status']) for m in sorted(rows)
                     if m not in self.entries and (v := measured(pick(rows[m]['axes']))) is not None]
            if not found:
                return None
            v, m, status = max(found, key=lambda t: t[0])
            return {'value': round(float(v), 4), 'model': m, 'status': status, 'n': len(found),
                    'statistic': 'highest measured value at High among catalogued models'}
        out = {}
        for a in AXES:
            facet_names = list(dict.fromkeys([*AXES[a], *(LEVEL_FACETS[a][3] if a in LEVEL_FACETS else ())]))
            facet_targets = {}
            for f in facet_names:
                own_a, own_f = FACET_OWNER.get(f, (a, f))
                fst = stats(lambda ax, oa=own_a, of=own_f: (ax[oa].get('facets') or {}).get(of))
                if fst is not None:
                    t1_f, t2_f, t3_f = fst['min'], fst['median'], fst['max']
                    facet_targets[f] = {'levels': {'1': {'target': t1_f, 'zero': t1_f, 'statistic': 'panel minimum', 'n': fst['n']},
                                                   '2': {'target': t2_f, 'zero': t1_f, 'statistic': 'panel median', 'n': fst['n']},
                                                   '3': {'target': t3_f, 'zero': t1_f, 'statistic': 'panel maximum', 'n': fst['n'],
                                                         'model': fst['max_model'], 'ceiling': fst['ceiling']}}}
            if a not in LEVEL_FACETS:
                st = stats(lambda ax, a=a: ax[a])
                levels = {str(level): ({'target': st[key], 'zero': st['min'], 'statistic': name, 'n': st['n'],
                                        **({'model': st['max_model'], 'ceiling': st['ceiling']} if level == 3 else {})} if st else
                                       {'target': None, 'zero': None, 'reason': 'no panel model has a value'})
                          for level, key, name in ((1, 'min', 'panel minimum'), (2, 'median', 'panel median'),
                                                   (3, 'max', 'panel maximum'))}
                gap = (levels['3']['target'] - levels['2']['target']) if (
                    st and levels['3']['target'] is not None and levels['2']['target'] is not None
                ) else None
                out[a] = {'levels': levels, 'facets': facet_targets, 'continuity': None if gap is None else round(gap, 4)}
                continue
            per = {}
            for f in facet_names:
                own_a, own_f = FACET_OWNER.get(f, (a, f))
                per[f] = stats(lambda ax, oa=own_a, of=own_f: (ax[oa].get('facets') or {}).get(of))
            levels, prev = {}, {}
            level_spec = {1: ('min', 'panel minimum'), 2: ('median', 'panel median'), 3: ('max', 'panel maximum')}
            for level in (1, 2, 3):
                chosen = LEVEL_FACETS[a][level]
                if not chosen:
                    levels[str(level)] = {'by_facet': {}, 'reason': 'no ability requirement'}
                    continue
                stat_key, stat_name = level_spec[level]
                by = {}
                for f in chosen:
                    st = per[f]
                    if st is None:
                        continue
                    same = level == 3 and (a, f) in LEVEL3_ASKS_LEVEL2 and f in prev
                    by[f] = {'target': prev[f] if same else st[stat_key], 'zero': st['min'], 'n': st['n'],
                             'statistic': 'panel median (levels 2 and 3 ask the same of it)' if same else stat_name,
                             **({'ceiling': st['ceiling']} if level == 3 else {})}
                levels[str(level)] = {'by_facet': by}
                prev = {f: r['target'] for f, r in by.items() if r.get('target') is not None}
            l3_facets = levels['3']['by_facet']
            facet_gaps = [
                l3_facets[f]['target'] - facet_targets[f]['levels']['2']['target']
                for f in l3_facets
                if f in facet_targets and l3_facets[f].get('target') is not None
                and facet_targets[f]['levels']['2'].get('target') is not None
            ]
            gap = max((g for g in facet_gaps if g > 0), default=(max(facet_gaps) if facet_gaps else None))
            out[a] = {'levels': levels, 'facets': facet_targets, 'continuity': None if gap is None else round(gap, 4)}
        return out


PAIR_EFFORTS = ('high', 'medium', 'xhigh', 'low', 'max')    # the effort a model's arena/AA pair is read at: nearest High first
AA_TIME, ARENA_TIME = 'aa.cost_time', 'arena.agent_sessions'
MIN_FLEET_RATIO_N = 3       # models that must list both efforts before their time ratio is cited for a model that lists neither


def time_rows(entries, source, readings, point=None):
    """{effort: row} of `source`'s Time/Task listings (minutes > 0). An effort-less listing (stored as `default`) is the
    model's single configuration and is keyed at the effort `readings` (effort_readings) names for it, unless that effort
    is also listed; efforts outside EFFORTS (a non-reasoning variant) are not routable efforts and are left out."""
    rows = {}
    for e in entries or ():
        t = e.get('time_per_task_min')
        if e.get('source') == source and isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
            rows[e.get('effort') or 'default'] = e
    if point is not None:       # an operating point row: its one listing (point_listing), at the effort `default` reads
        k = point_listing(rows, point, readings)
        return {readings['default']: rows[k]} if k is not None else {}
    for name, reads in readings.items():
        if name in rows:
            rows.setdefault(reads, rows.pop(name))
    return {k: v for k, v in rows.items() if k in EFFORTS or k == 'none'}


def anchor_minutes(anchor_rows):
    """The anchor model's Artificial Analysis Time/Task at High in minutes. Without that row every duration would be on a
    scale nothing ties to a cited value, so the build stops."""
    row = anchor_rows.get('high')
    if not row:
        raise ValueError(f'the anchor {measurements.ANCHOR} has no Artificial Analysis Time/Task at High in the public '
                         'measurements: durations cannot be put on one cited scale')
    return row['time_per_task_min']


def nearest_effort(known, effort):
    """The listed effort of `known` closest to `effort` on the effort ladder (the lower one on a tie)."""
    at = EFFORTS.index(effort)
    return min(known, key=lambda e: (abs(EFFORTS.index(e) - at), EFFORTS.index(e)))


def time_size_step(listed, relations):
    """The Time/Task size step (owner 2026-11-18): {'ratio', 'median_ratio', 'n', 'agree', 'reason'?, 'basis', 'pairs'}.
    One log ratio log(small / large) per declared same-version pair (`relations`: series.tier_up) both listed at High
    (`listed`: {model: minutes}); `ratio` is the exp of their median when the pairs agree (`pair_agreement`), else 1: the
    larger model's time."""
    pairs = [(s, r['model']) for s, r in sorted(relations.items())
             if r['version'] == 'same' and listed.get(s) and listed.get(r['model'])]
    logs = [float(np.log(listed[s] / listed[large])) for s, large in pairs]
    rec = pair_agreement(logs)
    return {'ratio': round(float(np.exp(np.median(logs))), 4) if rec['agree'] else 1.0,
            'median_ratio': round(float(np.exp(np.median(logs))), 4) if logs else None, 'n': rec['n'], 'agree': rec['agree'],
            **({'reason': rec['reason']} if 'reason' in rec else {}), 'basis': 'time_per_task',
            'pairs': [{'small': s, 'large': large, 'ratio': round(listed[s] / listed[large], 4)} for s, large in pairs]}


def apply_time_size_step(minutes, basis, relations, step):
    """A small row with no time of its own takes its larger model's (same or nearest version) times the size step's
    `ratio` at every effort (`time_size_step`); `minutes` and `basis` ({model: {effort: ...}}) are updated in place."""
    for m, r in sorted(relations.items()):
        if minutes[m] or not minutes[r['model']]:
            continue
        for e, t in minutes[r['model']].items():
            minutes[m][e] = round(t * step['ratio'], 3)
            basis[m][e] = {'method': 'size_step', 'base_model': r['model'], 'version': r['version'], 'ratio': step['ratio'],
                           'n': step['n'], 'agree': step['agree'], **({'reason': step['reason']} if 'reason' in step else {}),
                           'pairs': [[p['small'], p['large']] for p in step['pairs']], 'source': AA_TIME}


def cross_line_times(scorer, rows, acc, same, minutes, basis, skip):
    """Time/Task of a row with no reading of its own or of its line, from the maker's other lines (owner 2026-11-19): the
    relatives of another line, same maker and size, read on a board at some effort (Artificial Analysis or the arena
    fit; an estimate is never another row's basis, so a model entry's estimate-only release row moves no row), at the
    nearest version distance up to the declared one, with their times after the effort conversion. Where each relative's line and the row's own line are both read
    (Artificial Analysis or the arena fit) on at least two same-version pairs, the relative's times the median ratio own / other over those pairs at High (method
    `cross_line_extrapolation`, n and spread shown), the median over the relatives; else the median of the relatives'
    times (method `cross_line_median`). `minutes` and `basis` are updated in place; rows in `skip` are left alone."""
    pool = without_entries(series.groups({**acc, 'same_model': same}, 'ability'), scorer.entries)
    line_of, versions, small = series.lines(acc), series.versions(acc), series.small_rows(acc)
    up_to = estimation_order(acc)['sibling_first_up_to_distance']
    read = {b for b in rows if 'high' in minutes[b]
            and (basis[b].get('high') or {}).get('method') in ('aa', 'arena_fit')}
    timed = {b for b in rows if any(x.get('method') in ('aa', 'arena_fit') for x in basis[b].values())}
    for m in sorted(rows):
        if minutes[m] or m in skip or (m in same and 'duration' not in (same[m].get('carries') or ())
                                       and 'duration' not in (same[m].get('own_estimated') or ())):
            continue
        mine = line_of.get(m)
        cands = [b for b in pool.get(m, ()) if line_of.get(b) != mine and b in timed]
        groups = series.nearest(m, cands, pool.get(m, ()), versions, small, None, line_of, up_to)
        if not groups or groups[0][0] is None:
            continue
        d, group = groups[0]
        lines = {}
        for b in group:
            ours = [x for x in pool.get(m, ()) if line_of.get(x) == mine and x in read]
            theirs = [y for y in pool.get(m, ()) if line_of.get(y) == line_of.get(b) and y in read]
            pairs = sorted((x, y) for x in ours for y in theirs
                           if series.distance(x, y, pool.get(x, ()), versions) == 0)
            ratios = [minutes[x]['high'] / minutes[y]['high'] for x, y in pairs]
            lines[b] = {'line': line_of.get(b), 'n': len(pairs), 'pairs': [list(q) for q in pairs],
                        **({'ratio': round(float(np.median(ratios)), 4),
                            'spread': [round(min(ratios), 4), round(max(ratios), 4)]} if ratios else {})}
        extrapolate = all(r['n'] >= MIN_SIZE_PAIRS for r in lines.values())
        for e in [e for e in LADDER if all(e in minutes[b] for b in group)]:
            vals = [minutes[b][e] * (lines[b]['ratio'] if extrapolate else 1.0) for b in group]
            minutes[m][e] = round(float(np.median(vals)), 3)
            basis[m][e] = {'method': 'cross_line_extrapolation' if extrapolate else 'cross_line_median',
                           'base_model': group, 'version_distance': d, 'base_minutes': [minutes[b][e] for b in group],
                           'source': AA_TIME, 'lines': lines}


def durations(scorer, rows, acc):
    """({model: duration record}, fit or None, why the fit was rejected or None): one basis for the time a task takes,
    Artificial Analysis Time/Task at the same effort, as a multiple of the anchor's at High (module docstring)."""
    reads = scorer.readings
    anchor = anchor_minutes(time_rows(scorer.trusted((rows.get(measurements.ANCHOR) or {}).get('fields', {}).get('throughput')),
                                      AA_TIME, reads))
    aa = {m: time_rows(scorer.trusted(r['fields'].get('throughput')), AA_TIME, reads, scorer.points.get(m))
          for m, r in rows.items()}
    arena = {m: time_rows(scorer.trusted(r['fields'].get('throughput')), ARENA_TIME, reads, scorer.points.get(m))
             for m, r in rows.items()}
    # The reasoning-off listings (`none`) are read as measured, after the efforts the boards' effort ladder fills (below).
    off = {m: t.pop('none') for m, t in aa.items() if 'none' in t}
    for t in arena.values():
        t.pop('none', None)
    pairs = []
    fleet_aa = {m: t for m, t in aa.items() if m not in scorer.entries}   # fleet quantities: snapshot rows (item 8)
    for m in rows:
        if m in scorer.entries:
            continue
        shared = next((e for e in PAIR_EFFORTS if e in aa[m] and e in arena[m]), None)
        if shared:
            pairs.append((arena[m][shared]['time_per_task_min'], aa[m][shared]['time_per_task_min']))
    fit, why = Scorer.fit(pairs, require_skill=True)
    fit_rec = Scorer.fit_record('aa_time~arena_time', fit) if fit else None
    minutes, basis = {}, {}
    for m in rows:
        minutes[m], basis[m] = {}, {}
        if aa[m]:
            for e, row in aa[m].items():
                minutes[m][e] = row['time_per_task_min']
                basis[m][e] = {'method': 'aa', 'source': AA_TIME, 'listed_as': row.get('listed_as'),
                               'as_of': row.get('as_of'), 'url': row.get('url')}
        elif arena[m] and fit:
            for e, row in arena[m].items():
                x = row['time_per_task_min']
                minutes[m][e] = round(Scorer.predict(fit, x), 3)
                basis[m][e] = {'method': 'arena_fit', 'source': ARENA_TIME, 'arena_minutes': x, **fit_rec,
                               'listed_as': row.get('listed_as'), 'as_of': row.get('as_of'), 'url': row.get('url')}
    same = release_same_model(acc, scorer.entries)
    for m, entry in same.items():
        base = entry.get('model')
        if m in rows and 'duration' in (entry.get('carries') or ()) and base in rows and same_model_point(entry) is None:
            for e, t in list(minutes[base].items()):
                if e not in minutes[m]:
                    minutes[m][e] = t
                    basis[m][e] = {'method': 'same_model', 'base_model': base, 'kind': entry.get('kind'),
                                   'url': entry.get('url'), 'as_of': entry.get('as_of'),
                                   'statement': entry.get('statement')}
    declared = series.groups({**acc, 'same_model': same}, 'line')     # Time/Task is a speed quantity: its own line only
    sibs = without_entries(declared, scorer.entries)
    line_of = series.lines(acc)
    measured = {m for m in rows if minutes[m]}
    speed = {m: sp for m, e in same.items() if m in rows and (sp := speed_declaration(e)) is not None}
    for m in rows:
        if minutes[m] or m in speed or (m in same and not {'duration'} & {*(same[m].get('carries') or ()),
                                                                          *(same[m].get('own_estimated') or ())}):
            continue        # a row declared to be another model's channel that does not carry its time keeps the channel's own
        groups = series.nearest(m, [b for b in sibs.get(m, ()) if b in measured], sibs.get(m, ()), series.versions(acc),
                                series.small_rows(acc), same, line_of)
        if groups:
            d, group = groups[0]
            for e in [e for e in LADDER if all(e in minutes[b] for b in group)]:
                minutes[m][e] = round(float(np.median([minutes[b][e] for b in group])), 3)
                basis[m][e] = {'method': 'series', 'base_model': group[0] if len(group) == 1 else group,
                               'version_distance': d, 'source': basis[group[0]][e].get('source'),
                               'base_method': basis[group[0]][e]['method'], 'series': sorted([m, *declared.get(m, ())])}
    for m, sp in sorted(speed.items()):
        base = same[m]['model']
        if minutes[m] or base not in rows:
            continue        # its own Time/Task reading stands
        r = sp['ratio']
        cite = {k: v for k, v in sp.items() if k not in ('ratio', 'stated')}
        for e, t in minutes[base].items():
            minutes[m][e] = round(t / r, 3) if r else t
            basis[m][e] = {'method': 'speed_ratio' if r else 'speed_lower_bound', 'base_model': base, 'base_minutes': t,
                           'base_method': basis[base][e]['method'], 'source': basis[base][e].get('source'),
                           **({'ratio': r, 'stated_ratio': sp['stated']} if r else
                              {'bound': 'upper: no longer than its base (no ratio stated)'}), 'speed': cite}
    # a declared response floor: the rows with no reading of their own; it bounds, last (below), what the other steps give
    floored = sorted(m for m in scorer.response_floor if m in rows
                     and not any(b.get('method') in ('aa', 'arena_fit') for b in basis[m].values()))
    fleet = {}

    def fleet_ratio(e, e0):
        """(median, n, q1, q3) of AA time[e] / time[e0] over the models that list both, or None under 3 models."""
        if (e, e0) not in fleet:
            xs = [t[e]['time_per_task_min'] / t[e0]['time_per_task_min'] for t in fleet_aa.values() if e in t and e0 in t]
            fleet[(e, e0)] = None if len(xs) < MIN_FLEET_RATIO_N else (
                float(np.median(xs)), len(xs), float(np.percentile(xs, 25)), float(np.percentile(xs, 75)))
        return fleet[(e, e0)]

    def fleet_position(e, lo, up):
        """(median p, n, q1, q3) of the position of effort `e` between `lo` and `up` on the log-time scale,
        p = log(t_e / t_lo) / log(t_up / t_lo), over the models whose Artificial Analysis listing has all three efforts
        (and a lo that differs from up), or None under 3 models."""
        if (e, lo, up) not in fleet:
            ps = []
            for t in fleet_aa.values():
                if e in t and lo in t and up in t:
                    span = float(np.log(t[up]['time_per_task_min'] / t[lo]['time_per_task_min']))
                    if span != 0.0:
                        ps.append(float(np.log(t[e]['time_per_task_min'] / t[lo]['time_per_task_min'])) / span)
            fleet[(e, lo, up)] = None if len(ps) < MIN_FLEET_RATIO_N else (
                float(np.median(ps)), len(ps), float(np.percentile(ps, 25)), float(np.percentile(ps, 75)))
        return fleet[(e, lo, up)]
    out = {}
    for m, r in rows.items():
        direct = dict(minutes[m])
        if direct:
            groups = series.nearest(m, sibs.get(m, ()), sibs.get(m, ()), series.versions(acc), series.small_rows(acc), same,
                                    line_of)
            known = dict(direct)

            def side(effort, pool):
                at = EFFORTS.index(effort)
                return ([k for k in pool if EFFORTS.index(k) < at], [k for k in pool if EFFORTS.index(k) > at])

            def hold(t, rec, effort, pool):
                """An effort outside the range of `pool` is never faster than a lower effort of it (nor slower than a higher
                one): held at that time, and marked, when the estimate would be."""
                below, above = side(effort, pool)
                if below:
                    bound, label = max(pool[k] for k in below), 'a lower listed effort'
                    hit = t < bound
                else:
                    bound, label = min(pool[k] for k in above), 'a higher listed effort'
                    hit = t > bound
                if hit:
                    t = bound
                    rec['bounded_by'] = label
                    rec['note'] = (rec['note'] + '; ' if rec.get('note') else '') + f'held at the time of {label}'
                return round(t, 3), rec
            # 1. outside the listed range, the nearest same-series sibling's ratio from the nearest listed effort
            for e in EFFORTS:
                below, above = side(e, direct)
                if e in direct or (below and above):
                    continue
                e0 = nearest_effort(direct, e)
                for _, group in groups:
                    have = [b for b in group if e in aa[b] and e0 in aa[b]]
                    if have:
                        ratio = float(np.median([aa[b][e]['time_per_task_min'] / aa[b][e0]['time_per_task_min'] for b in have]))
                        known[e], basis[m][e] = hold(direct[e0] * ratio, {
                            'method': 'series_effort', 'base_model': have[0] if len(have) == 1 else have, 'from_effort': e0,
                            'ratio': round(ratio, 4), 'source': AA_TIME}, e, direct)
                        break
            # 2. the rest, with the model's own listed and sibling-derived efforts as the endpoints (an effort estimated in
            # this step is never an endpoint of another)
            anchors = dict(known)
            for e in EFFORTS:
                if e in anchors:
                    continue
                below, above = side(e, anchors)
                if below and above:
                    lo, up = max(below, key=LADDER.index), min(above, key=LADDER.index)
                    got = fleet_position(e, lo, up)
                    if got:
                        med, n, q1, q3 = got
                        p = min(max(med, 0.0), 1.0)
                        known[e] = round(anchors[lo] * (anchors[up] / anchors[lo]) ** p, 3)
                        basis[m][e] = {'method': 'fleet_effort_position', 'lower_effort': lo, 'upper_effort': up,
                                       'position': round(med, 4), 'n': n, 'iqr': [round(q1, 4), round(q3, 4)], 'source': AA_TIME,
                                       'note': f'fleet effort position {e} in {lo}..{up}, n={n}, IQR {q1:.2f}-{q3:.2f}'}
                        if p != med:
                            basis[m][e]['bounded_by'] = 'position held within [0, 1]'
                            basis[m][e]['note'] += f'; the median position {med:.2f} is held at {p:.0f}'
                    continue
                e0 = nearest_effort(anchors, e)
                got = fleet_ratio(e, e0)
                if got:
                    med, n, q1, q3 = got
                    known[e], basis[m][e] = hold(anchors[e0] * med, {
                        'method': 'fleet_effort_ratio', 'from_effort': e0, 'ratio': round(med, 4), 'n': n,
                        'iqr': [round(q1, 4), round(q3, 4)], 'source': AA_TIME,
                        'note': f'fleet effort ratio {e}/{e0}, n={n}, IQR {q1:.2f}-{q3:.2f}'}, e, anchors)
            minutes[m].update({e: t for e, t in known.items() if e not in direct})
    # Size step (module docstring): a small row with no time of its own takes its larger model's (same or nearest version)
    # times the size step, the pairs' median ratio at High where they agree, else 1.
    rel = {m: r for m, r in release_tier_up(acc, scorer.entries).items() if m in rows and r['model'] in rows and m not in same}
    listed = {m: minutes[m]['high'] for m in rows if (basis[m].get('high') or {}).get('method') == 'aa'
              and m not in scorer.entries}
    scorer.time_size_step = time_size_step(listed, rel)
    apply_time_size_step(minutes, basis, rel, scorer.time_size_step)
    for m in rows:
        if minutes[m]:
            # Time/Task never shortens as effort rises (owner 2026-11-18): bounded with High as the anchor; a listed time the
            # bound moves is no longer a listing (method `effort_bound`, the listing kept under `was`).
            bounded = user_scores.bound_along_effort(minutes[m], True)
            for e, t in bounded.items():
                if round(float(t), 3) != round(float(minutes[m][e]), 3):
                    ladder = [k for k in LADDER if k in minutes[m]]
                    anchor_e = user_scores.anchor_effort(minutes[m])
                    toward = ladder[ladder.index(e) + (1 if LADDER.index(e) < LADDER.index(anchor_e) else -1)]
                    basis[m][e] = {'method': 'effort_bound', 'bound_by': toward, 'minutes_before': minutes[m][e],
                                   'was': basis[m][e]}
                    minutes[m][e] = round(float(t), 3)
    # Edge efforts (module docstring), after the bound: the neighbour times the fleet's median ratio time(neighbour) /
    # time(next) over the Artificial Analysis listings with both, bounded by no inversion; a `same_model` row carrying the
    # time copies its model's.
    for m, row in sorted(off.items()):
        if m in scorer.points:
            continue
        t = row['time_per_task_min']
        held = user_scores.bound_along_effort({**minutes[m], 'none': t}, True, [k for k in minutes[m]] or ['none'])['none']
        minutes[m]['none'] = round(float(held), 3)
        basis[m]['none'] = {'method': 'aa', 'source': AA_TIME, 'listed_as': row.get('listed_as'), 'as_of': row.get('as_of'),
                            'url': row.get('url'), **({'bounded_from': t} if round(float(held), 3) != round(t, 3) else {})}
    scorer.time_steps = {}
    measured_range = [t['time_per_task_min'] for m in fleet_aa for t in [*aa[m].values(), *([off[m]] if m in off else [])]]
    lo_t, hi_t = (min(measured_range), max(measured_range)) if measured_range else (None, None)
    for edge, (near, (end, nxt)) in EDGES.items():
        if edge_method(edge) == 'fleet_off_step':
            # reasoning off: the fleet's own Time/Task at none against its lowest measured effort, a ratio from the
            # measured pairs as the size step (pair_agreement on the log ratios: n >= MIN_SIZE_PAIRS, one direction); with
            # no fleet model measured at none and at a reasoning effort there is no step (as for the ability facets)
            xs = [off[m]['time_per_task_min'] / aa[m][lo]['time_per_task_min'] for m in off if m not in scorer.entries
                  for lo in [next((e for e in LADDER[1:] if e in aa.get(m, {})), None)] if lo is not None]
            if not xs:
                continue
            agree = pair_agreement([float(np.log(x)) for x in xs])
            scorer.time_steps.setdefault(near, {})[edge] = {
                'ratio': round(float(np.exp(agree['step'])), 4), 'n': agree['n'], 'agree': agree['agree'],
                **({'reason': agree['reason']} if 'reason' in agree else {}), 'sample': 'fleet', 'method': edge_method(edge)}
            continue
        xs = [t[end]['time_per_task_min'] / t[nxt]['time_per_task_min'] for t in fleet_aa.values() if end in t and nxt in t]
        if len(xs) >= MIN_FLEET_RATIO_N:
            scorer.time_steps.setdefault(near, {})[edge] = {'ratio': round(float(np.median(xs)), 4), 'n': len(xs),
                                                            'sample': 'fleet', 'method': edge_method(edge)}
    carriers = {m: e['model'] for m, e in same.items() if m in rows and 'duration' in (e.get('carries') or ()) and e.get('model') in rows}
    for m in rows:
        for edge, (step_key, _) in EDGES.items():
            step = (scorer.time_steps.get(step_key) or {}).get(edge)
            near = user_scores.edge_anchor(edge, minutes[m], [e for e in aa.get(m, {}) if e in minutes[m]])
            if step is None or m in carriers or near is None or edge in minutes[m]:
                continue
            t = minutes[m][near] * step['ratio']
            held = user_scores.bound_along_effort({**minutes[m], edge: t}, True, list(minutes[m]))[edge]
            # An extrapolated time never leaves the range of Time/Task the board measures for the fleet (declared clip).
            # (an estimated neighbour already beyond that range keeps the bound: no inversion along the order)
            clipped = float(user_scores.bound_along_effort({**minutes[m], edge: min(max(float(held), lo_t), hi_t)}, True,
                                                           list(minutes[m]))[edge])
            minutes[m][edge] = round(clipped, 3)
            # the effort whose time held it (the neighbour it came from, or the one on its other side)
            by = near if float(minutes[m][near]) == float(held) else next(
                (e for e in LADDER if e != edge and e in minutes[m] and float(minutes[m][e]) == float(held)), near)
            basis[m][edge] = {'method': step['method'], 'from_effort': near, 'ratio': step['ratio'], 'n': step['n'],
                              'source': AA_TIME, **({'bounded_by': by} if held != t else {}),
                              **({'clipped_to_fleet_range': [lo_t, hi_t]} if clipped != float(held) else {})}
    for m, base in carriers.items():
        for edge in EDGES:
            if edge in minutes[base] and edge not in minutes[m]:
                entry = same[m]
                minutes[m][edge] = minutes[base][edge]
                basis[m][edge] = {'method': 'same_model', 'base_model': base, 'kind': entry.get('kind'),
                                  'url': entry.get('url'), 'as_of': entry.get('as_of'), 'statement': entry.get('statement')}
    for m in scorer.points:     # an operating point row: every effort reads its one time, no effort step
        at = reads['default']
        if m in rows and at in minutes[m]:
            t = minutes[m][at]
            minutes[m] = {e: t for e in LADDER}
            basis[m] = {**{e: {'method': 'operating_point', 'from_effort': at} for e in LADDER if e != at}, at: basis[m][at]}
    # a same_model row with a declared point reads its model's time at that effort at every effort, after every step of
    # the model's own times
    for m, entry in sorted(same.items()):
        at = same_model_point(entry)
        base = entry.get('model')
        if at is None or m not in rows or 'duration' not in (entry.get('carries') or ()) or at not in minutes.get(base, {}):
            continue
        t, how = minutes[base][at], basis[base][at]
        minutes[m] = dict.fromkeys(LADDER, t)
        basis[m] = {e: {'method': 'same_model', 'base_model': base, 'at_effort': at, 'base_minutes': t,
                        'base_method': how['method'], 'kind': entry.get('kind'), 'url': entry.get('url'),
                        'as_of': entry.get('as_of'), 'statement': entry.get('statement'),
                        'at_effort_declaration': {k: v for k, v in entry['at_effort'].items() if k != 'effort'}}
                    for e in LADDER}
    # the maker's other lines, after every step of the row's own line and of the fleet's effort conversion
    cross_line_times(scorer, rows, acc, same, minutes, basis, set(speed) | set(scorer.points))
    for m in floored:   # the floor bounds: the longer of it and the other steps' time, then bounded along effort
        decl = scorer.response_floor[m]
        floor = round(decl['min_seconds'] / 60.0, 3)      # a task is at least one response (calibration/README.md)
        given, how = dict(minutes[m]), dict(basis[m])
        binds = {e: 'estimate' if given.get(e) is not None and given[e] >= floor else 'floor' for e in LADDER}
        moved = set()
        minutes[m] = bound_curve({e: given[e] if binds[e] == 'estimate' else floor for e in LADDER}, True, moved)
        basis[m] = {e: {'method': 'declared_response_floor', 'binds': binds[e], 'min_seconds': decl['min_seconds'],
                        'floor_minutes': floor,
                        'estimate': {'minutes': given[e], **how[e]} if given.get(e) is not None else None,
                        **({'bounded_by': 'the time at a neighbouring effort (no inversion along effort)'} if e in moved else {}),
                        'bound': 'lower: a task takes at least one response of at least the declared time',
                        'declaration': {k: v for k, v in decl.items() if k != 'min_seconds'}} for e in LADDER}
    for m in rows:
        out[m] = {'status': 'ok' if minutes[m] else 'missing', 'anchor_minutes': anchor,
                  'by_effort': {e: round(t / anchor, 4) for e, t in minutes[m].items()},
                  'minutes_by_effort': dict(minutes[m]), 'basis_by_effort': basis[m],
                  'efforts_without_time': [e for e in LADDER if e not in minutes[m]]}
    return out, fit_rec, why


def cite(o, **extra):
    """An entry of a capacity row: the fact with its date (`as_of`); its age is the reader's (data_age), never stored."""
    return {k: o.get(k) for k in ('source', 'kind', 'value', 'url', 'as_of', 'fetched_at', 'method')} | extra


def entry_date(e):
    """The date an entry rests on: a derived entry its oldest input's (`rests_on_as_of`), any other its own `as_of`."""
    return data_age.data_date(e.get('rests_on_as_of') or e.get('as_of'))


def oldest_as_of(*dates):
    """The oldest of ISO dates as written; None when one is not a date (then the result cannot be dated either)."""
    parsed = [(data_age.data_date(d), d) for d in dates]
    return None if any(day is None for day, _ in parsed) else min(parsed)[1]


def window_capacity(fam, plan, wid, ctx, rung_max=3):
    """One window of one plan down the capacity ladder (round 5): rung 1 the community observations (else a window
    ratio a community source states, rung 1 derived), rung 2 time-to-exhaust reports x the reported model's usd, rung 3
    a vendor plan multiplier x the sibling plan's value. The first rung with a value wins; rung 3 is also recorded as a
    cross-check where a lower rung won."""
    usd = ctx['usd']
    entries, rung = [cite(o, rung=1) for o in ctx['obs'].get((fam, plan, wid), ())], 1
    rule = ctx['ratios'].get((fam, wid))
    if not entries and rule:
        base = window_capacity(fam, plan, rule['of_window'], {**ctx, 'ratios': {}}, rung_max)
        if base['capacity_usd'] is not None:
            entries, rung = [{'source': rule['source'], 'kind': 'derived', 'value': round(rule['ratio'] * base['capacity_usd'], 2),
                              'url': rule['url'], 'as_of': rule['as_of'], 'fetched_at': rule.get('fetched_at'),
                              'method': f"{rule['ratio']:g} x the {rule['of_window']} ({rule['statement']}; {rule['original']})",
                              'derived_from': base['sources'],
                              'rests_on_as_of': oldest_as_of(rule.get('as_of'), base['as_of']), 'rung': base['rung']}], base['rung']
    if not entries and rung_max >= 2:
        for r in ctx['exhaust'].get((fam, plan, wid), ()):
            rate = (usd.get(r['model']) or {}).get('value')
            if rate:
                entries.append(cite(r, rung=2, kind='derived', value=round(r['hours'] * rate, 2),
                                    method=f"{r['hours']:g} h of {r['model']} until the window ran out x its usd {rate:g}/h "
                                           "(usd_per_hour)", report=r.get('quote')))
        rung = 2
    mult = ctx['mults'].get((fam, plan))
    sibling = None
    if mult and wid in mult['windows'] and rung_max >= 3:
        base = window_capacity(fam, mult['of_plan'], wid, {**ctx, 'mults': {}}, 2)
        if base['capacity_usd'] is not None:
            sibling = {'source': mult['source'], 'kind': 'derived', 'value': round(mult['factor'] * base['capacity_usd'], 2),
                       'url': mult['url'], 'as_of': mult['as_of'], 'fetched_at': mult.get('fetched_at'),
                       'method': f"{mult['factor']:g} x {fam}/{mult['of_plan']} {wid} ({mult['statement']})",
                       'derived_from': base['sources'], 'rests_on_as_of': oldest_as_of(mult.get('as_of'), base['as_of']),
                       'rung': 3}
    if not entries and sibling:
        entries, rung, sibling = [sibling], 3, None
    ref = (REFERENCE_MODELS.get(fam) or {}).get(wid)
    ref_usd = ((usd.get(ref) or {}).get('value')) if ref else None
    if not entries:
        return {'unit': 'usd', 'capacity_usd': None, 'status': 'error', 'rung': None, 'sources': [], 'reference_model': ref,
                'ref_hours': None, 'as_of': None}
    value = round(statistics.median(e['value'] for e in entries), 2)
    dated = [(day, e.get('rests_on_as_of') or e['as_of']) for e in entries for day in [entry_date(e)] if day is not None]
    # `as_of`: the newest date the value rests on (None when no entry is dated); how old that is is the reader's (data_age)
    row = {'unit': 'usd', 'capacity_usd': value, 'status': 'ok', 'rung': rung,
           'as_of': max(dated)[1] if dated else None, 'merge': f'median of {len(entries)} value(s), rung {rung}', 'sources': entries,
           'reference_model': ref, 'ref_hours': round(value / ref_usd, 1) if ref_usd else None}
    if sibling:
        row['cross_check'] = {'rung': 3, 'value': sibling['value'], 'method': sibling['method'],
                              'ratio_to_value': round(sibling['value'] / value, 2) if value else None}
    return row


def resolve_plan(table, plan):
    """(plan whose windows apply, alias or None) for a plan-templates entry that may be `{"alias": ...}`."""
    target, alias = plan, None
    for _ in range(3):
        nxt = (table.get(target) or {}).get('alias')
        if not isinstance(nxt, str):
            break
        target = alias = nxt
    return target, alias


def capacity(meas, plans, usd):
    """({family: {plan: {window: capacity row}}}, data errors) down the capacity ladder from `meas`. Facts and their dates
    only: how old a source is, and whether that is stale, is computed where it is read (data_age), so the file does not
    change with the calendar."""
    cap = meas.get('capacity') or {}
    obs = {}
    for o in cap.get('observations') or ():
        obs.setdefault((o['family'], o['plan'], o['window']), []).append(o)
    rules = cap.get('rules') or ()
    ctx = {'usd': usd, 'obs': obs,
           'ratios': {(r['family'], r['window']): r for r in rules if r.get('type', 'window_ratio') == 'window_ratio'},
           'mults': {(r['family'], r['plan']): r for r in rules if r.get('type') == 'plan_multiplier'},
           'exhaust': {}}
    for r in cap.get('exhaust') or ():
        ctx['exhaust'].setdefault((r['family'], r['plan'], r['window']), []).append(r)
    errors = [{'field': 'capacity', **e} for e in cap.get('errors') or ()]
    unmeasured = {(u['family'], u['plan'], u['window']): u for u in cap.get('unmeasured') or ()}
    out = {}
    for fam, table in plans.items():
        if not isinstance(table, dict):
            continue
        for plan, spec in table.items():
            if not isinstance(spec, dict):
                continue
            target, alias = resolve_plan(table, plan)
            for wid in (table.get(target) or {}).get('windows') or {}:
                row = window_capacity(fam, target, wid, ctx)
                if alias:
                    row['alias_of'] = alias
                out.setdefault(fam, {}).setdefault(plan, {})[wid] = row
                if alias:
                    continue
                if row['status'] == 'error':
                    effect = 'the bundles drawing this window stay out of automatic routing (name the model to dispatch it)'
                    declared = unmeasured.get((fam, target, wid))
                    if declared:    # a known gap the maintainers declared: listed, not a failed refresh
                        errors.append({'field': 'capacity', 'family': fam, 'plan': plan, 'window': wid, 'level': 'yellow',
                                       'reason': 'capacity_unmeasured', 'as_of': declared['as_of'],
                                       'statement': declared['statement'], 'effect': effect})
                    else:
                        errors.append({'field': 'capacity', 'family': fam, 'plan': plan, 'window': wid, 'level': 'red',
                                       'reason': 'capacity_ladder_empty',
                                       'effect': 'no community measurement, time-to-exhaust report or vendor multiplier '
                                                 f'for this window: {effect}'})
    return out, errors


def source_dates(meas):
    """{source: {kind, as_of, as_of_kind, fetched_at}} for every source the measurements cite (a source not fetched or not
    used: {missing, reason, url}). Dates only; how old each is, and whether that is stale, is the reader's (data_age)."""
    out = {}
    for src, row in sorted((meas.get('sources') or {}).items()):
        out[src] = {'kind': row.get('kind'), 'as_of': row.get('as_of'), 'as_of_kind': row.get('as_of_kind'),
                    'fetched_at': row.get('fetched_at')}
    for src, row in sorted((meas.get('missing_sources') or {}).items()):
        out[src] = {'kind': None, 'missing': True, 'reason': row.get('reason'), 'url': row.get('url')}
    return out


# --- usd per worker-hour ------------------------------------------------------------------------------------------


def arena_usd_miss_reason(scorer, model, raw_entries, meas):
    """True reason `arena.agent_sessions` gave no value for `model` (round 9 F3: never say 'not listed' when the source
    was distrusted, not captured, or listed the model without Cost/Task or Time/Task)."""
    if any(e.get('source') == 'arena.agent_sessions' for e in raw_entries or ()) and 'arena.agent_sessions' in scorer.distrust:
        return 'arena.agent_sessions distrusted'
    missing = ((meas or {}).get('missing_sources') or {}).get('arena.agent_sessions')
    if missing:
        return f"arena.agent_sessions unavailable ({missing.get('reason') or 'not captured'})"
    unpriced = ((((meas or {}).get('sources') or {}).get('arena.agent_sessions') or {}).get('unpriced') or {}).get(model)
    if unpriced:
        return unpriced['reason']
    return 'not listed on arena.ai Agent / Overall'


USD_ORDER = ('arena.agent_sessions', 'derived.community_mix')


def usd_community_mix(scorer, rows, meas):
    """The one usd_per_hour basis (rounds 8-9, user 2026-11-09): `arena.agent_sessions` (arena.ai Agent / Overall real
    sessions, Cost/Task P50 / Time/Task P50 x 60, kind `community`), else the labelled option B (`derived.community_mix`,
    AA throughput x community token mix x list price; `fallback: True`) for a model arena does not list with cost and
    time; `convert_usd` then replaces that for a model with a same-series sibling that has a direct value."""
    out = {}
    for m, r in rows.items():
        raw = r['fields'].get('usd_per_hour') or ()
        by_src = {}
        for e in scorer.trusted(raw):
            by_src.setdefault(e['source'], []).append(e)
        chosen = next((s for s in USD_ORDER if s in by_src), None)
        if not chosen:
            out[m] = {'value': None, 'status': 'missing', 'sources': {}, 'merge': None,
                      'reason': arena_usd_miss_reason(scorer, m, raw, meas)}
            continue
        picked = pick_usd(by_src[chosen])
        is_fb = chosen != 'arena.agent_sessions'
        kind = by_src[chosen][0].get('kind') or ('derived' if is_fb else 'community')
        merge = f"fallback ({arena_usd_miss_reason(scorer, m, raw, meas)}): {picked['merge']}" if is_fb else picked['merge']
        out[m] = {**picked, 'merge': merge, 'kind': kind, 'fallback': is_fb}
    return out


def axis_evidence(axes):
    """Round 058 review F5: per axis, the measured sources that are not Artificial Analysis (the AA boards are one
    source); `aa_only` when no axis has any, though some axis has a value."""
    indep = {a: sorted({s for c in (ax.get('facets') or {}).values() if str(c.get('status', '')).startswith('measured')
                        for s in (c.get('sources') or {}) if not s.startswith('aa.')}) for a, ax in axes.items()}
    valued = any(ax.get('status') in ('measured', 'measured_partial', 'interpolated', 'partial', 'estimated')
                 for ax in axes.values())
    return {'independent_sources': indep, 'aa_only': valued and not any(indep.values())}


def token_mix(meas):
    """The community token mix shares (public-measurements.json `capacity.token_mix`), every numeric component."""
    mix = (meas.get('capacity') or {}).get('token_mix') or {}
    shares = {k: v for k, v in mix.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    return shares if sum(shares.values()) > 0 else {}


def pair_prices(entries_t, entries_b):
    """(price of t, price of b) for a list-price ratio (pick_price): from the first price source that lists both models, so
    the ratio is read on one price list, else each model's own first."""
    for src in PRICE_SOURCES:
        pt, pb = (pick_price([e for e in entries or () if e.get('source') == src]) for entries in (entries_t, entries_b))
        if pt and pb:
            return pt, pb
    return pick_price(entries_t), pick_price(entries_b)


def list_price_ratio(price_t, price_b, mix):
    """The ratio of two cited API list prices (pick_price) over the token mix, or None when either lacks a component."""
    if not mix or price_t is None or price_b is None:
        return None
    if any(not isinstance(p['value'].get(c), (int, float)) for p in (price_t, price_b) for c in mix):
        return None
    cost_t, cost_b = (sum(p['value'][c] * share for c, share in mix.items()) for p in (price_t, price_b))
    return cost_t / cost_b if cost_b > 0 else None


def convert_usd(usd, rows, scorer, meas, acc):
    """Series conversion of usd_per_hour: a model without a direct value (`derived` or missing) and with a same-series
    sibling that has one takes the nearest such sibling's value times the ratio of the two cited API list prices over
    the full community token mix; one with no such sibling takes, the same way, its larger model's (series.tier_up,
    `relation: size`) when that has a direct value."""
    direct = {m for m, u in usd.items() if u.get('value') is not None and u.get('kind') not in ('derived', 'converted')}
    same = release_same_model(acc, scorer.entries)
    sibs = without_entries(series.groups({**acc, 'same_model': same}, 'line'), scorer.entries)   # a speed and price quantity
    line_of = series.lines(acc)
    mix = token_mix(meas)
    larger = release_tier_up(acc, scorer.entries)
    for m in rows:
        if m in direct:
            continue
        groups = series.nearest(m, [b for b in sibs.get(m, ()) if b in direct], sibs.get(m, ()), series.versions(acc),
                                series.small_rows(acc), same, line_of)
        near = groups[0][1] if groups else []
        conv = []
        for b in near:
            prices = dict(zip((m, b), pair_prices(*(scorer.trusted(rows[k]['fields'].get('price')) for k in (m, b)))))
            ratio = list_price_ratio(prices[m], prices[b], mix)
            if ratio is not None:
                conv.append((usd[b]['value'] * ratio, b, ratio, prices))
        if len(conv) > 1:
            usd[m] = {'value': round(float(np.median([c[0] for c in conv])), 3), 'status': 'ok', 'kind': 'converted',
                      'fallback': False, 'relation': 'series',
                      'merge': 'series conversion: the median over the tied relatives of usd x API list price ratio over the '
                               'community token mix', 'used': [],
                      'sources': {'bases': [{**usd[b], 'model': b, 'ratio': round(r, 4), 'prices': p} for _, b, r, p in conv],
                                  'token_mix': mix}}
            continue
        up = [larger[m]['model']] if m in larger and larger[m]['model'] in direct else []
        for b in [*near, *up]:
            prices = dict(zip((m, b), pair_prices(*(scorer.trusted(rows[k]['fields'].get('price')) for k in (m, b)))))
            ratio = list_price_ratio(prices[m], prices[b], mix)
            if ratio is None:
                continue
            how = 'series' if b in near else 'size'
            usd[m] = {'value': round(usd[b]['value'] * ratio, 3), 'status': 'ok', 'kind': 'converted', 'fallback': False,
                      'relation': how,
                      'merge': f'{how} conversion: usd of {b} x API list price ratio {ratio:.4f} over the community token mix',
                      'used': [],
                      'sources': {'base': {**usd[b], 'model': b}, 'ratio': round(ratio, 4), 'prices': prices, 'token_mix': mix}}
            sp = speed_declaration(same.get(m)) if (same.get(m) or {}).get('model') == b else None
            if sp is not None:
                # a speed variant at its own list price: r times the base's tokens an hour (cost per task: the price ratio)
                usd[m]['value'] = round(usd[m]['value'] * (sp['ratio'] or 1.0), 3)
                usd[m]['speed'] = ({'ratio': sp['ratio'], 'stated_ratio': sp['stated']} if sp['ratio'] else
                                   {'bound': "lower: at the base's tokens an hour (no ratio stated)"})
                usd[m]['merge'] += (f" x the stated speed ratio {sp['ratio']}" if sp['ratio'] else
                                    "; a lower bound: the variant's speed ratio is not stated")
            break
    for m, entry in sorted(same.items()):
        base = entry.get('model')
        if (m in usd and usd[m].get('value') is None
                and 'usd_per_hour' in (entry.get('carries') or ())
                and (usd.get(base) or {}).get('value') is not None):
            decl = {k: entry[k] for k in ('kind', 'statement', 'url', 'as_of') if entry.get(k)}
            usd[m] = {
                'value': usd[base]['value'],
                'status': 'ok',
                'kind': entry.get('kind') or 'maintainer',
                'method': 'same_model',
                'base_model': base,
                'fallback': False,
                'merge': f"same_model: usd_per_hour of {base} ({entry.get('statement') or entry.get('url') or entry.get('kind')})",
                'used': list(usd[base].get('used') or ()),
                **({k: v for k, v in decl.items() if k != 'kind'}),
                'sources': {'base': {**usd[base], 'model': base}, 'declaration': decl},
            }


def scale_fallback_usd(usd, rows, scorer):
    """A usd_per_hour that still rests on the fallback source alone (`derived.community_mix`, `fallback: True`, after
    convert_usd) is put on the primary source's scale (owner 2026-11-19, round 6 review finding 11: the fallback runs high
    against arena sessions): divided by the fleet's median ratio fallback / primary over the rows that have both
    (`scorer.trusted`, each source read by pick_usd), when at least MIN_FLEET_RATIO_N rows do. The cell keeps the
    fallback's own value and states the ratio with its n and spread (`on_primary_scale`), marked estimated."""
    pairs = {}
    for m, r in rows.items():
        if m in scorer.entries:
            continue        # a fleet quantity: the release snapshot's rows only (round 6 item 8)
        by_src = {}
        for e in scorer.trusted(r['fields'].get('usd_per_hour') or ()):
            by_src.setdefault(e['source'], []).append(e)
        if all(src in by_src for src in USD_ORDER):
            primary, fallback = (pick_usd(by_src[src])['value'] for src in USD_ORDER)
            if primary and fallback and primary > 0 and fallback > 0:
                pairs[m] = fallback / primary
    if len(pairs) < MIN_FLEET_RATIO_N:
        return
    ratios = sorted(pairs.values())
    scale = {'ratio': round(float(np.median(ratios)), 4), 'n': len(ratios),
             'spread': {'min': round(ratios[0], 4), 'p10': round(float(np.percentile(ratios, 10)), 4),
                        'p90': round(float(np.percentile(ratios, 90)), 4), 'max': round(ratios[-1], 4)},
             'rows': sorted(pairs), 'of': f'{USD_ORDER[1]} / {USD_ORDER[0]}'}
    for m, u in usd.items():
        if u.get('fallback') and u.get('value') is not None and m not in pairs:
            usd[m] = {**u, 'value': round(u['value'] / scale['ratio'], 3), 'estimated': True,
                      'on_primary_scale': {**scale, 'fallback_value': u['value']},
                      'merge': (f"{u['merge']}; put on the {USD_ORDER[0]} scale: divided by the fleet's median ratio "
                                f"{scale['ratio']:.4f} of the two sources over the {scale['n']} rows that have both")}


class PanelChangeRefused(ValueError):
    """The panel's members changed and the change was not accepted (`--accept-panel-change`): nothing is written."""


def previous_doc(path):
    """The five-axis-scores.json a run is about to replace, or None when there is none yet."""
    target = Path(path)
    return json.loads(target.read_text(encoding='utf-8-sig')) if target.is_file() else None


def panel_sd(meas):
    """{'<category>.<facet>': {board: sd_panel}}: each scaled capability board's panel standard deviation in its raw unit
    (measurements.py `scale`), kept so the next run can report how a panel change moves it."""
    out = {}
    for src, (axis, facet) in measurements.CAPABILITY_SOURCES.items():
        scale = ((meas.get('sources') or {}).get(src) or {}).get('scale')
        if scale:
            out.setdefault(f'{axis}.{facet}', {})[src] = scale['sd_panel']
    return out


def _level_points(levels):
    """t1, t2, t3 and the level-3 ceiling of one `levels` map (a category's or a facet's)."""
    levels = levels or {}
    return {'t1': (levels.get('1') or {}).get('target'), 't2': (levels.get('2') or {}).get('target'),
            't3': (levels.get('3') or {}).get('target'),
            'ceiling': ((levels.get('3') or {}).get('ceiling') or {}).get('value')}


def panel_change(previous, anchor):
    """None when this run's `anchor` block has the same panel members as `previous` (the anchor block it replaces); else
    {'added', 'removed', 'shifts'}, `shifts` holding per category (and per facet) the old and new t1, t2, t3 and ceiling, and
    per facet the old and new sd_panel of each board."""
    old_members, new_members = set(previous['panel']), set(anchor['panel'])
    if old_members == new_members:
        return None
    pair = lambda old, new: {'old': old, 'new': new}
    old_sd, new_sd = previous.get('sd_panel') or {}, anchor['sd_panel']
    shifts = {}
    for a, facets in AXES.items():
        old_t, new_t = previous['targets'].get(a) or {}, anchor['targets'].get(a) or {}
        row = {}
        if a not in LEVEL_FACETS:
            o, n = _level_points(old_t.get('levels')), _level_points(new_t.get('levels'))
            row = {k: pair(o[k], n[k]) for k in o}
        row['facets'] = {}
        for f in facets:
            key = f'{a}.{f}'
            o = _level_points(((old_t.get('facets') or {}).get(f) or {}).get('levels'))
            n = _level_points(((new_t.get('facets') or {}).get(f) or {}).get('levels'))
            boards = sorted(set(old_sd.get(key) or {}) | set(new_sd.get(key) or {}))
            row['facets'][f] = {**{k: pair(o[k], n[k]) for k in o},
                                'sd_panel': {b: pair((old_sd.get(key) or {}).get(b), (new_sd.get(key) or {}).get(b))
                                             for b in boards}}
        shifts[a] = row
    return {'added': sorted(new_members - old_members), 'removed': sorted(old_members - new_members), 'shifts': shifts}


def panel_change_text(change):
    """The panel change as the lines a refresh prints."""
    num = lambda v: '-' if v is None else f'{v:g}'
    moved = lambda rec, keys: ', '.join(f"{k} {num(rec[k]['old'])} -> {num(rec[k]['new'])}" for k in keys if k in rec)
    lines = [f"panel membership changed: added {', '.join(change['added']) or 'none'}; "
             f"removed {', '.join(change['removed']) or 'none'}"]
    for a, row in change['shifts'].items():
        cat = moved(row, ('t1', 't2', 't3', 'ceiling'))
        lines.append(f'  {a}' + (f': {cat}' if cat else ''))
        for f, rec in row['facets'].items():
            lines.append(f"    {a}.{f}: {moved(rec, ('t1', 't2', 't3', 'ceiling'))}; "
                         f"sd_panel {moved(rec['sd_panel'], rec['sd_panel']) or '-'}")
    return '\n'.join(lines)


def guard_panel(previous, anchor, accept, stamp):
    """The `anchor.panel_change` record to write (module docstring, panel guard). No previous file: None. Same members:
    the previous record, carried forward. Changed members: refused (`PanelChangeRefused`, carrying the report) unless
    `accept`, then printed and recorded with the date."""
    if previous is None:
        return None
    change = panel_change(previous['anchor'], anchor)
    if change is None:
        return previous['anchor'].get('panel_change')
    text = panel_change_text(change)
    if not accept:
        raise PanelChangeRefused(f'{text}\nnothing written: a panel change moves sd_panel, t1, t2, t3 and the exchange rate '
                                 'between ability and quota cost; rerun with --accept-panel-change to accept it')
    print(text)
    return {'date': stamp.date().isoformat(), **change}


def check_scales(targets):
    """ValueError naming the first category or facet whose panel does not give three different level targets. A scale of
    `t1 == t2` or `t2 == t3` makes two levels the same (tasks of both ask the same and every hand-written level between them
    maps to one value); the document is not written. Nothing to accept: the data (another source state) is what changes."""
    for category, block in targets.items():
        scales = {f'{category}.{f}': row.get('levels') for f, row in (block.get('facets') or {}).items()}
        if category not in LEVEL_FACETS:
            scales[category] = block.get('levels')
        for name, levels in scales.items():
            t = [((levels or {}).get(k) or {}).get('target') for k in '123']
            if None not in t and not t[0] < t[1] < t[2]:
                raise ValueError(f'degenerate level scale on {name}: panel minimum {t[0]}, median {t[1]}, maximum {t[2]} '
                                 '(levels 1, 2 and 3 need three different values; the panel does not give them)')


def build(meas, acc, plans, distrust=(), meas_bytes=b'', now=None, previous=None, accept_panel_change=False):
    """The five-axis-scores.json document. `previous` is the document it replaces (None when there is none): a panel whose
    members differ from it is refused unless `accept_panel_change` (`guard_panel`)."""
    scorer = Scorer(meas, acc, distrust)
    rows, tier, fits, rejected, links, targets = scorer.score()
    check_scales(targets)
    usd = usd_community_mix(scorer, rows, meas)
    convert_usd(usd, rows, scorer, meas, acc)
    scale_fallback_usd(usd, rows, scorer)
    caps, cap_errors = capacity(meas, plans, usd)
    dur, dur_fit, dur_rejected = durations(scorer, rows, acc)
    models, errors = {}, []
    for m, r in rows.items():
        model_errors = []
        if usd[m]['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'usd_per_hour', 'reason': 'no_cited_source', 'level': 'red_if_metered',
                                 'effect': 'out of automatic routing on any quota-pool or metered account; a named '
                                           'dispatch runs with its cost unpriced'})
        if dur[m]['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'duration_multiplier', 'reason': 'no_time_per_task', 'level': 'red',
                                 'effect': 'no Artificial Analysis Time/Task, and no arena.ai Agent Time/Task with a fit that has '
                                           'skill: out of automatic routing at every effort; a named dispatch carries the '
                                           'task\'s own hours estimate unscaled'})
        elif dur[m]['efforts_without_time']:
            model_errors.append({'model': m, 'field': 'duration_multiplier', 'reason': 'effort_without_time', 'level': 'yellow',
                                 'effect': 'out of automatic routing at ' + ', '.join(dur[m]['efforts_without_time'])
                                           + ' (no Time/Task at that effort and no same-series sibling to convert it); '
                                             'the listed efforts still route'})
        img = pick_image_input(scorer.trusted(r['fields'].get('image_input')))
        if img['status'] == 'unknown':
            model_errors.append({'model': m, 'field': 'image_input', 'reason': 'no_cited_source',
                                 'level': 'red_if_vision_demanded',
                                 'effect': 'unknown image-input modality; excluded from automatic routing when '
                                           'tool_operation is 3 (mode B)'})
        fm = pick_fast_mode(m, scorer.trusted(r['fields'].get('fast_mode')), scorer.models)
        if fm['status'] == 'missing':
            model_errors.append({'model': m, 'field': 'fast_mode', 'reason': 'no_cited_source', 'level': 'yellow',
                                 'effect': 'fast mode is not priced or enabled from a policy multiplier on the five-axis path'})
        for a, ax in r['axes'].items():
            if ax.get('flag') == 'missing':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_trusted_source', 'level': 'red_if_demanded',
                                     'effect': 'no value: a task that demands this category leaves automatic routing for the '
                                               'model (mode B)'})
            elif ax.get('missing_facets'):
                model_errors.append({'model': m, 'field': f"{a}.{','.join(ax['missing_facets'])}",
                                     'reason': 'facet_unmeasured_no_estimate', 'level': 'red_if_demanded',
                                     'effect': 'missing facet with no estimate: excluded under mode B at levels requiring this facet'})
            elif ax.get('flag') == 'partial':
                model_errors.append({'model': m, 'field': a, 'reason': 'no_high_measurement', 'level': 'yellow',
                                     'effect': 'measured only at non-High or disjoint efforts across facets: excluded under mode B '
                                               'at unmeasured efforts until filled via user-scores.json'})
        evidence = axis_evidence(r['axes'])
        if evidence['aa_only']:
            model_errors.append({'model': m, 'field': 'axes', 'reason': 'aa_only', 'level': 'yellow',
                                 'effect': 'every axis value comes from Artificial Analysis (the Intelligence Index and AA '
                                           'boards collinear with it): one source, not independent evidence'})
        if tier[m]['method'] == 'none':
            model_errors.append({'model': m, 'field': 'tier', 'reason': 'no_overall_board', 'level': 'yellow',
                                 'effect': 'no arena.ai Overall reading, no other overall board and no same-series sibling: '
                                           'its type scores that no board measures stay missing (mode B when demanded)'})
        errors += model_errors
        idx = scorer.trusted(r['fields'].get('intelligence_index'))
        models[m] = {'axes': r['axes'], 'scores': {a: ax.get('value') for a, ax in r['axes'].items()},
                     'missing_axes': [a for a, ax in r['axes'].items() if ax.get('flag') == 'missing'],
                     'usd_per_hour': usd[m], 'duration_multiplier': dur[m],
                     'image_input': img, 'fast_mode': fm,
                     'price': pick_price(scorer.trusted(r['fields'].get('price'))),
                     'intelligence_index': idx[0]['value'] if idx else None,
                     'tier': tier[m], 'evidence': evidence, 'data_errors': model_errors}
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    anchor = {'model': measurements.ANCHOR, 'value': 0.0, 'scale': 'panel standard score',
              'panel': (meas.get('panel') or {}).get('models') or [], 'panel_n': (meas.get('panel') or {}).get('n'),
              'targets': targets, 'sd_panel': panel_sd(meas)}
    anchor['panel_change'] = guard_panel(previous, anchor, accept_panel_change, stamp)
    doc = {'schema': SCHEMA, 'generated_at': stamp.isoformat(timespec='seconds'),
           'measurements_sha256': hashlib.sha256(meas_bytes).hexdigest() if meas_bytes else None,
           'distrusted_sources': sorted(distrust),
           'method': __doc__.strip().splitlines()[0],
           'estimate_fits': fits, 'rejected_fits': rejected,
           'duration_fit': dur_fit, 'duration_fit_rejected': dur_rejected,
           'tier_sources': {k: sum(1 for t in tier.values() if t['method'] == k)
                            for k in ('arena_overall', 'other_overall', 'sibling', 'none')},
           'capacity': caps,
           'capacity_not_found': (meas.get('capacity') or {}).get('not_found') or {},
           'capacity_errors': cap_errors, 'source_dates': source_dates(meas),
           'usd_checks': [{**h, 'usd_per_hour': (usd.get(h['model']) or {}).get('value'),
                           'community_over_usd': (round(h['value'] / usd[h['model']]['value'], 2)
                                                    if (usd.get(h['model']) or {}).get('value') else None)}
                          for h in (meas.get('capacity') or {}).get('hourly_checks') or ()],
           'anchor': anchor,
           'links': links, 'effort_correlations': scorer.effort_correlations,
           'effort_readings': scorer.readings,
           'effort_steps': {**scorer.steps, 'time_per_task': scorer.time_steps},
           'size_steps': {**scorer.size_steps, 'time_per_task': scorer.time_size_step},
           'effort_fill_refused': scorer.fill_refused,
           'measured_size_inversions': scorer.measured_inversions,
           'checks': meas.get('checks') or [],
           'models': models, 'data_errors': errors}
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--measurements', default=str(measurements.OUT))
    ap.add_argument('--distrust', action='append', default=[], help='a source id to leave out of every field (repeatable)')
    ap.add_argument('--out', default=str(OUT))
    ap.add_argument('--accept-panel-change', action='store_true',
                    help='write the document although the panel members differ from the file being replaced')
    args = ap.parse_args()
    raw = Path(args.measurements).read_bytes()
    try:
        doc = build(json.loads(raw), sources.accounts(), json.loads(PLANS.read_text(encoding='utf-8')), args.distrust, raw,
                    previous=previous_doc(args.out), accept_panel_change=args.accept_panel_change)
    except PanelChangeRefused as exc:
        sys.stderr.write(f'{exc}\n')
        return 2
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(doc["models"])} models -> {args.out}')
    print(f'{"model":28s} {"usd/h":>7}  ' + ' '.join(f'{a[:10]:>10}' for a in AXES))
    for m, r in doc['models'].items():
        u = r['usd_per_hour']['value']
        mark = {'estimated': '~'}
        cells = ' '.join((f"{r['scores'][a]:>9.2f}" if r['scores'][a] is not None else f"{'-':>9}")
                         + mark.get(r['axes'][a]['status'], '!' if r['axes'][a].get('flag') else ' ') for a in AXES)
        print(f"{m:28s} {u if u is not None else '-':>7}  {cells}")
    print('~ estimated (sibling or tier fit)  ! missing or partial')
    print('tier sources:', doc['tier_sources'])
    for fam, plans in doc['capacity'].items():
        for plan, wins in plans.items():
            print(f'capacity {fam}/{plan}: ' + ', '.join(
                f"{w}={r['capacity_usd'] if r['capacity_usd'] is not None else '-'}({r['status']}, rung {r['rung']})"
                for w, r in wins.items()))


if __name__ == '__main__':
    sys.exit(main())
