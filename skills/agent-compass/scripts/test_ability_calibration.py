"""Evidence-derived routing outcomes (accounts.json `ability_floor_basis`, route `ability_floor_basis`,
`route_fit_peers_basis`), pinned as behaviour on fresh-install account sets rather than as numbers.

- SWE-bench Verified by fix time: Flash-class models stay within a tenth of the best model under 15 minutes,
  not at 15 minutes to an hour.
- Terminal-Bench: hard terminal work does not need more than a Flash-class model.
- LMArena WebDev/Agent: Claude Haiku 5.5 and GPT-6 Luna sit with Gemini 3.8 Flash.
- Frontier work is the user's definition (classifier-cases.json `frontier_*`: game architecture, detailed game-system
  design, ultra-low-latency VPS architecture): only the top tier takes it, and nothing lower is offered instead.
"""
from datetime import datetime, timezone
import copy
import json
import unittest

import allocator
import route_judge
import route_simulation as sim
from router_test_support import isolated_router_state
from test_fleet_parity import shipped_template, snapshot


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def flash_class(accounts):
    """Gemini 3.8 Flash and the models the route data declares its peers (no model names in the assertions)."""
    capabilities = allocator.load_capabilities(accounts=accounts)
    anchor = 'gemini-3.8-flash-high'
    return {anchor} | {peer for peer, target in (capabilities.get('route_fit_peers') or {}).items() if target == anchor}


class CalibratedRoutingTests(unittest.TestCase):
    def plan(self, accounts, kind, difficulty, state='ample', local=True, pools=None, account_set=None):
        usage = sim.snapshot(sim.synthetic_document(accounts, NOW, sim.STATES[state], pools), accounts, NOW)
        task = {'objective': f'{kind} task', 'work_kind': kind, 'difficulty': difficulty, 'needs': {'local_only': local}}
        return allocator.plan(task, usage, NOW, accounts=copy.deepcopy(accounts), overrides=[], records=[],
                              host_policy_config=sim.host_policy_for('fresh', accounts, account_set))

    def test_routine_tool_work_goes_to_a_flash_class_model_when_claude_is_scarce(self):
        accounts = sim.fresh_accounts(shipped_template(), snapshot())
        decision = self.plan(accounts, 'tool_execution', 'routine', state='claude scarce')
        model = allocator.canonical(decision['selected_bundle']['model'], accounts)
        self.assertIn(model, flash_class(accounts))

    def test_flash_class_models_qualify_for_routine_but_not_hard_implementation(self):
        accounts = sim.fresh_accounts(shipped_template(), snapshot())
        peers = flash_class(accounts)
        for difficulty, admitted in (('routine', True), ('hard', False)):
            decision = self.plan(accounts, 'general_implementation', difficulty)
            rows = [c for c in decision['ranking'] if allocator.canonical(c['bundle']['model'], accounts) in peers
                    and not c['bundle'].get('disabled')]
            self.assertTrue(rows)
            floor_excluded = [c for c in rows if any('below the' in e for e in c['excluded'])]
            self.assertEqual(not floor_excluded, admitted, (difficulty, [(c['id'], c['excluded']) for c in rows]))

    def test_hard_terminal_work_admits_a_flash_class_model(self):
        accounts = sim.fresh_accounts(shipped_template(), snapshot())
        decision = self.plan(accounts, 'tool_execution', 'hard')
        flash = [c for c in decision['ranking'] if allocator.canonical(c['bundle']['model'], accounts) in flash_class(accounts)]
        self.assertTrue(flash)
        self.assertFalse([c for c in flash if any('below the' in e for e in c['excluded'])])

    def test_coupled_work_needs_more_than_the_tool_and_routine_tiers(self):
        accounts = sim.fresh_accounts(shipped_template(), snapshot())
        decision = self.plan(accounts, 'tool_execution', 'coupled')
        flash = [c for c in decision['ranking'] if allocator.canonical(c['bundle']['model'], accounts) in flash_class(accounts)]
        self.assertTrue(flash)
        self.assertTrue(all(any('below the' in e for e in c['excluded']) for c in flash))

    def test_a_free_tier_only_user_gets_a_worker_for_routine_work(self):
        accounts, pools = sim.accounts_for('fresh', 'opencode free')
        for kind in ('general_implementation', 'tool_execution', 'bounded_bulk_work'):
            decision = self.plan(accounts, kind, 'routine', pools=pools, account_set='opencode free')
            self.assertEqual(decision['selected_bundle']['account'], 'opencode', kind)

    def frontier_cases(self):
        cases = json.loads(route_judge.CLASSIFIER_CASES_PATH.read_text(encoding='utf-8'))['calibration_cases']
        found = [c for c in cases if c['id'].startswith('frontier_')]
        self.assertEqual(len(found), 3)
        return found

    def frontier_plan(self, case, accounts, pools, account_set, state):
        prof = dict(case['expected_profile'])
        usage = sim.snapshot(sim.synthetic_document(accounts, NOW, sim.STATES[state], pools), accounts, NOW)
        task = {'objective': case['task'], 'work_kind': prof['work_kind'], 'profile': prof, 'expected_hours': prof['expected_hours']}
        return allocator.plan(task, usage, NOW, accounts=copy.deepcopy(accounts), overrides=[], records=[],
                              host_policy_config=sim.host_policy_for('fresh', accounts, account_set))

    def test_the_users_frontier_examples_are_frontier_work(self):
        for case in self.frontier_cases():
            prof = allocator.validate_profile(dict(case['expected_profile']))
            self.assertEqual(allocator.profile_difficulty(prof, allocator.load_accounts()['scoring']), 'frontier', case['id'])
            self.assertIn(prof['work_kind'], ('complex_build_plan', 'direction_reasoning'), case['id'])

    def test_frontier_work_goes_to_the_top_tier_or_waits(self):
        floor = allocator.load_accounts()['scoring']['ability_floor']['frontier']
        for account_set in sim.ACCOUNT_SETS:
            accounts, pools = sim.accounts_for('fresh', account_set)
            for case in self.frontier_cases():
                for state in ('ample', 'claude scarce', 'codex scarce'):
                    with self.subTest(account_set=account_set, case=case['id'], state=state):
                        decision = self.frontier_plan(case, accounts, pools, account_set, state)
                        if decision['choice'] != 'none':
                            chosen = next(c for c in decision['ranking'] if c['id'] == decision['choice'])
                            self.assertGreaterEqual(chosen['components']['ability_value'], floor)
                            continue
                        # Nothing below the floor instead: a wait with the next reset, or a clear reason to decide.
                        self.assertTrue(decision['decision']['needs_user_decision'])
                        reason = decision['decision']['reason']
                        self.assertTrue('floor' in reason, reason)
                        able = [c for c in decision['ranking'] if not c.get('below_floor')
                                and 'excluded by host policy' not in c['excluded']]
                        if not able:
                            self.assertIn('No bundle on the usable accounts clears the frontier floor', reason)

    def test_frontier_work_routes_to_a_top_tier_model_where_one_has_quota(self):
        # Claude Max 5 and a pooled Claude plan carry Opus 5.5 with room for a 2-3 h frontier task; Codex-only and
        # Antigravity-only sets do not reach the top tier, and the free tier has no frontier-capable model.
        for account_set, expect in (('claude max5', True), ('magpie pool (3x claude pro)', True), ('codex pro', False),
                                    ('antigravity pro', False), ('opencode free', False)):
            accounts, pools = sim.accounts_for('fresh', account_set)
            for case in self.frontier_cases():
                with self.subTest(account_set=account_set, case=case['id']):
                    decision = self.frontier_plan(case, accounts, pools, account_set, 'ample')
                    self.assertEqual(decision['choice'] != 'none', expect)
        accounts, pools = sim.accounts_for('fresh', 'claude max5')
        scarce = self.frontier_plan(self.frontier_cases()[0], accounts, pools, 'claude max5', 'claude scarce')
        self.assertEqual(scarce['choice'], 'none')
        self.assertGreater(scarce['decision']['next_reset']['hours'], 0)

    def test_a_route_floor_in_the_data_replaces_the_scoring_floor_for_that_difficulty(self):
        policy = {'ability_floor': {'routine': 2.0, 'hard': 3.0}}
        self.assertEqual(allocator.ability_floor(policy, {'ability_floor': {'hard': 2.5}}, 'hard'), 2.5)
        self.assertEqual(allocator.ability_floor(policy, {'ability_floor': {'hard': 2.5}}, 'routine'), 2.0)
        self.assertEqual(allocator.ability_floor(policy, {}, 'hard'), 3.0)


if __name__ == '__main__':
    unittest.main()
