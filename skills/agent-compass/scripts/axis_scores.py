"""Four-category (five-axis) scoring: model score data, the account overlay, demand, targets and fit."""

import copy
import json
import os
from pathlib import Path

import account_state
import provider_inventory
import public_data
import user_scores
import account_fleet


# --- Scoring: the four category scores and the quota economy (the only scoring) -----------------------------------------
# Model data comes from references/five-axis-scores.json (calibration/five_axis.py, cited public sources only). The four
# categories (user, 2026-11-11): divergence, coding, aesthetics, tool_operation. Every board is read as a panel standard
# score: z = (x - x_anchor) / sd_panel, the anchor (Opus 5.5) at High is 0 and one unit is one panel standard deviation;
# parallel boards of one facet are linked and averaged, the facets of a category combine by `min`, and a facet a model
# lacks is estimated (same-series sibling, else the fit on the arena.ai Overall tier) and marked estimated.
# - A task demands levels 0..3 on the four categories (the classifier's `axis_demand`, always produced) and nothing
#   else scores or gates on quality: no scalar tier, no route preference or restriction lists, no harness quality
#   factors, no difficulty floors, no free-usage bonus.
# - Targets come from the panel (five-axis-scores.json `anchor.targets`): level 1 the panel's weakest, level 2 its median,
#   level 3 the anchor. tool_operation asks per facet: level 1 nothing, level 2 responsiveness, level 3 responsiveness
#   and image recognition, never less of a facet than the level below. Each level-3 row also carries its `ceiling`, the
#   best measured High value among the catalogued models (owner, 2026-11-13).
# - Fit (five_axis_fit) = sum_a w_a x (min(z_a, c_a) - t1_a) / sum_a w_a over the demanded categories, w_a the demand
#   level, t1_a the level-1 target (the panel's weakest) and c_a the cap: the level's target at levels 1 and 2, the
#   ceiling at level 3. It is in panel standard deviations: 0 is the panel's weakest model on every demanded category,
#   routine levels give no credit above their target, and the hardest level keeps rewarding ability up to the best
#   measured model. A candidate below the panel's weakest scores below 0. The economy reads the same fit, and 0 is
#   its total loss (a candidate at or below it is never an outside alternative).
# - Continuity: continuing a related worker (coupling high, the same bundle) is worth one demand level, the largest
#   positive t3 - t2 among the task's highest-demand categories (else 0); the context rotation cap (worker_worn) stands.
# - Missing data (rule B): a demanded category with no data at all, or no cited usd on a billed account, leaves
#   automatic routing; a named model still runs, with the data error as a warning.
# - Plan capacity is the cited figure of the capacity ladder (community, time-to-exhaust, vendor multiplier), priced at
#   the merged usd scale; a window with an empty ladder is a data error that still prices scarcity from its live
#   percentage and reset time.
# - Economy: every long window charges the candidate's burn (usd over the window's capacity) times the window's quota
#   price (FiveAxisEconomy), counted in hours of the window's reference model.
FIVE_AXIS_FILE = account_fleet.REFERENCES / 'five-axis-scores.json'
USER_SCORES_FILE = account_fleet.REFERENCES / 'user-scores.json'
FIVE_AXES = ('divergence', 'coding', 'aesthetics', 'tool_operation')
FIVE_AXIS_DEFAULTS = {
    # fit and continuity are both in panel standard deviations: one unit each, not a hand-set trade-off
    'weights': {'fit': 1.0, 'continuity': 1.0},
    'demanded_level': 1,         # rule B: a category demanded at this level or above must have data
}
# What each demand level 0..3 means per category: the one text both the schema of the JSON classifiers and the questions of
# the System-One (Jev) classifier are built from (user, 2026-11-11).
FIVE_AXIS_DEMAND_LEVELS = {
    'divergence': ('Divergent work: creative writing, brainstorming, research synthesis and data analysis, and decomposing '
                   'a problem from first principles outside the existing architecture.', (
        'execute a known spec or checklist',
        'small judgment calls or wording choices',
        'several options or hypotheses to generate and compare, or a research or data-analysis question to investigate '
        'and synthesize',
        'open-ended creative writing, open research synthesis across many sources, or a first-principles redesign outside '
        'the existing architecture')),
    'coding': ('Coding ability: reading, writing, reviewing and changing code correctly, doing exactly what was asked and '
               'reporting it honestly.', (
        'no code read or written',
        'a small script or config change, or a result a test verifies',
        'a production module with its tests, or a review or audit of one',
        'changes across coupled modules, concurrency, protocol or core-library work, a whole-system review or audit, or '
        'a defect would be costly or irreversible')),
    'aesthetics': ('Visual and design judgment: GUI and interaction design, layout and typesetting, tables and charts, '
                   '2D or 3D modeling.', (
        'no visual output',
        'plain output where only tidy formatting matters',
        'a user-facing screen, page, chart or document whose look matters',
        'design-led work: a new interface or interaction, a visual identity, a 3D scene or model')),
    'tool_operation': ('Operating tools fast: per-turn speed inside a tool loop, and reading images. Writing or fixing '
                       'software about real-time systems is coding, not a live loop.', (
        'reads files and runs a few shell commands',
        'routine tool or API calls',
        'heavy tool or API orchestration, or the worker itself stays inside a live external system (a game, a ticket '
        'queue, an order book, a live page or chat) where slow turns degrade the outcome',
        'must read images or screenshots inside the loop, or operate a desktop or browser GUI through screenshots')),
}
FIVE_AXIS_DEMAND_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': list(FIVE_AXES),
    'description': ('How much the task needs each capability category of the model, 0..3. Economy is not a task demand: '
                    'the router prices it from cost and live quota.'),
    'properties': {axis: {'type': 'integer', 'minimum': 0, 'maximum': 3,
                          'description': intro + ' ' + '; '.join(f'{i} = {text}' for i, text in enumerate(levels)) + '.'}
                   for axis, (intro, levels) in FIVE_AXIS_DEMAND_LEVELS.items()},
}
# tool_operation's facets per demand level (calibration/five_axis.TOOL_LEVEL_FACETS): level 1 imposes no ability requirement.
TOOL_LEVEL_FACETS = {1: (), 2: ('responsiveness',), 3: ('responsiveness', 'image_recognition')}
IMAGE_INPUT_LEVEL = 3           # tool_operation at this level must read images: a text-only model leaves the task


def five_axis_config(policy):
    raw = (policy or {}).get('five_axis')
    raw = raw if isinstance(raw, dict) else {}
    return {**FIVE_AXIS_DEFAULTS, **raw, 'weights': {**FIVE_AXIS_DEFAULTS['weights'], **(raw.get('weights') or {})}}


def load_five_axis(path=None, *, use_effective=True):
    default_file = account_fleet.REFERENCES / 'five-axis-scores.json'
    target = Path(path) if path is not None else (public_data.effective(default_file) if use_effective else default_file)
    return json.loads(target.read_text(encoding='utf-8'))


def require_level3_ceilings(data):
    """Fail fast on a five-axis-scores.json written before level-3 ceilings (schema agent-compass.five-axis-scores/3): the
    fit reads `ceiling` on every level-3 row that has a target (per facet for tool_operation), so an older file is named
    with what to run instead of failing later with a KeyError."""
    targets = (data.get('anchor') or {}).get('targets') or {}
    missing = []
    for a in FIVE_AXES:
        level3 = ((targets.get(a) or {}).get('levels') or {}).get('3') or {}
        rows = (level3.get('by_facet') or {}).items() if a == 'tool_operation' else [(None, level3)]
        for f, r in rows:
            if r.get('target') is not None and not isinstance((r.get('ceiling') or {}).get('value'), (int, float)):
                missing.append(a if f is None else f'{a}.{f}')
    if missing:
        raise ValueError(
            f"five-axis-scores.json (schema {data.get('schema')!r}) has no level-3 ceiling for {', '.join(missing)}; this "
            'code reads agent-compass.five-axis-scores/3. In an install run `agentcompass data --refresh` (or update '
            'AgentCompass); in a checkout run `python -X utf8 agentcompass data refresh --no-fetch`.')


def load_public_user_scores(path=None, *, use_effective=True):
    default_file = account_fleet.REFERENCES / 'user-scores.json'
    target = Path(path) if path is not None else (public_data.effective(default_file) if use_effective else default_file)
    return user_scores.load_public(target, effective=False)


def validate_axis_demand(raw):
    if not isinstance(raw, dict) or set(raw) != set(FIVE_AXES):
        raise ValueError('task.profile.axis_demand must give exactly ' + ', '.join(FIVE_AXES))
    out = {}
    for axis in FIVE_AXES:
        value = raw[axis]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
            raise ValueError(f'task.profile.axis_demand.{axis} must be an integer from 0 to 3')
        out[axis] = value
    return out


def opencode_list_prices(models_table):
    """{name or alias: (table name, cited list price row)} for models whose public data carries an input and output list
    price in USD per million tokens (`five_axis.list_price`, from the data's `price`: AA first, OpenRouter second)."""
    prices = {}
    for name, info in (models_table or {}).items():
        price = ((info or {}).get('five_axis') or {}).get('list_price') if isinstance(info, dict) else None
        value = (price or {}).get('value') if isinstance(price, dict) else None
        if isinstance(value, dict) and all(isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool)
                                           for k in ('input', 'output')):
            for key in (name, *(info.get('aliases') or ())):
                prices[str(key)] = (name, price)
    return prices


def opencode_token_usd(price, tok_in, tok_out, tok_cache_read, tok_cache_write):
    """List-price dollars of one model's tokens: input and cache writes at the input price, cache reads at the cited
    cache-read price (0 when the data has none: a lower bound), output (with reasoning) at the output price."""
    value = price['value']
    return (float(tok_in or 0) * value['input'] + float(tok_cache_write or 0) * value['input']
            + float(tok_cache_read or 0) * float(value.get('cache_read') or 0.0)
            + float(tok_out or 0) * value['output']) / 1e6


def opencode_local_spend(models_table, discovery=None, *, read_local_db=True):
    """(spend, unpriced): lower-bound USD per model from local `opencode.db` token counts at the model's public list
    price, and the sorted ids of models with tokens but no list price in the data (round 12 S6). A model id matches a
    table name or alias exactly; there is no prefix guessing and no invented price, so a model without a cited price is
    a surfaced gap (mode B), never a $0 or a borrowed rate. When `read_local_db=False` (fresh/public evaluation),
    no machine-local `opencode.db` is read."""
    import sqlite3
    prices = opencode_list_prices(models_table)
    readings = []       # (source, model id, input, output + reasoning, cache read, cache write)
    disc_models = (((discovery or {}).get('accounts') or {}).get('opencode') or {}).get('usage', {}).get('models') or {}
    for model_id, m_row in disc_models.items():
        if isinstance(m_row, dict):
            readings.append(('discovery', model_id, m_row.get('input'),
                             float(m_row.get('output') or 0) + float(m_row.get('reasoning') or 0),
                             m_row.get('cache_read'), m_row.get('cache_write')))
    db_path = (Path(os.environ.get('OPENCODE_DB_PATH')
                    or (Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db'))
               if read_local_db else None)
    if db_path is not None and db_path.exists():
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
            try:
                rows = conn.execute(
                    "SELECT json_extract(data, '$.modelID'), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.input'), 0)), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.output'), 0) + COALESCE(json_extract(data, '$.tokens.reasoning'), 0)), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.cache.read'), 0)), "
                    "SUM(COALESCE(json_extract(data, '$.tokens.cache.write'), 0)) "
                    "FROM message WHERE json_extract(data, '$.role') = 'assistant' "
                    "GROUP BY json_extract(data, '$.modelID')"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            rows = []
        readings += [('db', *row) for row in rows if row[0]]
    by_source = {'discovery': {}, 'db': {}}
    unpriced = set()
    for source, model_id, tok_in, tok_out, tok_cr, tok_cw in readings:
        if float(tok_in or 0) + float(tok_out or 0) + float(tok_cr or 0) + float(tok_cw or 0) <= 0:
            continue
        hit = prices.get(str(model_id))
        if hit is None:
            unpriced.add(str(model_id))
            continue
        name, price = hit
        by_source[source][name] = by_source[source].get(name, 0.0) + opencode_token_usd(price, tok_in, tok_out, tok_cr, tok_cw)
    spend = {}
    for name in set(by_source['discovery']) | set(by_source['db']):     # the two reads can overlap: keep the larger
        usd = max(by_source['discovery'].get(name, 0.0), by_source['db'].get(name, 0.0))
        if usd > 0:
            spend[name] = round(usd, 4)
    return spend, sorted(unpriced)


def _capacity_rules(accounts):
    """Map `(target_family, target_window) -> rule` for valid `same_model[row].capacity_rule` declarations."""
    rules = {}
    for model_key, entry in sorted((accounts.get('same_model') or {}).items()):
        if not isinstance(entry, dict) or not public_data._valid_public_declaration(entry):
            continue
        rule = entry.get('capacity_rule')
        if public_data._valid_capacity_rule(rule):
            rules[(rule['target_family'].strip(), rule['target_window'].strip())] = {**rule, 'model': model_key}
    return rules


def _plan_unknown_text(src_spec, raw_plan):
    if isinstance(src_spec, dict) and src_spec.get('plan_unknown'):
        return str(src_spec['plan_unknown'])
    raw_str = str(raw_plan or '').strip()
    if account_state.plan_without_vendor(raw_str) in ('', 'unknown', 'subscription'):
        return 'subscription tier not reported'
    return f'unrecognized tier {raw_str!r}'


def _resolve_rule_capacity(accounts, community, cap_family, raw_plan, plan, src_spec, rule, shipped=None):
    """Apply a `same_model` monthly-price capacity rule at runtime from `plan-templates.json` and `community` ladder."""
    table = provider_inventory.plan_table(accounts, cap_family, shipped=shipped)
    target_tpl = provider_inventory.plan_defaults(accounts, cap_family, plan, shipped=shipped) if plan and plan in table else None
    if target_tpl is None:
        reason = _plan_unknown_text(src_spec, raw_plan)
        return None, {'status': 'plan_unknown', 'age_days': None, 'plan': None, 'reason': reason}, reason
    target_price = target_tpl.get('price_usd_per_month')
    target_psrc = target_tpl.get('price_source')
    if isinstance(target_price, bool) or not isinstance(target_price, (int, float)) or target_price <= 0:
        reason = f'plan {plan!r} has no paid monthly price (${target_price or 0:g}/mo)'
        return None, {'status': 'unpriced_plan', 'age_days': None, 'plan': plan, 'reason': reason}, reason
    if not public_data._valid_vendor_price_source(target_psrc):
        reason = f'plan {plan!r} has no cited vendor price_source'
        return None, {'status': 'uncited_plan_price', 'age_days': None, 'plan': plan, 'reason': reason}, reason
    ref_family = rule['reference_family'].strip()
    ref_window = rule['reference_window'].strip()
    candidates = []
    for ref_plan, raw_ref in sorted(provider_inventory.plan_table(accounts, ref_family, shipped=shipped).items()):
        if not isinstance(raw_ref, dict) or 'alias' in raw_ref:
            continue
        ref_tpl = provider_inventory.plan_defaults(accounts, ref_family, ref_plan, shipped=shipped)
        if not isinstance(ref_tpl, dict):
            continue
        ref_price = ref_tpl.get('price_usd_per_month')
        ref_psrc = ref_tpl.get('price_source')
        ref_row = ((community.get(ref_family) or {}).get(ref_plan) or {}).get(ref_window) or {}
        ref_cap = ref_row.get('capacity_usd')
        if (isinstance(ref_price, bool) or not isinstance(ref_price, (int, float)) or ref_price <= 0
                or not public_data._valid_vendor_price_source(ref_psrc)
                or not isinstance(ref_cap, (int, float)) or ref_cap <= 0):
            continue
        candidates.append({
            'plan': ref_plan,
            'price': float(ref_price),
            'url': str(ref_psrc['url']).strip(),
            'cap': float(ref_cap),
            'row': ref_row,
        })
    if not candidates:
        return None, {'status': 'missing', 'age_days': None, 'plan': plan}, None
    target_p = float(target_price)
    exact_matches = [c for c in candidates if abs(c['price'] - target_p) < 1e-9]
    if exact_matches:
        chosen = min(exact_matches, key=lambda c: c['plan'])
        exact = True
        ratio = 1.0
    else:
        chosen = min(candidates, key=lambda c: (abs(c['price'] - target_p), c['price'], c['plan']))
        exact = False
        ratio = target_p / chosen['price']
    scaled = round(ratio * chosen['cap'], 2)
    target_url = str(target_psrc['url']).strip()
    ref_url = chosen['url']
    if exact:
        evidence = (
            f"same-price {ref_family} subscription: {ref_family}/{chosen['plan']} ({ref_window}, "
            f"${chosen['price']:g}/mo [{ref_url}]) matches {cap_family}/{plan} "
            f"(${target_p:g}/mo [{target_url}]) at 1x -> ${scaled:g}"
        )
    else:
        evidence = (
            f"no {ref_family} subscription at ${target_p:g}/mo; nearest plan by price is "
            f"{ref_family}/{chosen['plan']} (${chosen['price']:g}/mo [{ref_url}]), scaled by price ratio "
            f"{ratio:g}x (${target_p:g}/mo {cap_family}/{plan} [{target_url}] / "
            f"${chosen['price']:g}/mo {ref_family}/{chosen['plan']}) -> ${scaled:g}"
        )
    meta = {
        'status': chosen['row'].get('status', 'ok'),
        'age_days': chosen['row'].get('age_days'),
        'plan': plan,
        'detected_tier': raw_plan,
        'plan_price_usd_per_month': target_p,
        'plan_price_url': target_url,
        'match': 'exact_price' if exact else 'nearest_price_scaled',
        'reference_family': ref_family,
        'reference_plan': chosen['plan'],
        'reference_window': ref_window,
        'reference_price_usd_per_month': chosen['price'],
        'reference_price_url': ref_url,
        'price_ratio': round(ratio, 6),
        'capacity_usd': scaled,
        'rule': {k: rule[k] for k in ('kind', 'as_of', 'statement', 'model') if rule.get(k)},
        'derived_from': chosen['row'].get('sources') or [],
        'evidence': evidence,
    }
    return scaled, meta, None


def five_axis_overlay(accounts, data=None, *, read_local_state=True, public_scores=None, local_scores=None):
    """A copy of `accounts` carrying the scoring data of references/five-axis-scores.json plus manual scores
    (`references/user-scores.json` maintainer layer, then local `user-scores.json` when `read_local_state=True`).

    Each model row gets `five_axis` {categories (the scores file's category cells: value, by_effort, facets), scores,
    missing, estimates, axis_status, tier, usd_status, image_input, fast_mode, duration: {status, by_effort,
    basis_by_effort, efforts_without_time}}: the time a task takes per effort as a multiple of
    the anchor's Artificial Analysis Time/Task at High, each with its cited basis (`duration_at`); usd_per_hour becomes the
    merged cited value (an unsourced usd is removed, not defaulted). Each windowed account's capacity is the
    data's community real-usage figure for its plan and window (round 4), replacing the packaged figure and any dated
    change; a window without one is marked unknown, with no template fallback. Free pools are ordinary quota pools with
    C/U/t0/T (round 11): when capacity is unreadable, they enter mode B (`capacity_unknown`), out of automatic routing."""
    explicit_data = data is not None
    cfg_path = five_axis_config(accounts.get('scoring')).get('path')
    data = data if explicit_data else (load_five_axis(cfg_path) if read_local_state
                                       else load_five_axis(cfg_path, use_effective=False))
    out = copy.deepcopy(accounts)
    rows = data.get('models') or {}
    for name, info in (out.get('models') or {}).items():
        if not isinstance(info, dict):
            continue
        row = rows.get(name)
        usd = ((row or {}).get('usd_per_hour') or {}).get('value')
        img = (row or {}).get('image_input') or {'status': 'missing'}
        fast = (row or {}).get('fast_mode') or {'status': 'not_offered'}
        dur_row = (row or {}).get('duration_multiplier') or {}
        price = (row or {}).get('price')       # public list price, USD per million tokens (aa.price, else openrouter.price)
        cats = (row or {}).get('axes') or {}
        info['five_axis'] = {'categories': {a: cats[a] for a in FIVE_AXES if isinstance(cats.get(a), dict)},
                             'scores': {a: (cats.get(a) or {}).get('value') for a in FIVE_AXES},
                             'missing': list(FIVE_AXES) if row is None else list(row.get('missing_axes') or ()),
                             'estimates': {} if row is None else estimate_summary(row),
                             'axis_status': {a: (cats.get(a) or {}).get('status', 'missing') for a in FIVE_AXES},
                             'tier': (row or {}).get('tier') or {'value': None, 'method': 'none'},
                             'usd_status': 'ok' if usd is not None else 'missing',
                             'image_input': img,
                             'fast_mode': fast,
                             'duration': {k: dur_row[k] for k in ('status', 'by_effort', 'basis_by_effort',
                                                                  'efforts_without_time') if k in dur_row},
                             **({'list_price': price} if isinstance(price, dict) and price.get('value') else {})}
        if usd is not None:
            info['usd_per_hour'] = usd
        else:
            info.pop('usd_per_hour', None)
    local_spend, local_unpriced = opencode_local_spend(
        out.get('models') or {}, out.get('_discovery'), read_local_db=bool(read_local_state))
    community = data.get('capacity') or {}
    cap_rules = _capacity_rules(out)
    rule_families = {fam for fam, _ in cap_rules}
    shipped_templates = provider_inventory._shipped_plan_templates()
    families_by_account = {}
    for name, spec in (out.get('accounts') or {}).items():
        if not isinstance(spec, dict):
            continue
        family = str(provider_inventory.account_family(name, out, out.get('_discovery') or {}) or account_fleet.account_family(spec) or '')
        family = family.removesuffix('-resilient')
        families_by_account[name] = family
    for name, spec in (out.get('accounts') or {}).items():
        if not isinstance(spec, dict):
            continue
        spec.pop('observed', None)          # never feed 3h/24h historical rates into five-axis economics
        # A free pool is OpenCode's free starter allowance, declared by the account's own `free_allowance` field (or a
        # free plan), never by its name. `unmetered` alone does not make one: Dot and ChatGPT Web are purpose-limited
        # channels that usually cannot be used up, and stay in automatic routing (owner, 2026-11-10).
        is_free_pool = bool(spec.get('free_allowance') or str(spec.get('plan') or '') == 'free')
        family = families_by_account[name]
        if is_free_pool and spec.get('billing') != 'metered':
            # Round 11 (user 2026-11-10): "free" models are an ordinary quota pool (same C/U/t0/T; a starter allowance
            # has T = inf so recovery line is 0 and X = S). Remove unmetered / unlimited / $0 spend on the five-axis path.
            spec.pop('unmetered', None)
            spec.pop('unlimited', None)
            spec['five_axis_free_pool'] = True
            if not spec.get('windows'):
                spec['windows'] = {'allowance': {'hours': None}}
                spec['capacity_usd'] = {}
                spec['capacity_unknown'] = True
                spec['capacity_source'] = {'allowance': {'status': 'missing', 'plan': spec.get('plan') or 'free'}}
                # Auto bundles were derived while this account declared no window, so they carry an explicit empty `draws`
                # (authoritative): every model of a free pool spends the one allowance, so they draw it (round 15).
                for b in (spec.get('bundles') or {}).values():
                    if isinstance(b, dict) and b.get('draws') == []:
                        b['draws'] = ['allowance']
            acct_spend = 0.0
            seen_models = set()
            for b in (spec.get('bundles') or {}).values():
                if not isinstance(b, dict):
                    continue
                m_key = account_fleet.canonical(b.get('model') or '', out)
                if m_key in local_spend and m_key not in seen_models:
                    seen_models.add(m_key)
                    acct_spend += local_spend[m_key]
            if acct_spend == 0.0 and family == 'opencode':      # opencode.db is this family's own usage record
                acct_spend = sum(local_spend.values())
            if acct_spend > 0:
                spec['local_spend_usd'] = round(acct_spend, 4)
                spec['local_spend_source'] = 'opencode.db token counts x public list price (lower bound)'
            if family == 'opencode' and local_unpriced:
                # no cited list price for these models: their spend is a surfaced gap, not $0 and not a borrowed rate
                spec['local_spend_gap'] = list(local_unpriced)
        cap_family = family
        raw_plan = spec.get('plan')
        src_spec = spec
        qsrc = provider_inventory.quota_source(name, out)
        if qsrc != name and isinstance((out.get('accounts') or {}).get(qsrc), dict):
            src_spec = out['accounts'][qsrc]
            cap_family = families_by_account.get(qsrc) or cap_family
            raw_plan = src_spec.get('plan')
        if not spec.get('windows') or spec.get('unmetered'):
            if not spec.get('unmetered') and not spec.get('unlimited') and cap_family in rule_families:
                probe_plan = provider_inventory.configured_plan(cap_family, raw_plan, accounts=out, shipped=shipped_templates)
                probe_table = provider_inventory.plan_table(out, cap_family, shipped=shipped_templates)
                probe_tpl = provider_inventory.plan_defaults(out, cap_family, probe_plan, shipped=shipped_templates) if probe_plan and probe_plan in probe_table else None
                probe_price = (probe_tpl or {}).get('price_usd_per_month')
                if probe_tpl is None or isinstance(probe_price, bool) or not isinstance(probe_price, (int, float)) or probe_price <= 0:
                    spec['capacity_unknown'] = True
                    spec['plan_unknown_reason'] = (
                        _plan_unknown_text(src_spec, raw_plan) if probe_tpl is None
                        else f'plan {probe_plan!r} has no paid monthly price (${probe_price or 0:g}/mo)'
                    )
                else:
                    spec.pop('capacity_unknown', None)
                    spec.pop('plan_unknown_reason', None)
            continue
        plan = provider_inventory.configured_plan(cap_family, raw_plan, accounts=out, shipped=shipped_templates)
        if cap_family == 'pi':      # Pi signs in with ChatGPT and spends the Codex account's allowance: its plan's windows
            cap_family, plan = 'codex', next((provider_inventory.configured_plan('codex', s.get('plan'), accounts=out, shipped=shipped_templates)
                                              for n, s in (out.get('accounts') or {}).items() if isinstance(s, dict)
                                              and families_by_account.get(n) == 'codex'), plan)
        rows_ = (community.get(cap_family) or {}).get(plan) or {}
        explicit_caps = spec.get('capacity_usd') if isinstance(spec.get('capacity_usd'), dict) else {}
        caps = {wid: float(rows_[wid]['capacity_usd']) for wid in spec['windows']
                if isinstance((rows_.get(wid) or {}).get('capacity_usd'), (int, float))}
        cap_sources = {wid: {'status': (rows_.get(wid) or {}).get('status', 'missing'),
                             'age_days': (rows_.get(wid) or {}).get('age_days'), 'plan': plan or None}
                       for wid in spec['windows']}
        plan_unknown_reason = None
        for wid in spec['windows']:
            if wid in caps:
                continue
            rule = cap_rules.get((cap_family, wid))
            if rule is not None:
                scaled_val, rule_meta, err_reason = _resolve_rule_capacity(
                    out, community, cap_family, raw_plan, plan, src_spec, rule, shipped=shipped_templates)
                cap_sources[wid] = rule_meta
                if scaled_val is not None:
                    caps[wid] = scaled_val
                elif err_reason and not plan_unknown_reason:
                    plan_unknown_reason = err_reason
        # A free allowance or a metered account (a prepaid $200 proxy) states its own capacity: community data measures
        # subscription plans only. A subscription's packaged figure is not a declaration and stays unknown (round 11).
        if not caps and explicit_caps and (is_free_pool or spec.get('billing') == 'metered'):
            caps = {wid: float(v) for wid, v in explicit_caps.items()
                    if wid in spec['windows'] and isinstance(v, (int, float)) and v > 0}
            for wid in caps:
                cap_sources[wid] = {'status': 'explicit', 'age_days': None, 'plan': plan or None}
        spec['capacity_usd'] = caps
        unknown = [wid for wid in spec['windows'] if wid not in caps]
        if (unknown and not plan_unknown_reason and not is_free_pool
                and spec.get('billing') != 'metered' and not src_spec.get('plan_priced_as')):
            table = provider_inventory.plan_table(out, cap_family, shipped=shipped_templates)
            known_tpl = provider_inventory.plan_defaults(out, cap_family, plan, shipped=shipped_templates) if plan and plan in table else None
            if known_tpl is None and table:
                plan_unknown_reason = _plan_unknown_text(src_spec, raw_plan)
                for wid in unknown:
                    if cap_sources.get(wid, {}).get('status') == 'missing':
                        cap_sources[wid] = {'status': 'plan_unknown', 'age_days': None, 'plan': None, 'reason': plan_unknown_reason}
        spec['capacity_source'] = cap_sources
        if unknown:
            spec['capacity_unknown'] = True
            if plan_unknown_reason:
                spec['plan_unknown_reason'] = plan_unknown_reason
            else:
                spec.pop('plan_unknown_reason', None)
        else:
            spec.pop('capacity_unknown', None)
            spec.pop('plan_unknown_reason', None)
    anchor = data.get('anchor') or {}
    targets = anchor.get('targets') or {}
    if not anchor.get('model') or set(targets) != set(FIVE_AXES):
        raise ValueError('five-axis-scores.json names no anchor model or no panel targets for the four categories: the fit '
                         'has nothing to stand on')
    require_level3_ceilings(data)
    out['_five_axis'] = {'generated_at': data.get('generated_at'), 'anchor': anchor['model'], 'targets': targets,
                         'panel_n': anchor.get('panel_n')}
    pub_manual = public_scores if public_scores is not None else (
        None if explicit_data else (load_public_user_scores() if read_local_state
                                    else load_public_user_scores(use_effective=False))
    )
    loc_manual = local_scores if local_scores is not None else (user_scores.load_local() if read_local_state else None)
    user_scores.apply_overlay(
        out.setdefault('models', {}), targets,
        public_scores=pub_manual, local_scores=loc_manual, aliases=out.get('aliases'),
    )
    for info in (out.get('models') or {}).values():
        if isinstance(info, dict) and isinstance(info.get('five_axis'), dict):
            info['five_axis']['shares'] = five_axis_shares(five_axis_values(info['five_axis'], 'high'), targets)
    return out


def five_axis_demand(task):
    """{category: level 0..3}: the classifier's `axis_demand` as it stands (validated with the profile). An all-zero answer
    is a task that asks nothing of the categories: every model fits it alike and the economy decides."""
    return dict(task['profile']['axis_demand'])


def estimate_summary(row):
    """{'category.facet': provenance} for the facets of a five-axis-scores row that were estimated (a same-series sibling
    or the fit on the tier) rather than measured."""
    return {f'{a}.{f}': dict(comp.get('estimate') or {})
            for a, ax in (row.get('axes') or {}).items() for f, comp in (ax.get('facets') or {}).items()
            if comp.get('status') == 'estimated'}


def _at_effort(cell, effort):
    """A score cell's value at `effort`: its per-effort entry in `by_effort` when `by_effort` is present (an effort-less
    bundle runs its single configuration, keyed `'high'` or its lone entry), else `cell['value']` for minimal cells."""
    if not isinstance(cell, dict):
        return None
    if 'by_effort' in cell and isinstance(cell.get('by_effort'), dict):
        by = cell['by_effort']
        if not by:
            return None
        value = by.get(effort) if effort else (by.get('high') if 'high' in by else cell.get('value'))
        return None if value is None else float(value)
    value = cell.get('value')
    return None if value is None else float(value)


def five_axis_values(state, effort):
    """{category: z}, with tool_operation as {facet: z}: this model's panel standard scores at `effort` (None where it has
    no value). The thinking categories carry the model's own additive effort shift (five_axis.py); responsiveness is
    measured per effort."""
    cats = (state or {}).get('categories') or {}
    out = {a: _at_effort(cats.get(a), effort) for a in FIVE_AXES if a != 'tool_operation'}
    facets = ((cats.get('tool_operation') or {}).get('facets')) or {}
    out['tool_operation'] = {f: _at_effort(facets.get(f), effort) for f in TOOL_LEVEL_FACETS[3]}
    return out


def five_axis_targets(demand, targets):
    """{category: the demanded level's target row} from five-axis-scores.json `anchor.targets`, for every demanded category
    that asks for something (tool_operation level 1 asks for nothing). `cap` is where the fit stops counting: the target
    itself at levels 1 and 2, the level-3 row's `ceiling` (the best measured model) at level 3."""
    def row_of(r, w):
        return {'target': r['target'], 'zero': r['zero'], 'cap': r['ceiling']['value'] if w == 3 else r['target']}
    out = {}
    for a, w in demand.items():
        if w <= 0:
            continue
        row = ((targets.get(a) or {}).get('levels') or {}).get(str(w)) or {}
        if a == 'tool_operation':
            if row.get('by_facet'):
                out[a] = {'by_facet': {f: row_of(r, w) for f, r in row['by_facet'].items()}}
        elif row.get('target') is not None:
            out[a] = row_of(row, w)
    return out


def five_axis_fit(values, demand, targets):
    """(fit, {category: row}): sum_a w_a x (min(z_a, c_a) - t1_a) / sum_a w_a over the demanded categories that ask for
    something, in panel standard deviations (module comment above); c_a is the row's `cap` (five_axis_targets): the
    target at levels 1 and 2, the category's ceiling at level 3. tool_operation reads min over its level's facets of
    (min(z_f, c_f) - t1_f). The shortfall is still measured to the target. A category or facet without a value stands at
    its zero (the panel's weakest): rule B keeps such a model out of automatic routing, so this only prices a named
    dispatch."""
    rows, value, total = {}, 0.0, 0
    for a, w in demand.items():
        tgt = targets.get(a)
        if w <= 0 or not tgt:
            continue
        if 'by_facet' in tgt:
            parts = {}
            for f, t in tgt['by_facet'].items():
                z = (values.get(a) or {}).get(f)
                parts[f] = {'score': None if z is None else round(z, 4), 'target': t['target'], 'zero': t['zero'],
                            'cap': t['cap'], 'term': round((min(z, t['cap']) - t['zero']) if z is not None else 0.0, 4)}
            scored = list(parts.values())
            term = min(p['term'] for p in scored)
            rows[a] = {'demand': w, 'facets': parts, 'term': term,
                       'shortfall': round(max(max(0.0, p['target'] - p['score']) if p['score'] is not None
                                              else p['target'] - p['zero'] for p in scored), 4)}
        else:
            z = values.get(a)
            term = round((min(z, tgt['cap']) - tgt['zero']) if z is not None else 0.0, 4)
            rows[a] = {'demand': w, 'score': None if z is None else round(z, 4), 'target': tgt['target'],
                       'zero': tgt['zero'], 'cap': tgt['cap'], 'term': term,
                       'shortfall': round(max(0.0, tgt['target'] - z) if z is not None else tgt['target'] - tgt['zero'], 4)}
        value += w * term
        total += w
    return (value / total if total else 0.0), rows


def five_axis_headroom(rows, targets):
    """{category: {'above_anchor', 'ceiling', 'model'}} for the categories `rows` (five_axis_fit) demand at level 3: how far
    the candidate's score lies above t3 (the anchor; 0 at or below it) and the level-3 cap the fit reads there, the
    category's ceiling, with the model that sets it. tool_operation reports the facet that decides its term. Display only
    (the decision radar's hover): the radar spoke itself stays five_axis_shares."""
    out = {}
    for a, r in rows.items():
        if r['demand'] != 3:
            continue
        level3 = ((targets.get(a) or {}).get('levels') or {}).get('3') or {}
        if 'facets' in r:
            f, part = min(r['facets'].items(), key=lambda kv: kv[1]['term'])
            r, ceiling = part, level3['by_facet'][f]['ceiling']
        else:
            ceiling = level3['ceiling']
        z = r['score']
        out[a] = {'above_anchor': None if z is None else round(max(0.0, z - r['target']), 4),
                  'ceiling': ceiling['value'], 'model': ceiling['model']}
    return out


def five_axis_shares(values, targets):
    """{category: 0..1} for display (the tray radar): (min(z, t3) - t1) / (t3 - t1), where t1 is the level-1 target (the
    panel's weakest) and t3 the level-3 target (the anchor): 0 at the panel's weakest, 1 at the anchor, never more; a
    level-3 demand's credit above the anchor up to the ceiling is shown in the decision hover (five_axis_headroom).
    tool_operation takes the weakest of its level-3 facets when all required level-3 facets have a value; None where any
    required facet has no value."""
    def share(z, row):
        if z is None or not row or row.get('target') is None or row['target'] <= row['zero']:
            return None
        return round(max(0.0, (min(z, row['target']) - row['zero']) / (row['target'] - row['zero'])), 4)
    out = {}
    for a in FIVE_AXES:
        level3 = ((targets.get(a) or {}).get('levels') or {}).get('3') or {}
        if a == 'tool_operation':
            parts = [share((values.get(a) or {}).get(f), r) for f, r in (level3.get('by_facet') or {}).items()]
            out[a] = min(parts) if parts and all(p is not None for p in parts) else None
        else:
            out[a] = share(values.get(a), level3)
    return out


def five_axis_level(rows):
    """The demand-weighted mean of the candidate's scores over the demanded categories (tool_operation: the weakest of its
    level's facets), uncapped: the last tie-break between equal net scores. A missing value stands at its zero."""
    total, value = 0, 0.0
    for r in rows.values():
        if 'facets' in r:
            z = min(p['score'] if p['score'] is not None else p['zero'] for p in r['facets'].values())
        else:
            z = r['score'] if r['score'] is not None else r['zero']
        value += r['demand'] * z
        total += r['demand']
    return value / total if total else 0.0


def five_axis_continuity_value(demand, targets):
    """The worth of continuing a related worker, in panel standard deviations (user, 2026-11-11): one demand level, the
    largest positive t3 - t2 among all demanded categories (five-axis-scores.json `anchor.targets.<category>.continuity`),
    else 0. A zero-gap category such as `tool_operation` never wipes out a demanded thinking category's continuity."""
    gaps = [(targets.get(a) or {}).get('continuity') for a, w in demand.items() if w >= 1]
    return max([g for g in gaps if isinstance(g, (int, float)) and g > 0], default=0.0)
