"""The task profile: classifier schema and validation, task kind, modality needs, and task normalization."""

import math

import account_fleet
import axis_scores
import worker_sessions
from model_resolution import DIRECT_SOURCES, split_model_effort


INTERACTION_STYLES = ('engineering', 'exploration', 'text_only', 'advisory')
PROFILE_INTEGERS = {'reasoning_depth': (1, 4), 'code_complexity': (0, 3)}
PROFILE_FLAGS = ('needs_visual', 'needs_gui', 'local_only', 'wants_fast', 'read_only_review')
WANTS_FAST_QUESTION = (
    'True ONLY when the assignment affirmatively requests fast mode, priority service tier, urgent/ASAP turnaround, '
    'or a pre-reset quota sprint (e.g. "pre-reset sprint", "before quota resets", "重置前冲刺") for the task itself, '
    'and does not negate or opt out of fast mode (e.g. "do not use fast mode", "no fast mode", or "not urgent" -> '
    'false). False for action adverbs inside runtime steps (e.g. "click Save immediately when the button appears"), '
    'ordinary mentions of quota/reset without requesting fast execution, or ordinary tasks without an explicit speed/fast request.')
READ_ONLY_REVIEW_QUESTION = (
    'True ONLY when the assignment is a read-only code, diff, branch, security, or release review/audit that inspects '
    'existing material and reports findings without writing or modifying repository production/test code (code_complexity = 0, '
    'even if the reviewer saves its review report file to a specified output path). False when the worker implements, '
    'fixes, or refactors code (including fixing issues found by a prior review), runs operational/release commands, '
    'or performs general exploration/research that is not a code/diff/release audit.')
ACCESS_LEVELS = ('read-only', 'write')
ACCESS_QUESTION = (
    'Whether this brief only reads, inspects, reviews, or analyzes without modifying workspace files or state '
    '("read-only"), or writes, edits, creates, or deletes workspace files or runs mutating commands ("write"). '
    'Any brief with read_only_review=true is "read-only"; any brief with code_complexity > 0 is "write".')
DIFFICULTY_ORDER = ('routine', 'hard', 'coupled', 'frontier')
# The System-One task profile (spec 2026-10-19). Integers carry minimum/maximum, never an integer enum: Gemini's
# protobuf schema rejects integer enums ("enum[0] cannot be empty").
HOURS_RANGE = (0.05, 24.0)
CONFIDENCE_LEVELS = ('low', 'medium', 'high')
URGENCY_LEVELS = ('unknown', 'low', 'normal', 'high')
# Whether the brief bundles separate tasks. One question for every scorer (LLM schema field, Jev noul/choice); the
# parent, not the router, then works out what the tasks are.
TASK_COUNTS = ('1', '2', '3', '>3')
MULTI_TASK_QUESTION = (
    'True when the brief bundles more than one separate task: deliverables that different workers could produce at the '
    'same time without waiting for each other\'s result (e.g. collect reference images, compare game engines, design '
    'the gameplay loop). False for one task, however many steps: investigate-fix-test-release, implement plus its '
    'tests, a review plus its report, the remaining steps of a handoff, a brief that already assigns one share of a '
    'larger task, or a sequence where each part needs the previous part\'s output.')
TASK_COUNT_QUESTION = 'How many separate tasks, in the sense of multi_task, the brief holds: 1 when multi_task is false.'
TASK_PROFILE_SCHEMA = {
    'type': 'object',
    'properties': {
        'reasoning_depth': {'type': 'integer', 'minimum': 1, 'maximum': 4, 'description': (
            'How hard the thinking is, not how long the brief is. 1 = routine or direct execution with obvious steps, even '
            'when the brief is long and sectioned (run an existing script, checklist, drift round, build/install/switch release '
            'runbook; a mechanical edit or constant tweak; click and screenshot; reformat text); 2 = standard engineering or '
            'analysis with a clear direction (implement or fix one module, a standard read-only review of a diff/module, '
            'execute a structured research packet and report metrics, diagnose one subsystem); 3 = cross-module architectural '
            'design/refactor, subtle concurrency or distributed-invariant debugging, or orchestrating a multi-stage research '
            'line; 4 = frontier mathematical, strategic or architectural critique and formulation.')},
        'code_complexity': {'type': 'integer', 'minimum': 0, 'maximum': 3, 'description': (
            'Production code the worker itself writes or modifies (NOT the complexity of code being read or reviewed). '
            '0 = none: ANY read-only code/release/diff review or independent audit (even when saving a review report file '
            'to a specified path), diagnosis that only proposes, running existing CLI/release/test commands, log or data '
            'exploration, text drafting; 1 = a small script, config/constant tweak, localized helper, or analysis scripts '
            'inside a research packet; 2 = a production module implemented, fixed or refactored with its unit tests; '
            '3 = complex multi-module, concurrency, protocol or core-library changes.')},
        'needs_visual': {'type': 'boolean', 'description': 'True when the work must look at images, screenshots or other visual input.'},
        'needs_gui': {'type': 'boolean', 'description': 'True when the work must operate a desktop or browser GUI through screenshots, mouse and keyboard.'},
        'local_only': {'type': 'boolean', 'description': (
            'True when the task explicitly depends on existing files, directories, or repositories on this machine '
            '(reading, copying, searching, editing, building, or testing un-inlined local files) or on local processes/GUI/'
            'environment. False when all required inputs are inlined in the prompt or fetched from the public web '
            '(web_research, self_contained_code, text_in_text_out, direction_reasoning, complex_build_plan)—even if the '
            'brief has a workspace header or names a local target path where the finished output will be saved.')},
        'wants_fast': {'type': 'boolean', 'description': WANTS_FAST_QUESTION},
        'read_only_review': {'type': 'boolean', 'description': READ_ONLY_REVIEW_QUESTION},
        'access': {'type': 'string', 'enum': list(ACCESS_LEVELS), 'description': ACCESS_QUESTION},
        'interaction_style': {'type': 'string', 'enum': list(INTERACTION_STYLES), 'description': (
            'engineering = write or change software; exploration = explore a codebase, run experiments or backtests, '
            'analyze logs or data; text_only = pure text in and out or public web lookup; advisory = direction '
            'reasoning or complex build planning.')},
        'expected_hours': {'type': 'number', 'minimum': HOURS_RANGE[0], 'maximum': HOURS_RANGE[1], 'description': (
            'Active worker-hours this task needs at an ordinary agent pace (a few tool calls = 0.1; one module fix with a '
            'test = 0.5; a multi-file feature or investigation = 1-3; a long multi-stage build = 4 or more). Estimate '
            'work, never money, quota or balances. Use 0.5 when the text gives no basis and set hours_confidence low.')},
        'hours_confidence': {'type': 'string', 'enum': list(CONFIDENCE_LEVELS), 'description': (
            'How well the text supports expected_hours: high = scope and steps are explicit; medium = scope is clear but steps '
            'are not; low = guess.')},
        'urgency': {'type': 'string', 'enum': list(URGENCY_LEVELS), 'description': (
            'unknown unless the text itself states a deadline, an incident or "asap"; then high/normal/low. Never infer urgency '
            'from topic or tone. Shown in the result so the caller can see it; fast mode is controlled by wants_fast or --fast, not urgency.')},
        'multi_task': {'type': 'boolean', 'description': MULTI_TASK_QUESTION},
        'task_count': {'type': 'string', 'enum': list(TASK_COUNTS), 'description': TASK_COUNT_QUESTION},
    },
    'required': list(PROFILE_INTEGERS) + list(PROFILE_FLAGS) + ['access', 'expected_hours', 'hours_confidence', 'urgency', 'interaction_style',
                                                                 'multi_task', 'task_count'],
}
PROFILE_KINDS = ('general_implementation', 'frontend_implementation', 'debug_review', 'bounded_bulk_work', 'tool_execution',
                 'self_contained_code', 'exploration_analysis', 'research_packet', 'computer_use', 'visual_analysis',
                 'text_in_text_out', 'web_research', 'dot_execution', 'direction_reasoning', 'complex_build_plan',
                 'other_or_mixed')
GENERIC_CALLER_KINDS = (None, 'general_implementation', 'tool_execution', 'debug_review', 'other_or_mixed')


def validate_profile(raw):
    """A task profile with every field checked, or ValueError. `reason`, `work_kind` and the hours estimate may be omitted."""
    if not isinstance(raw, dict):
        raise ValueError('task.profile must be an object')
    profile = {}
    if raw.get('work_kind') is not None:
        if not isinstance(raw['work_kind'], str) or raw['work_kind'] not in PROFILE_KINDS:
            raise ValueError('task.profile.work_kind must be one of ' + ', '.join(PROFILE_KINDS))
        profile['work_kind'] = raw['work_kind']
    for name, (low, high) in PROFILE_INTEGERS.items():
        value = raw.get(name)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'task.profile.{name} must be an integer from {low} to {high}')
        profile[name] = value
    for name in PROFILE_FLAGS:
        if not isinstance(raw.get(name), bool):
            raise ValueError(f'task.profile.{name} must be true or false')
        profile[name] = raw[name]
    if profile.get('read_only_review') is True and profile.get('code_complexity') != 0:
        raise ValueError(
            f"task.profile.read_only_review true contradicts task.profile.code_complexity {profile.get('code_complexity')!r} (must be 0)"
        )
    if raw.get('access') is not None:
        if raw['access'] not in ACCESS_LEVELS:
            raise ValueError('task.profile.access must be one of ' + ', '.join(ACCESS_LEVELS))
        if raw['access'] == 'read-only' and profile.get('code_complexity') != 0:
            raise ValueError(
                f"task.profile.access 'read-only' contradicts task.profile.code_complexity {profile.get('code_complexity')!r} (must be 0)"
            )
        if profile.get('read_only_review') is True and raw['access'] == 'write':
            raise ValueError("task.profile.access 'write' contradicts task.profile.read_only_review true")
        profile['access'] = raw['access']
    # The hours estimate is the classifier's to give; with no caller `expected_hours` it is the task's budget.
    hours = raw.get('expected_hours')
    if hours is not None:
        try:
            finite = not isinstance(hours, bool) and isinstance(hours, (int, float)) and math.isfinite(hours)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('task.profile.expected_hours must be a finite number')
        profile['expected_hours'] = round(min(max(float(hours), HOURS_RANGE[0]), HOURS_RANGE[1]), 2)
    for name, levels in (('hours_confidence', CONFIDENCE_LEVELS), ('urgency', URGENCY_LEVELS)):
        if raw.get(name) is not None:
            if raw[name] not in levels:
                raise ValueError(f'task.profile.{name} must be one of ' + ', '.join(levels))
            profile[name] = raw[name]
    if raw.get('interaction_style') not in INTERACTION_STYLES:
        raise ValueError('task.profile.interaction_style must be one of ' + ', '.join(INTERACTION_STYLES))
    profile['interaction_style'] = raw['interaction_style']
    reason = raw.get('reason')
    if isinstance(reason, str) and reason.strip():
        profile['reason'] = reason.strip()[:300].rstrip()
    if not isinstance(raw.get('multi_task'), bool):
        raise ValueError('task.profile.multi_task must be true or false')
    profile['multi_task'] = raw['multi_task']
    profile['task_count'] = task_count_bucket(raw.get('task_count'))
    if profile['multi_task'] != (profile['task_count'] != '1'):
        raise ValueError(f"task.profile.multi_task {str(profile['multi_task']).lower()} contradicts "
                         f"task.profile.task_count {profile['task_count']!r}")
    # The classifier always answers the category demand.
    if raw.get('axis_demand') is None:
        raise ValueError('task.profile.axis_demand is required')
    profile['axis_demand'] = axis_scores.validate_axis_demand(raw['axis_demand'])
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
    """The label for a profile's reasoning depth, from the policy's `profile.difficulty_by_depth` (a display label: nothing
    scores on it)."""
    return policy['profile']['difficulty_by_depth'][str(profile['reasoning_depth'])]


def named_route(profile):
    """A classifier route other than the unmatched bucket. None when the profile did not name one."""
    if not isinstance(profile, dict):
        return None
    named = profile.get('work_kind')
    if named in PROFILE_KINDS and named != 'other_or_mixed':
        return named
    return None


def profile_kind(profile, policy):
    """The route a profile implies when the caller named none.

    A named route stands. needs_gui becomes computer_use only when the profile did not name one;
    the GUI requirement itself is needs_visual / needs_gui, not a route rewrite."""
    named = named_route(profile)
    if named:
        return named
    if profile['needs_gui']:
        return 'computer_use'
    if profile['needs_visual'] and profile['code_complexity'] == 0 and profile['interaction_style'] != 'text_only':
        return 'visual_analysis'
    if profile['interaction_style'] == 'engineering' and profile['code_complexity'] == 0:
        return 'tool_execution'
    return ((policy.get('profile') or {}).get('kind_by_style') or {}).get(profile['interaction_style'], 'other_or_mixed')


def task_kind(task, capabilities, policy=None):
    """The route plan() uses: the caller's work_kind, else the profile's when no work_kind was given, else other_or_mixed."""
    routes = capabilities['routes']
    if not isinstance(task, dict):
        return 'other_or_mixed'
    prof = task.get('profile') if isinstance(task.get('profile'), dict) else None
    caller = task.get('work_kind')
    named = named_route(prof)
    # The classifier's route stands when the caller left the kind unset or repeated it.
    # A generic caller kind plus needs_gui becomes computer_use only when the profile named no real route.
    if named in routes and (caller is None or caller == named):
        return named
    if prof and prof.get('needs_gui') and caller in GENERIC_CALLER_KINDS and named is None:
        return 'computer_use'
    if caller in routes:
        return task['work_kind']
    if not task.get('work_kind') and prof is not None:
        try:
            kind = profile_kind(validate_profile(prof), policy or {})
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
    this machine). dot_execution marked local-only is a contradiction, so Dot is not offered and the plan asks the user.
    Explicit false is kept. Route implies fill a flag only when the caller and the profile left it unset."""
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
    raw = constraints.get('model') or task.get('pinned_model')
    if not raw:
        return constraints.get('bundle') or None, effort
    resolved = account_fleet.resolve_model(raw, accounts)
    name = resolved['key'] if resolved['source'] in DIRECT_SOURCES else split_model_effort(raw)[0]
    return name, effort or resolved['effort']


COUPLINGS = ('low', 'medium', 'high')       # chain.coupling; high is a related task that continues its worker


def normalize_task(raw, policy, capabilities, accounts):
    task = dict(raw or {})
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
    if profile is None:
        raise ValueError('task.profile is required: the classifier always produces it, with its axis_demand')
    kind = task_kind({**task, 'profile': profile}, capabilities, policy)
    derived = profile_difficulty(profile, policy)
    difficulty = task.get('difficulty') or derived
    source = 'caller' if task.get('difficulty') else 'profile'
    if difficulty not in DIFFICULTY_ORDER:
        raise ValueError('difficulty must be one of ' + ', '.join(DIFFICULTY_ORDER))
    chain = task.get('chain') if isinstance(task.get('chain'), dict) else {}
    coupling = chain.get('coupling') or 'low'
    if coupling not in COUPLINGS:
        raise ValueError('chain.coupling must be low, medium or high')
    if not (task.get('expected_hours') or profile.get('expected_hours')):
        raise ValueError('expected_hours is required: the caller gives it (--hours) or the classifier estimates it in task.profile')
    hours = float(task.get('expected_hours') or profile['expected_hours'])
    constraints = task.get('constraints') if isinstance(task.get('constraints'), dict) else {}
    constraint_model, constraint_effort = named_pin(constraints, task, accounts)
    implies = capabilities['routes'][kind].get('implies') or {}
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
        needs_gui = bool(kind == 'computer_use')
    return {'kind': kind, 'difficulty': difficulty, 'difficulty_source': source, 'hours': hours,
            'profile': profile, 'profile_difficulty': derived,
            'needs_gui': needs_gui,
            'wants_fast': wants_fast,
            'read_only_review': read_only_review,
            'force_fast': bool(task.get('force_fast') or needs.get('force_fast')),
            'disable_fast': bool(task.get('disable_fast') or needs.get('disable_fast')),
            'pinned_bundle': task.get('pinned_bundle'),
            'chain_hours': float(chain.get('total_hours') or hours), 'coupling': coupling, 'worker': worker,
            'handoff_ready': bool(chain.get('handoff_ready')), 'needs_visual': needs['visual'],
            'local_only': bool(needs.get('local_only')),
            'file_transfer': bool(needs.get('file_transfer')), 'outbound_ok': needs.get('outbound_ok'),
            'constraint_bundle': constraints.get('bundle') if isinstance(constraints.get('bundle'), str) else None,
            'constraint_model': constraint_model, 'constraint_effort': constraint_effort,
            'metered_ok': bool(task.get('metered_ok')), 'include_unverified': bool(task.get('include_unverified')),
            'can_wait_hours': float(task.get('can_wait_hours') or 0), 'chain_id': chain.get('id'),
            'cwd': task.get('cwd')}
