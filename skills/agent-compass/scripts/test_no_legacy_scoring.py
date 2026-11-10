"""The four category scores and the quota economy are the only scoring: no field of the older scoring may come back.

One test. It fails when a legacy field reappears in the shipped data, in the classifier's schema or answer, in a
normalized task, or as a module-level name of the allocator.
"""
import json
from pathlib import Path
import unittest

import allocator
import route_judge

REFERENCES = Path(__file__).resolve().parents[1] / 'references'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'

# Keys that belonged to the older scoring (a per-model tier score, capability and preference rows and their weights, ability
# gates, exchange-rate pricing, packaged capacity and burn priors, per-difficulty hours). They may appear nowhere in shipped data.
LEGACY_KEYS = {
    'capabilities', 'subjective', 'weights', 'five_axis', 'ability_floor', 'route_ability_floor', 'ability_floor_basis',
    'ability_score_origin', 'critical_headroom_by_difficulty', 'opportunity_exchange', 'shortage_loss_per_usd',
    'shortage_loss_per_worker_hour', 'unmeasured_models', 'mainstream_defaults', 'burn_pct_per_worker_hour',
    'capacity_change', 'free_usage_bonus', 'default_hours', 'fast_burn_multiplier', 'burn_basis', 'context_scale',
    'hallucination_risk', 'error_cost', 'reset_sunk_weight', 'demand_pct', 'surplus_pct',
    # the effort burn curve (the spend of an effort is the usd rate times the hours at that effort) and the flat speed hours
    'effort_burn', 'burn', 'burn_mult', 'speed_hours', 'speed_source',
}
# Keys a model row of the accounts catalogue may not carry: its scores, tier and price live in five-axis-scores.json.
LEGACY_MODEL_KEYS = {'tier', 'visual', 'cost', 'usd_per_hour', 'score_estimated', 'capability', 'dna'}
# Profile fields only the older rows read; the classifier answers `axis_demand` instead.
LEGACY_PROFILE_FIELDS = {'context_scale', 'divergence', 'hallucination_risk', 'error_cost', 'needs_critical'}
LEGACY_ALLOCATOR_NAMES = {
    'USD_PER_HOUR_DEFAULTS', 'exchange_rate', 'effort_ability', 'demand_forecast', 'task_demand', 'five_axis_enabled',
    'load_subjective', 'capability_fit', 'tier_gate', 'ability_floor', 'default_hours',
    'effort_value', 'burn_scale', 'model_duration_multiplier', 'five_axis_duration_multiplier', 'five_axis_speed_hours',
    'FIVE_AXIS_ANCHOR_VALUE',
}


def walk_keys(node, path=''):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path + '/' + key, key
            yield from walk_keys(value, path + '/' + key)
    elif isinstance(node, list):
        for value in node:
            yield from walk_keys(value, path)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


class NoLegacyScoringTests(unittest.TestCase):
    def test_no_legacy_field_in_shipped_data_or_classifier_output(self):
        accounts = load(REFERENCES / 'accounts.json')
        documents = {'accounts.json': accounts, 'reference-accounts.json': load(FIXTURES / 'reference-accounts.json'),
                     'plan-templates.json': load(REFERENCES / 'plan-templates.json'),
                     'model-capabilities.json': load(REFERENCES / 'model-capabilities.json'),
                     'public-data.json': load(REFERENCES / 'public-data.json'),
                     'classifier-cases.json': load(REFERENCES / 'classifier-cases.json')}
        found = [f'{name}{path}' for name, doc in documents.items() for path, key in walk_keys(doc) if key in LEGACY_KEYS]
        self.assertEqual(found, [], 'legacy scoring fields in shipped data')

        # A model row carries routing flags only; its scores, tier and price are the public data's.
        for name, doc in documents.items():
            for model, row in (doc.get('models') or {}).items():
                self.assertFalse(LEGACY_MODEL_KEYS & set(row), (name, model, LEGACY_MODEL_KEYS & set(row)))
        # A subscription account states its plan, never a capacity: that comes from public data only.
        for name, doc in documents.items():
            for account, spec in (doc.get('accounts') or {}).items():
                if spec.get('billing') == 'subscription':
                    self.assertFalse({'capacity_usd', 'capacity_basis'} & set(spec), (name, account))
        # A plan template names window structure; capacity and burn come from public data only.
        for family, plans in load(REFERENCES / 'plan-templates.json').items():
            for plan, template in (plans if isinstance(plans, dict) else {}).items():
                self.assertFalse({'capacity_usd', 'burn'} & set(template if isinstance(template, dict) else ()), (family, plan))

        # The scores the router reads carry the four categories, the economy's inputs and the tier only as provenance.
        scores = load(REFERENCES / 'five-axis-scores.json')
        for model, row in scores['models'].items():
            self.assertEqual(set(row['axes']), set(allocator.FIVE_AXES), model)
            self.assertFalse({'capabilities', 'subjective', 'score'} & set(row), model)

        # The classifier: the answer's schema, the shipped calibration cases and a normalized task.
        schema = route_judge.scorer_schema(allocator, allocator.load_capabilities())
        self.assertFalse(LEGACY_PROFILE_FIELDS & set(schema['properties']), 'classifier schema')
        self.assertIn('axis_demand', schema['required'])
        for case in load(REFERENCES / 'classifier-cases.json')['calibration_cases']:
            self.assertFalse(LEGACY_PROFILE_FIELDS & set(case['expected_profile']), case['id'])
            self.assertEqual(set(case['expected_profile']['axis_demand']), set(allocator.FIVE_AXES), case['id'])
        self.assertFalse(LEGACY_PROFILE_FIELDS & set(allocator.TASK_PROFILE_SCHEMA['properties']))
        accepted = set(allocator.TASK_PROFILE_SCHEMA['properties']) | {'work_kind', 'axis_demand', 'reason'}
        self.assertFalse(LEGACY_PROFILE_FIELDS & accepted)

        # The router's own scoring policy has no switch, weights or gates of the older scoring.
        self.assertFalse(LEGACY_KEYS & set(accounts['scoring']), 'scoring policy')
        self.assertFalse(LEGACY_ALLOCATOR_NAMES & set(dir(allocator)), 'allocator names')


if __name__ == '__main__':
    unittest.main()
