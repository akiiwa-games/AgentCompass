"""The task profile: 11 atomic work kinds, deterministic 4D capability envelope projection, validation, and normalization."""

import math

import account_fleet
import axis_scores
import worker_sessions
from model_resolution import DIRECT_SOURCES, check_model_pin, split_model_effort


WORK_KINDS = (
    'creative_ideation',
    'architecture_reasoning',
    'code_implementation',
    'frontend_ui',
    'code_audit',
    'exploration_forensics',
    'web_research',
    'visual_analysis',
    'text_processing',
    'live_loop',
    'gui_control',
)
PROFILE_KINDS = WORK_KINDS + ('other_or_mixed',)
SECONDARY_KINDS = ('none',) + WORK_KINDS
GENERIC_CALLER_KINDS = (None, 'code_implementation', 'other_or_mixed')

# Role codes per (creativity, coding, aesthetics, tool_operation) for depth s in {1, 2, 3}:
#   '0'  -> 0           (orthogonal: never introduces cross-axis noise)
#   'B'  -> I[s >= 2]   (baseline t1 cap at s >= 2: filters broken models, zero premium above t1)
#   'S'  -> s - 1       (supporting axis: caps at panel median t2 at s = 3, never opening L3 ceiling)
#   'P'  -> s           (primary bottleneck axis: 1:1 with depth, unlocking L3 ceiling at s = 3)
#   'F2' -> 2           (real-time text/API loop responsiveness floor on tool_operation)
#   'F3' -> 3           (real-time visual GUI control floor on tool_operation: responsiveness + image_recognition)
ROLE_FN = {
    '0': lambda s: 0,
    'B': lambda s: 1 if s >= 2 else 0,
    'S': lambda s: s - 1,
    'P': lambda s: s,
    'F2': lambda s: 2,
    'F3': lambda s: 3,
}
# The role each work kind gives each category: a maintainer declaration (2026-11-17), documented in
# references/work-kinds.md. A design rule, not a measurement.
WORK_KIND_SIGNATURES = {
    'creative_ideation': ('P', '0', 'B', '0'),
    'architecture_reasoning': ('S', 'P', '0', '0'),
    'code_implementation': ('B', 'P', '0', 'B'),
    'frontend_ui': ('B', 'P', 'P', 'B'),
    'code_audit': ('B', 'P', '0', '0'),
    'exploration_forensics': ('S', 'P', '0', 'B'),
    'web_research': ('S', 'B', '0', 'B'),
    'visual_analysis': ('B', 'B', 'P', '0'),
    'text_processing': ('B', '0', 'B', '0'),
    'live_loop': ('0', 'S', '0', 'F2'),
    'gui_control': ('0', 'B', 'S', 'F3'),
}

PROFILE_INTEGERS = {'primary_level': (1, 3), 'secondary_level': (0, 3)}
REQUIRED_BOOL_FLAGS = ('local_only', 'needs_visual', 'wants_fast')
ACCESS_LEVELS = ('read-only', 'write')
DIFFICULTY_ORDER = ('routine', 'hard', 'coupled', 'frontier')
HOURS_RANGE = (0.05, 24.0)
TASK_COUNTS = ('1', '2', '3', '>3')

WORK_KIND_QUESTION = (
    'Primary observable activity of the assignment (choose the core deliverable; ignore role/receipt boilerplate): '
    'creative_ideation = cross-domain association, de-novo concept/mechanism invention, creative writing, dialogue, open brainstorming; '
    'architecture_reasoning = first-principles system architecture, mathematical/formal proof, rigorous trade-off critique or planning without editing code; '
    'code_implementation = write, fix, refactor, or test backend, CLI, library, algorithm, data/experiment script, or config code; '
    'frontend_ui = implement or restyle user-facing UI, web/desktop views, CSS, animations, charts, or interactive components; '
    'code_audit = independent read-only code, PR, diff, branch, security, or release review/audit (even when saving a report file); '
    'exploration_forensics = read-only codebase mapping, log/database/metric forensics, or runtime root-cause diagnosis without modifying code; '
    'web_research = open-web, literature, vendor, documentation, or multi-source public fact gathering and synthesis; '
    'visual_analysis = static offline inspection of images, photos, screenshots, mockups, OCR, UI layout diffs, or fine-grained visual styling/material classification; '
    'text_processing = bounded rule-based text cleaning, translation, formatting, mechanical extraction, or summarization; '
    'live_loop = low-latency observe-act loop attached to a live external text/API stream (live market, ticket polling, live chat); '
    'gui_control = operate a live desktop, browser, or mobile GUI via screenshots, mouse clicks, and keyboard actions.'
)
PRIMARY_LEVEL_QUESTION = (
    'Depth of the primary work_kind (1..3), based on task complexity rather than prompt length: '
    '1 = routine/localized (small script or config tweak, checklist/runbook, coarse OCR, simple lookup or formatting); '
    '2 = standard module/analysis (implement, fix, or audit a production module with tests, standard UI page/chart or mockup diff, structured research synthesis); '
    '3 = frontier/coupled/fine-grained (cross-module concurrency/protocol architecture, de-novo cross-domain invention, design-led UI system, or fine-grained outfit/material/lighting visual classification).'
)
SECONDARY_KIND_QUESTION = (
    'Optional second distinct work_kind ONLY when a single worker must execute a genuinely composite task spanning two atomic domains '
    '(e.g. code_implementation + creative_ideation for inventing a novel mechanism and coding it, frontend_ui + gui_control for coding UI and clicking through a live browser); '
    'use "none" whenever the task belongs to one primary domain (including normal test running, git/CLI usage, or reading files before coding).'
)
SECONDARY_LEVEL_QUESTION = (
    'Depth of secondary_kind: MUST be 0 when secondary_kind is "none"; otherwise 1..3 rating only the secondary part\'s own depth.'
)
ACCESS_QUESTION = (
    'Whether this brief only reads, inspects, reviews, diagnoses, or returns output in its reply without modifying workspace files or state '
    '("read-only"), or creates, edits, or deletes workspace files or runs mutating commands ("write"). '
    'code_audit is ALWAYS "read-only" (even when saving a review report file); local repository code_implementation and frontend_ui are ALWAYS "write".'
)
LOCAL_ONLY_QUESTION = (
    'True when the task explicitly depends on existing un-inlined files, directories, repositories, processes, or GUI on this machine. '
    'False when all required inputs are inlined in the prompt or fetched from the public web—even if the brief has a workspace header or names a local output path.'
)
NEEDS_VISUAL_QUESTION = (
    'True when the worker must inspect images, photos, screenshots, mockups, or operate a GUI (always true for visual_analysis and gui_control); '
    'false for pure text/code work.'
)
WANTS_FAST_QUESTION = (
    'True ONLY when the assignment affirmatively requests fast mode, priority service tier, urgent/ASAP turnaround, '
    'or a pre-reset quota sprint (e.g. "pre-reset sprint", "before quota resets", "重置前冲刺") for the task itself, '
    'and does not negate or opt out of fast mode (e.g. "do not use fast mode", "no fast mode", or "not urgent" -> '
    'false). False for action adverbs inside runtime steps (e.g. "click Save immediately when the button appears"), '
    'ordinary mentions of quota/reset without requesting fast execution, or ordinary tasks without an explicit speed/fast request.'
)
TASK_COUNT_QUESTION = (
    'How many independent parallel tasks the brief bundles ("1" when it is a single task, including multi-step sequential workflows '
    'like investigate-fix-test, implement+tests, or review+report; "2", "3", or ">3" ONLY when different workers could produce '
    'separate deliverables in parallel without waiting on one another).'
)

# Integers carry minimum/maximum, never an integer enum: Gemini's protobuf schema rejects integer enums ("enum[0] cannot be empty").
TASK_PROFILE_SCHEMA = {
    'type': 'object',
    'properties': {
        'work_kind': {'type': 'string', 'enum': list(WORK_KINDS), 'description': WORK_KIND_QUESTION},
        'primary_level': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'description': PRIMARY_LEVEL_QUESTION},
        'secondary_kind': {'type': 'string', 'enum': list(SECONDARY_KINDS), 'description': SECONDARY_KIND_QUESTION},
        'secondary_level': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': SECONDARY_LEVEL_QUESTION},
        'access': {'type': 'string', 'enum': list(ACCESS_LEVELS), 'description': ACCESS_QUESTION},
        'local_only': {'type': 'boolean', 'description': LOCAL_ONLY_QUESTION},
        'needs_visual': {'type': 'boolean', 'description': NEEDS_VISUAL_QUESTION},
        'wants_fast': {'type': 'boolean', 'description': WANTS_FAST_QUESTION},
        'expected_hours': {'type': 'number', 'minimum': HOURS_RANGE[0], 'maximum': HOURS_RANGE[1], 'description': (
            'Active worker-hours this task needs at an ordinary agent pace (quick check, runbook, or small review = 0.1-0.25; '
            'standard investigation, bug fix, or 1-5 file feature with tests = 0.3-0.6; wide 8+ file refactor or broad survey = '
            '1-3; multi-stage build or sustained live loop = 4 or more). Estimate work, never money, quota or balances; '
            'brief length and boilerplate add nothing. Use 0.5 when the text gives no basis.')},
        'task_count': {'type': 'string', 'enum': list(TASK_COUNTS), 'description': TASK_COUNT_QUESTION},
    },
    'required': [
        'work_kind', 'primary_level', 'secondary_kind', 'secondary_level',
        'access', 'local_only', 'needs_visual', 'wants_fast', 'expected_hours', 'task_count',
    ],
}


def project_axis_demand(work_kind, primary_level, secondary_kind='none', secondary_level=0):
    """Project `(work_kind, primary_level, secondary_kind, secondary_level)` onto 4D `axis_demand` via tropical max envelope."""
    if work_kind not in WORK_KIND_SIGNATURES:
        raise ValueError('task.profile.work_kind must be one of ' + ', '.join(WORK_KINDS))
    if isinstance(primary_level, bool) or not isinstance(primary_level, int) or not 1 <= primary_level <= 3:
        raise ValueError('task.profile.primary_level must be an integer from 1 to 3')
    if secondary_kind not in SECONDARY_KINDS:
        raise ValueError('task.profile.secondary_kind must be one of ' + ', '.join(SECONDARY_KINDS))
    if isinstance(secondary_level, bool) or not isinstance(secondary_level, int) or not 0 <= secondary_level <= 3:
        raise ValueError('task.profile.secondary_level must be an integer from 0 to 3')
    primary = [ROLE_FN[r](primary_level) for r in WORK_KIND_SIGNATURES[work_kind]]
    if secondary_kind != 'none' and secondary_level > 0:
        secondary = [ROLE_FN[r](secondary_level) for r in WORK_KIND_SIGNATURES[secondary_kind]]
        levels = [max(a, b) for a, b in zip(primary, secondary)]
    else:
        levels = primary
    return dict(zip(axis_scores.FIVE_AXES, levels))


def _check_int(raw, name, low, high):
    value = raw.get(name)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'task.profile.{name} must be an integer from {low} to {high}')
    return value


def validate_profile(raw):
    """Validate a task profile and populate deterministic derived fields (`axis_demand`, `needs_gui`, `read_only_review`,
    `multi_task`). A derived field the input already carries is checked against its derivation; an explicit `axis_demand`
    stands as given (a validated profile read back, the simulation grid and the tray's quota probe name a point of the
    demand space directly)."""
    if not isinstance(raw, dict):
        raise ValueError('task.profile must be an object')
    work_kind = raw.get('work_kind')
    if not isinstance(work_kind, str) or work_kind not in WORK_KINDS:
        raise ValueError('task.profile.work_kind must be one of ' + ', '.join(WORK_KINDS))
    primary_level = _check_int(raw, 'primary_level', 1, 3)
    secondary_kind = raw.get('secondary_kind')
    if not isinstance(secondary_kind, str) or secondary_kind not in SECONDARY_KINDS:
        raise ValueError('task.profile.secondary_kind must be one of ' + ', '.join(SECONDARY_KINDS))
    secondary_level = _check_int(raw, 'secondary_level', 0, 3)
    if secondary_kind == 'none' and secondary_level != 0:
        raise ValueError("task.profile.secondary_level must be 0 when secondary_kind is 'none'")
    if secondary_kind != 'none' and secondary_level == 0:
        raise ValueError("task.profile.secondary_level must be 1 to 3 when secondary_kind is not 'none'")
    if secondary_kind == work_kind:
        raise ValueError("task.profile.secondary_kind must differ from work_kind (use 'none' for single-domain tasks)")

    profile = {
        'work_kind': work_kind,
        'primary_level': primary_level,
        'secondary_kind': secondary_kind,
        'secondary_level': secondary_level,
    }
    for name in REQUIRED_BOOL_FLAGS:
        if not isinstance(raw.get(name), bool):
            raise ValueError(f'task.profile.{name} must be true or false')
        profile[name] = raw[name]

    kinds_used = (work_kind,) if secondary_kind == 'none' else (work_kind, secondary_kind)
    derived_gui = 'gui_control' in kinds_used
    if 'needs_gui' in raw and raw['needs_gui'] is not None:
        if not isinstance(raw['needs_gui'], bool):
            raise ValueError('task.profile.needs_gui must be true or false')
        if derived_gui and raw['needs_gui'] is False:
            raise ValueError("task.profile.needs_gui false contradicts gui_control")
        profile['needs_gui'] = raw['needs_gui']
    else:
        profile['needs_gui'] = derived_gui

    if profile['needs_gui'] or 'visual_analysis' in kinds_used:
        profile['needs_visual'] = True

    derived_ro = work_kind == 'code_audit'
    if 'read_only_review' in raw and raw['read_only_review'] is not None:
        if not isinstance(raw['read_only_review'], bool):
            raise ValueError('task.profile.read_only_review must be true or false')
        if derived_ro and raw['read_only_review'] is False:
            raise ValueError("task.profile.read_only_review false contradicts work_kind 'code_audit'")
        profile['read_only_review'] = raw['read_only_review']
    else:
        profile['read_only_review'] = derived_ro

    access = raw.get('access')
    if access not in ACCESS_LEVELS:
        raise ValueError('task.profile.access must be one of ' + ', '.join(ACCESS_LEVELS))
    if profile['read_only_review'] is True and access == 'write':
        raise ValueError("task.profile.access 'write' contradicts read_only_review / work_kind 'code_audit'")
    if profile['local_only'] is True and work_kind in ('code_implementation', 'frontend_ui') and access == 'read-only':
        raise ValueError(f"task.profile.access 'read-only' contradicts local {work_kind!r}")
    profile['access'] = access

    hours = raw.get('expected_hours')
    if hours is not None:
        try:
            finite = not isinstance(hours, bool) and isinstance(hours, (int, float)) and math.isfinite(hours)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('task.profile.expected_hours must be a finite number')
        profile['expected_hours'] = round(min(max(float(hours), HOURS_RANGE[0]), HOURS_RANGE[1]), 2)

    reason = raw.get('reason')
    if isinstance(reason, str) and reason.strip():
        profile['reason'] = reason.strip()[:300].rstrip()

    profile['task_count'] = task_count_bucket(raw.get('task_count'))
    derived_multi = profile['task_count'] != '1'
    if 'multi_task' in raw and raw['multi_task'] is not None:
        if not isinstance(raw['multi_task'], bool):
            raise ValueError('task.profile.multi_task must be true or false')
        if raw['multi_task'] != derived_multi:
            raise ValueError(f"task.profile.multi_task {str(raw['multi_task']).lower()} contradicts "
                             f"task.profile.task_count {profile['task_count']!r}")
    profile['multi_task'] = derived_multi

    if 'axis_demand' in raw and raw['axis_demand'] is not None:
        profile['axis_demand'] = axis_scores.validate_axis_demand(raw['axis_demand'])
    else:
        profile['axis_demand'] = project_axis_demand(work_kind, primary_level, secondary_kind, secondary_level)
    return profile


class InvalidCallerProfile(ValueError):
    """A profile the caller wrote failed validation; the classifier's own failures retry or block instead."""


def validate_caller_profile(raw):
    try:
        return validate_profile(raw)
    except ValueError as exc:
        raise InvalidCallerProfile(f'invalid_caller_profile: {exc}') from None


def task_count_bucket(value):
    """The schema's task-count band for a band or a positive whole number; anything else is an invalid profile."""
    if isinstance(value, str) and value in TASK_COUNTS:
        return value
    whole = value if isinstance(value, int) and not isinstance(value, bool) else (
        int(value) if isinstance(value, float) and value.is_integer() else None)
    if whole is not None and whole >= 1:
        return TASK_COUNTS[min(whole, 4) - 1]
    raise ValueError('task.profile.task_count must be one of ' + ', '.join(TASK_COUNTS) + ' or a positive whole number')


def profile_difficulty(profile, policy):
    """Display label for a profile's primary_level, from the policy's `profile.difficulty_by_level` (nothing scores or
    gates on it)."""
    lvl = profile['primary_level']
    if lvl == 3 and (
        profile.get('work_kind') in ('architecture_reasoning', 'creative_ideation')
        or profile.get('secondary_level') == 3
    ):
        return 'frontier'
    return policy['profile']['difficulty_by_level'][str(lvl)]


def named_route(profile):
    """A classifier route other than the unmatched bucket. None when the profile did not name one."""
    if not isinstance(profile, dict):
        return None
    named = profile.get('work_kind')
    if named in WORK_KINDS:
        return named
    return None


def profile_kind(profile):
    """The route a profile names (`work_kind`); a profile that names none is `gui_control` when it needs a GUI, else the
    unmatched bucket."""
    named = named_route(profile)
    if named:
        return named
    if isinstance(profile, dict) and profile.get('needs_gui'):
        return 'gui_control'
    return 'other_or_mixed'


def task_kind(task, capabilities):
    """The route plan() uses: the profile's work_kind when caller left work_kind unset or repeated it, else caller's valid route, else other_or_mixed."""
    routes = capabilities['routes']
    if not isinstance(task, dict):
        return 'other_or_mixed'
    prof = task.get('profile') if isinstance(task.get('profile'), dict) else None
    caller = task.get('work_kind')
    named = named_route(prof)
    if named in routes and (caller is None or caller == named):
        return named
    if prof and prof.get('needs_gui') and caller in GENERIC_CALLER_KINDS and named is None:
        return 'gui_control'
    if caller in routes:
        return task['work_kind']
    if not task.get('work_kind') and prof is not None:
        try:
            kind = profile_kind(validate_profile(prof))
        except ValueError:
            return 'other_or_mixed'
        return kind if kind in routes else 'other_or_mixed'
    return 'other_or_mixed'


def _flag_present(mapping, key):
    return isinstance(mapping, dict) and key in mapping and mapping.get(key) is not None


def resolve_modality_needs(needs, profile, implies):
    """Resolve visual and local_only.

    An explicit caller true or false wins, including false. A profile value fills a flag the caller
    left unset (a missing profile key stays unset). Route implies fill only what both left unset.
    needs_gui still forces visual unless the caller explicitly set visual=false.
    """
    needs = needs if isinstance(needs, dict) else {}
    profile = profile if isinstance(profile, dict) else None
    implies = implies if isinstance(implies, dict) else {}
    if _flag_present(needs, 'visual'):
        visual = bool(needs.get('visual'))
    elif profile is not None and (_flag_present(profile, 'needs_visual') or _flag_present(profile, 'needs_gui')):
        visual = bool(profile.get('needs_visual') or profile.get('needs_gui'))
    else:
        visual = bool(implies.get('visual'))
    if profile is not None and profile.get('needs_gui') and not _flag_present(needs, 'visual'):
        visual = True
    if _flag_present(needs, 'local_only'):
        local_only = bool(needs.get('local_only'))
    elif profile is not None and _flag_present(profile, 'local_only'):
        local_only = bool(profile.get('local_only'))
    else:
        local_only = bool(implies.get('local_only'))
    return {'visual': visual, 'local_only': local_only}


def dot_may_take(kind, needs, capabilities, profile=None):
    """Whether Dot is a candidate. It has its own computer and several ways to receive data and return files, so every route
    admits it except work that needs vision and work the caller or classifier marks needs.local_only (it must run on
    this machine). Explicit false is kept. Route implies fill a flag only when the caller and the profile left it unset."""
    route = capabilities['routes'].get(kind) or {}
    resolved = resolve_modality_needs(needs, profile, route.get('implies') or {})
    if resolved.get('local_only') or resolved.get('visual'):
        return False
    return True


def named_pin(constraints, task, accounts):
    """(model the user named, effort the user named) from the constraints or the pinned_* request fields.

    The effort comes from its own field, else from the name (`model:effort`). A name that only a family guess could
    place stays itself: naming gemini-2.5-pro never launches the flagship the family folds to."""
    effort = constraints.get('effort') or task.get('pinned_effort')
    effort = effort.strip().lower() if isinstance(effort, str) and effort.strip() else None
    has_c_model = 'model' in constraints and constraints.get('model') not in (None, '')
    has_p_model = 'pinned_model' in task and task.get('pinned_model') not in (None, '')
    if not has_c_model and not has_p_model:
        bundle = constraints.get('bundle')
        if bundle not in (None, ''):
            return check_model_pin(bundle, 'constraints.bundle'), effort
        return None, effort
    field = 'constraints.model' if has_c_model else 'pinned_model'
    raw = check_model_pin(constraints['model'] if has_c_model else task['pinned_model'], field)
    pool = (account_fleet.pin_pool(accounts)
            if isinstance(accounts, dict) and isinstance(accounts.get('accounts'), dict) else ())
    as_read = account_fleet.effective_pin(raw, pool, accounts) if pool else raw
    resolved = account_fleet.resolve_model(as_read, accounts)
    name = resolved['key'] if resolved['source'] in DIRECT_SOURCES else split_model_effort(as_read)[0]
    return name, effort or resolved['effort']


# Route fields: what AgentCompass sets for itself on the task it plans, from a decision it recorded. They reach
# `normalize_task` only through its `route` argument (allocator.plan(route=...)), never from a caller's task, since each
# changes what may launch without the decision that authorizes it:
# - pinned_bundle: the bundle a recorded user_override launches (dispatch_registry.prepare);
# - user_named_bundle: the bundle the user's naming or an explicit resume launches, which the two routing preferences do
#   not govern (candidate_eval: OUT_OF_AUTOMATIC_ROUTING, UNCHECKED);
# - unclassified: a named launch with no classifier answer, planned from --hours (route_judge.classify_pinned);
# - force_fast: a named fast route (route_judge.record_constraint, prepare).
ROUTE_FIELDS = ('pinned_bundle', 'user_named_bundle', 'unclassified', 'force_fast')
# Keys a caller's task may not carry at all: the route fields, the state the allocator itself derives on the planned task
# (`metered_fallback`, `worker`, `constraint_raw_model`; a caller names its worker in chain.worker and its pin in
# constraints/pinned_*), the rows a record keeps for a listed model, and `include_unverified`, which would lift the
# launch-evidence check (`verified` is discovery's fact).
NOT_CALLER_INPUT = (*ROUTE_FIELDS, 'metered_fallback', 'worker', 'listed_rows', 'include_unverified', 'constraint_raw_model')


def router_fields(task):
    """The fields of `task` only AgentCompass sets (NOT_CALLER_INPUT, and `needs.force_fast`), by name."""
    task = task if isinstance(task, dict) else {}
    needs = task.get('needs') if isinstance(task.get('needs'), dict) else {}
    return [k for k in NOT_CALLER_INPUT if k in task] + [f'needs.{k}' for k in ('force_fast',) if k in needs]


def check_caller_task(task):
    """Refuse a caller's task that carries a field only AgentCompass sets (router_fields), by name; never drop it.
    Called where a caller's task enters (route_judge: plan --input, record), not on a task AgentCompass stored: a stored
    decision in an earlier version's shape is dispatch_registry.earlier_version's."""
    task = task if isinstance(task, dict) else {}
    found = router_fields(task)
    if found:
        raise ValueError(
            f"invalid_task_input: {', '.join(found)} {'is' if len(found) == 1 else 'are'} set by AgentCompass itself, "
            "never by the caller's task. Remove it; to launch a route the user named, use --provider/--account/--model "
            "with the user's words in --basis.")
    return task


COUPLINGS = ('low', 'medium', 'high')       # chain.coupling; high is a related task that continues its worker


def normalize_task(raw, policy, capabilities, accounts, route=None):
    """`raw` is the task to plan: a caller's, checked where it entered (check_caller_task), or one a decision stored;
    a route field in it is never read. `route` holds the route fields AgentCompass passes for a decision it recorded
    (ROUTE_FIELDS)."""
    task = dict(raw or {})
    route = dict(route or {})
    unknown = sorted(set(route) - set(ROUTE_FIELDS))
    if unknown:
        raise ValueError(f"route fields must be among {', '.join(ROUTE_FIELDS)}, not {', '.join(unknown)}")
    profile = validate_caller_profile(task['profile']) if task.get('profile') is not None else None
    needs = dict(task.get('needs')) if isinstance(task.get('needs'), dict) else {}
    if not _flag_present(needs, 'gui') and _flag_present(task, 'needs_gui'):
        needs['gui'] = bool(task['needs_gui'])
    if not _flag_present(needs, 'visual') and _flag_present(task, 'needs_visual'):
        needs['visual'] = bool(task['needs_visual'])
    if not _flag_present(needs, 'local_only') and _flag_present(task, 'local_only'):
        needs['local_only'] = bool(task['local_only'])
    if _flag_present(needs, 'fast'):
        wants_fast = bool(needs['fast'])
    elif _flag_present(task, 'fast'):
        wants_fast = bool(task['fast'])
    elif _flag_present(task, 'wants_fast'):
        wants_fast = bool(task['wants_fast'])
    elif profile is not None and 'wants_fast' in profile:
        wants_fast = bool(profile['wants_fast'])
    else:
        wants_fast = False
    if _flag_present(needs, 'read_only_review'):
        read_only_review = bool(needs['read_only_review'])
    elif _flag_present(task, 'read_only_review'):
        read_only_review = bool(task['read_only_review'])
    elif profile is not None and 'read_only_review' in profile:
        read_only_review = bool(profile['read_only_review'])
    else:
        read_only_review = False
    if profile is not None:
        profile = {
            **profile,
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
        }
    # A named route launched without a classifier answer (route_judge.classify_pinned, only with --hours) carries the
    # reason instead of a profile: no work kind, no difficulty and no category demand are known, and none is made up.
    unclassified = route.get('unclassified') if profile is None and isinstance(route.get('unclassified'), str) else None
    if profile is None and not unclassified:
        raise ValueError('task.profile is required: the classifier always produces it, with its axis_demand')
    if unclassified:
        kind = derived = difficulty = None
        source = 'unclassified'
    else:
        kind = task_kind({**task, 'profile': profile}, capabilities)
        derived = profile_difficulty(profile, policy)
        difficulty = task.get('difficulty') or derived
        source = 'caller' if task.get('difficulty') else 'profile'
        if difficulty not in DIFFICULTY_ORDER:
            raise ValueError('difficulty must be one of ' + ', '.join(DIFFICULTY_ORDER))
    chain = task.get('chain') if isinstance(task.get('chain'), dict) else {}
    coupling = chain.get('coupling') or 'low'
    if coupling not in COUPLINGS:
        raise ValueError('chain.coupling must be low, medium or high')
    if not (task.get('expected_hours') or (profile or {}).get('expected_hours')):
        raise ValueError('expected_hours is required: the caller gives it (--hours) or the classifier estimates it in task.profile')
    hours = float(task.get('expected_hours') or profile['expected_hours'])
    constraints = task.get('constraints') if isinstance(task.get('constraints'), dict) else {}
    raw_pin_model = constraints.get('model') or task.get('pinned_model')
    constraint_raw_model = raw_pin_model.strip() if isinstance(raw_pin_model, str) and raw_pin_model.strip() else None
    constraint_model, constraint_effort = named_pin(constraints, task, accounts)
    implies = (capabilities['routes'][kind].get('implies') or {}) if kind else {}
    # Caller false stands. The profile fills an unset flag. Route implies fill only what both left unset.
    needs.update(resolve_modality_needs(needs, profile, implies))
    worker = dict(chain['worker']) if isinstance(chain.get('worker'), dict) else None
    if worker and worker.get('agent_id'):
        hist = worker_sessions.session_state(worker['agent_id'])
        for k, v in hist.items():
            if k in ('compactions', 'compacted_tokens', 'historical_context_tokens') and isinstance(v, (int, float)):
                worker[k] = max(worker.get(k) or 0, v)
            else:
                worker.setdefault(k, v)
    if _flag_present(needs, 'gui'):
        needs_gui = bool(needs['gui'])
    elif _flag_present(task, 'needs_gui'):
        needs_gui = bool(task['needs_gui'])
    elif profile is not None and _flag_present(profile, 'needs_gui'):
        needs_gui = bool(profile['needs_gui'])
    else:
        needs_gui = bool(kind == 'gui_control')
    return {'kind': kind, 'difficulty': difficulty, 'difficulty_source': source, 'hours': hours,
            'profile': profile, 'profile_difficulty': derived, 'unclassified': unclassified,
            'needs_gui': needs_gui,
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
            'force_fast': bool(route.get('force_fast')),
            'disable_fast': bool(task.get('disable_fast') or needs.get('disable_fast')),
            'pinned_bundle': route.get('pinned_bundle'), 'user_named_bundle': route.get('user_named_bundle'),
            'chain_hours': float(chain.get('total_hours') or hours), 'coupling': coupling, 'worker': worker,
            'handoff_ready': bool(chain.get('handoff_ready')), 'needs_visual': needs['visual'],
            'local_only': bool(needs.get('local_only')),
            'file_transfer': bool(needs.get('file_transfer')), 'outbound_ok': needs.get('outbound_ok'),
            'constraint_bundle': constraints.get('bundle') if isinstance(constraints.get('bundle'), str) else None,
            'constraint_raw_model': constraint_raw_model,
            'constraint_model': constraint_model, 'constraint_effort': constraint_effort,
            'metered_ok': bool(task.get('metered_ok')),
            'can_wait_hours': float(task.get('can_wait_hours') or 0), 'chain_id': chain.get('id'),
            'cwd': task.get('cwd')}
