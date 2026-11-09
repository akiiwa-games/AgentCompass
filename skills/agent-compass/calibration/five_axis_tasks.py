"""Synthetic task profiles shared by the five-axis floor calibration and the sampled parity run (round 047).

Each profile carries the classifier fields plus `axis_demand`, labelled by hand from the FIVE_AXIS_DEMAND_SCHEMA rubric
(allocator.py) the way the classifier is asked to answer it. The 53 classifier calibration cases carry no axis_demand
yet; the calibration reads those through allocator.five_axis_fallback_levels.
"""
BASE = {'reasoning_depth': 2, 'code_complexity': 2, 'context_scale': 1, 'divergence': 1, 'hallucination_risk': 2,
        'error_cost': 2, 'needs_visual': False, 'needs_gui': False, 'local_only': True, 'realtime_loop': False,
        'wants_fast': False, 'read_only_review': False, 'interaction_style': 'engineering', 'multi_task': False,
        'task_count': '1', 'expected_hours': 1.0, 'hours_confidence': 'medium', 'urgency': 'unknown'}


def p(demand, **kw):
    div, rigor, code, vision = demand
    return {**BASE, **kw, 'axis_demand': {'divergence': div, 'global_rigor': rigor, 'module_precision': code,
                                          'vision_tools': vision}}


# (label, class, profile); demand = (divergence, global_rigor, module_precision, vision_tools)
TASKS = [
    ('run a release checklist', 'routine-eng', p((0, 1, 0, 0), reasoning_depth=1, code_complexity=0, hallucination_risk=1, expected_hours=0.3)),
    ('tweak a constant + test', 'routine-eng', p((0, 1, 1, 0), reasoning_depth=1, code_complexity=1, hallucination_risk=1, error_cost=1, expected_hours=0.2)),
    ('small helper script', 'routine-eng', p((0, 1, 1, 0), reasoning_depth=1, code_complexity=1, error_cost=1, expected_hours=0.4)),
    ('bulk rename across files', 'routine-eng', p((0, 1, 1, 0), reasoning_depth=1, code_complexity=1, context_scale=2, hallucination_risk=1, expected_hours=1.0)),
    ('reformat text', 'routine-text', p((0, 0, 0, 0), reasoning_depth=1, code_complexity=0, interaction_style='text_only', local_only=False, error_cost=1, expected_hours=0.1)),
    ('summarise a public page', 'routine-text', p((0, 1, 0, 1), reasoning_depth=1, code_complexity=0, interaction_style='text_only', local_only=False, error_cost=1, hallucination_risk=2, expected_hours=0.2)),
    ('click and screenshot a setting', 'routine-gui', p((0, 1, 0, 3), reasoning_depth=1, code_complexity=0, needs_visual=True, needs_gui=True, error_cost=1, expected_hours=0.2)),
    ('describe a screenshot', 'routine-visual', p((1, 1, 0, 2), reasoning_depth=1, code_complexity=0, needs_visual=True, interaction_style='exploration', error_cost=1, expected_hours=0.2)),
    ('implement one module + tests', 'hard-eng', p((0, 1, 2, 0))),
    ('fix a known bug in one module', 'hard-eng', p((0, 1, 2, 0), hallucination_risk=1, expected_hours=0.5)),
    ('frontend component', 'hard-eng', p((1, 1, 2, 2), work_kind='frontend_implementation', needs_visual=True, expected_hours=1.5)),
    ('frontend page, no screenshots', 'hard-eng', p((1, 1, 2, 0), work_kind='frontend_implementation', expected_hours=1.5)),
    ('read-only diff review', 'hard-review', p((0, 2, 0, 0), code_complexity=0, read_only_review=True, hallucination_risk=3, error_cost=1, expected_hours=0.5)),
    ('security review of a module', 'hard-review', p((1, 2, 0, 0), code_complexity=0, read_only_review=True, hallucination_risk=3, context_scale=2, error_cost=2, expected_hours=1.0)),
    ('diagnose a flaky test, cause unknown', 'hard-debug', p((2, 2, 2, 0), divergence=2, hallucination_risk=2, context_scale=2, expected_hours=1.5)),
    ('diagnose perf regression', 'hard-debug', p((2, 2, 1, 0), divergence=2, code_complexity=1, context_scale=2, interaction_style='exploration', expected_hours=2.0)),
    ('log / data exploration', 'hard-explore', p((2, 2, 0, 0), code_complexity=0, interaction_style='exploration', hallucination_risk=3, context_scale=2, error_cost=1, expected_hours=1.0)),
    ('research packet with scripts', 'hard-explore', p((2, 2, 1, 0), code_complexity=1, interaction_style='exploration', hallucination_risk=3, context_scale=2, error_cost=1, expected_hours=2.0)),
    ('backtest and report metrics', 'hard-explore', p((1, 2, 1, 0), code_complexity=1, interaction_style='exploration', hallucination_risk=3, error_cost=1, expected_hours=3.0)),
    ('web research synthesis', 'hard-text', p((1, 2, 0, 1), code_complexity=0, interaction_style='text_only', local_only=False, hallucination_risk=3, context_scale=2, error_cost=1, expected_hours=1.0)),
    ('draft a design doc', 'hard-text', p((2, 2, 0, 0), code_complexity=0, interaction_style='text_only', divergence=2, local_only=False, error_cost=1, expected_hours=1.0)),
    ('analyse UI screenshots for bugs', 'hard-visual', p((1, 2, 0, 2), code_complexity=0, needs_visual=True, interaction_style='exploration', hallucination_risk=3, error_cost=1, expected_hours=0.5)),
    ('GUI workflow automation', 'hard-gui', p((0, 1, 0, 3), code_complexity=0, needs_visual=True, needs_gui=True, expected_hours=1.0)),
    ('run tests and fix failures', 'hard-tool', p((0, 1, 1, 0), work_kind='tool_execution', code_complexity=1, hallucination_risk=1, expected_hours=0.5)),
    ('self-contained algorithm', 'hard-eng', p((1, 1, 2, 0), work_kind='self_contained_code', local_only=False, hallucination_risk=1, error_cost=1, expected_hours=0.5)),
    ('production release run', 'hard-ops', p((0, 3, 0, 0), code_complexity=0, error_cost=3, hallucination_risk=2, expected_hours=0.5)),
    ('cross-module refactor', 'coupled-eng', p((1, 2, 3, 0), reasoning_depth=3, code_complexity=3, context_scale=2, expected_hours=4.0)),
    ('concurrency bug, cause unknown', 'coupled-debug', p((2, 3, 2, 0), reasoning_depth=3, code_complexity=2, context_scale=2, divergence=2, hallucination_risk=3, expected_hours=3.0)),
    ('protocol change across services', 'coupled-eng', p((1, 3, 3, 0), reasoning_depth=3, code_complexity=3, context_scale=3, error_cost=3, expected_hours=6.0)),
    ('architecture review, read-only', 'coupled-review', p((2, 3, 0, 0), reasoning_depth=3, code_complexity=0, read_only_review=True, context_scale=3, hallucination_risk=3, error_cost=1, expected_hours=2.0)),
    ('multi-stage research line', 'coupled-explore', p((2, 3, 1, 0), reasoning_depth=3, code_complexity=1, context_scale=2, interaction_style='exploration', divergence=2, hallucination_risk=3, error_cost=1, expected_hours=4.0)),
    ('complex build plan', 'coupled-advisory', p((2, 3, 0, 0), reasoning_depth=3, code_complexity=0, context_scale=3, interaction_style='advisory', divergence=2, hallucination_risk=3, local_only=False, expected_hours=1.5)),
    ('large feature with UI', 'coupled-eng', p((1, 2, 3, 2), reasoning_depth=3, code_complexity=3, context_scale=2, needs_visual=True, expected_hours=5.0)),
    ('game design ideation', 'coupled-creative', p((3, 1, 0, 0), reasoning_depth=3, code_complexity=0, interaction_style='text_only', divergence=3, local_only=False, error_cost=1, expected_hours=1.0)),
    ('long GUI task', 'coupled-gui', p((1, 2, 0, 3), reasoning_depth=3, code_complexity=0, needs_visual=True, needs_gui=True, context_scale=2, expected_hours=2.0)),
    ('frontier architecture critique', 'frontier-advisory', p((3, 3, 0, 0), reasoning_depth=4, code_complexity=0, context_scale=3, interaction_style='advisory', divergence=3, hallucination_risk=3, local_only=False, error_cost=2, expected_hours=1.5)),
    ('frontier strategy formulation', 'frontier-advisory', p((3, 3, 0, 0), reasoning_depth=4, code_complexity=0, context_scale=2, interaction_style='advisory', divergence=3, hallucination_risk=3, local_only=False, error_cost=1, expected_hours=1.0)),
    ('frontier core-library rewrite', 'frontier-eng', p((2, 3, 3, 0), reasoning_depth=4, code_complexity=3, context_scale=3, divergence=2, hallucination_risk=3, error_cost=3, expected_hours=8.0)),
    ('frontier math proof', 'frontier-text', p((3, 3, 0, 0), reasoning_depth=4, code_complexity=0, interaction_style='text_only', divergence=3, hallucination_risk=3, local_only=False, error_cost=1, expected_hours=2.0)),
    ('frontier research synthesis', 'frontier-explore', p((2, 3, 1, 0), reasoning_depth=4, code_complexity=1, context_scale=3, interaction_style='exploration', divergence=2, hallucination_risk=3, error_cost=1, expected_hours=4.0)),
]
