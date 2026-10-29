import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/route-agent-work/scripts'))

import allocator
from tk_test_support import desktop_test, require_tk
require_tk()
import radar_chart
import router_data

CATALOG_JSON = Path(__file__).resolve().parents[1] / "skills" / "route-agent-work" / "references" / "accounts.json"


class ScoreDimensionTests(unittest.TestCase):
    def test_each_axis_follows_its_formula(self):
        values = radar_chart.score_dimensions({
            "divergence": 0.3, "global_rigor": 0.7, "module_precision": 0.0, "capability": 0.2,
            "fit": 0.7, "subjective_fit": 0.9, "vector_fit": 0.8, "route_fit": 0.6, "ability": 0.95,
            "overqualification": 0.05, "effort_penalty": 0.1, "opportunity_cost": 0.04, "reset_bonus": 0.1,
            "free_usage": 0.0, "boost": 0.05})
        self.assertEqual((values["divergence"], values["global_rigor"], values["module_precision"]), (0.3, 0.7, 0.0))
        self.assertEqual(values["capability"], 0.2)
        self.assertAlmostEqual(values["economy"], 1.0 - 0.2 + 0.1 + 0.05 - 0.05 - 0.1)

    def test_missing_subjective_terms_fall_back_to_fit_and_everything_is_clamped(self):
        values = radar_chart.score_dimensions({"fit": 0.5, "ability": 0.1, "overqualification": 0.4,
                                               "opportunity_cost": 0.0, "free_usage": 0.18})
        self.assertEqual((values["divergence"], values["global_rigor"], values["module_precision"]), (0.5, 0.5, 0.5))
        self.assertEqual(values["capability"], 0.5)
        self.assertAlmostEqual(values["economy"], 0.78)
        self.assertEqual(radar_chart.score_dimensions({"fit": .4})["capability"], .4)
        self.assertEqual(radar_chart.score_dimensions({"fit": .4, "vector_fit": .6})["divergence"], .6)
        self.assertEqual(radar_chart.score_dimensions({"overqualification": 2})["economy"], 0)
        self.assertEqual(radar_chart.score_dimensions({"boost": 2})["economy"], 1)
        self.assertEqual(radar_chart.score_dimensions({})["economy"], 1.0)
        self.assertEqual(radar_chart.clamp(float("nan")), 0.0)
        self.assertEqual(radar_chart.clamp("x"), 0.0)

    def test_raw_terms_for_the_hover_text(self):
        raw = radar_chart.score_raw({"capability": 0.0, "ability": 1.0, "opportunity_cost": 0.08, "boost": 0.35})
        self.assertEqual(raw["capability"], {"capability": 0.0})
        self.assertEqual(raw["economy"], {"opportunity_cost": 0.08, "boost": 0.35})
        raw = radar_chart.score_raw({"divergence": 0, "subjective_fit": .8, "fit": .6,
                                     "effort_penalty": .2, "overqualification": .1})
        self.assertEqual(raw["divergence"], {"divergence": 0})
        self.assertEqual(raw["global_rigor"], {"fit": .6})
        self.assertEqual(raw["capability"], {"fit": .6})
        self.assertEqual(raw["economy"], {"effort_penalty": .2, "overqualification": .1})
        self.assertEqual(radar_chart.score_raw({"capability": .8, "visual": .8, "gui": .6})["capability"],
                         {"visual": .8, "gui": .6})
        self.assertEqual(radar_chart.score_raw({"capability": 0, "capability_override": 0,
                                               "visual": .8})["capability"], {"capability_override": 0})


class DnaDimensionTests(unittest.TestCase):
    def test_model_table_exposes_resolved_prices_without_mutating_catalog(self):
        from types import SimpleNamespace
        models = {'priced': {'usd_per_hour': 15}, 'free': {}, 'estimated': {}}
        fake = SimpleNamespace(REFERENCES=Path('unused'), load_accounts=lambda _: {},
                               models_table=lambda _: models,
                               model_usd_per_hour=lambda name, info: {'priced': 15, 'free': 0, 'estimated': 30}[name])
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router_data, 'modules', return_value=(fake, None)), \
                mock.patch.object(router_data, 'STATE_DIR', Path(directory)):
            resolved = router_data.model_table()
        self.assertEqual([radar_chart.dna_dimensions(resolved[name])['economy'] for name in models],
                         [.5, 1, 1 / 3])
        self.assertNotIn('usd_per_hour', models['free'])

    def test_model_dna_from_subjective_ratings_and_capabilities(self):
        info = {"subjective": {"divergence": 0.62, "global_rigor": 0.98, "module_precision": 0.92},
                "capabilities": {"reasoning": 0.89, "anti_hallucination": 0.92, "visual": 1.0, "gui": 0.55},
                "tier": 3.2, "usd_per_hour": 15}
        self.assertEqual(radar_chart.dna_dimensions(info), {
            "divergence": 0.62, "global_rigor": 0.98, "module_precision": 0.92,
            "capability": 1.0, "economy": .5})
        raw = radar_chart.dna_raw(info)
        self.assertEqual(raw["capability"], {"visual": 1.0, "gui": .55})
        self.assertEqual(raw["economy"], {"usd_per_hour": 15})

    def test_unknown_model_gets_neutral_subjective_and_zero_capabilities(self):
        values = radar_chart.dna_dimensions({})
        self.assertEqual(values["divergence"], 0.5)
        self.assertEqual(values["capability"], 0.0)
        self.assertEqual(values["economy"], 0.75)

    def test_vision_tools_baseline_override_and_allocator_agree(self):
        cases = [
            ({"tier": 4, "capabilities": {"reasoning": 1, "coding": 1}}, 0),
            ({"capabilities": {"visual": .63, "gui": .91}}, .91),
            ({"capabilities": {"visual": 2, "gui": -.3}}, 1),
            ({"capabilities": {"visual": .9}, "subjective": {"capability": 0}}, 0),
            ({"capabilities": {"gui": .9}, "subjective": {"capability": .42}}, .42),
        ]
        for info, expected in cases:
            self.assertEqual(radar_chart.dna_dimensions(info)["capability"], expected)
            self.assertEqual(allocator.vision_tools_score(info), expected)
        self.assertEqual(radar_chart.dna_raw(cases[-1][0])["capability"], {"subjective.capability": .42})

    def test_saved_and_reset_sliders_use_model_specific_baselines(self):
        import router_window
        panel = router_window.Panel.__new__(router_window.Panel)
        panel.models = {"vision": {"capabilities": {"visual": .7, "gui": .8},
                                    "default_subjective": {"divergence": .6},
                                    "subjective": {"divergence": .9, "capability": .2}},
                        "text": {"capabilities": {"reasoning": 1}}}
        self.assertEqual(panel.saved_dna("vision")["capability"], .2)
        self.assertEqual(panel.saved_dna("text")["capability"], 0)
        panel.dna_model = "vision"
        panel.sync_dna_sliders = mock.Mock()
        panel.revert_dna()
        self.assertEqual(panel.dna_draft, {"divergence": .6, "global_rigor": .5,
                                          "module_precision": .5, "capability": .8})
        panel.sync_dna_sliders.assert_called_once()

    def test_free_and_paid_economy_and_axis_keys_are_consistent(self):
        self.assertEqual(radar_chart.dna_dimensions({"usd_per_hour": 0})["economy"], 1)
        self.assertLess(radar_chart.dna_dimensions({"usd_per_hour": 30})["economy"], .5)
        expected = ["divergence", "global_rigor", "module_precision", "capability", "economy"]
        self.assertEqual([key for key, _, _ in radar_chart.SCORE_AXES], expected)
        self.assertEqual([key for key, _, _ in radar_chart.DNA_AXES], expected)
        for result in (radar_chart.score_dimensions({}), radar_chart.score_raw({}),
                       radar_chart.dna_dimensions({}), radar_chart.dna_raw({})):
            self.assertEqual(list(result), expected)

    def test_real_model_table_values_stay_in_range(self):
        models = {k: v for k, v in json.loads(CATALOG_JSON.read_text(encoding="utf-8"))["models"].items()
                  if isinstance(v, dict)}
        self.assertTrue(models)
        for name, info in models.items():
            for value in radar_chart.dna_dimensions(info).values():
                self.assertTrue(0 <= value <= 1, name)


class GeometryTests(unittest.TestCase):
    def test_first_axis_points_up_and_axes_go_clockwise(self):
        x, y = radar_chart.point(100, 100, 50, 0, 5)
        self.assertAlmostEqual((x, y), (100, 50))
        x1, _ = radar_chart.point(100, 100, 50, 1, 5)
        self.assertGreater(x1, 100)

    def test_ring_is_a_regular_pentagon(self):
        points = radar_chart.ring(0, 0, 10)
        sides = [math.dist(points[i], points[(i + 1) % 5]) for i in range(5)]
        self.assertTrue(all(abs(side - sides[0]) < 1e-9 for side in sides))
        self.assertAlmostEqual(sides[0], 2 * 10 * math.sin(math.pi / 5))

    def test_polygon_scales_each_vertex_by_its_clamped_value(self):
        points = radar_chart.polygon([1.0, 0.5, 0.0, 2.0, -1], 0, 0, 100)
        distances = [math.hypot(x, y) for x, y in points]
        for got, want in zip(distances, (100, 50, 0, 100, 0)):
            self.assertAlmostEqual(got, want)

    def test_canvas_labels_and_value_pills_fit_inside_bounds_in_zh_and_en(self):
        import i18n
        import tk_test_support
        import ui_theme

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        chart = radar_chart.RadarChartCanvas(root, size=ui_theme.px(195))
        self.addCleanup(chart.canvas.destroy)
        series = [{"label": "DeepSeek Flash · Adjusted", "values": {k: 0.55 for k, _, _ in radar_chart.DNA_AXES}},
                  {"label": "Saved", "values": {k: 0.45 for k, _, _ in radar_chart.DNA_AXES}}]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(router_data, "STATE_DIR", Path(directory)):
            for lang in ("zh", "en"):
                i18n.set_language(lang)
                chart.show(radar_chart.DNA_AXES, series)
                x0, y0, x1, y1 = chart.canvas.bbox("all")
                self.assertGreaterEqual(x0, 0, lang)
                self.assertGreaterEqual(y0, 0, lang)
                self.assertLessEqual(x1, int(chart.canvas["width"]), lang)
                self.assertLessEqual(y1, int(chart.canvas["height"]), lang)


def candidate(cid, model, score, excluded=(), **components):
    return {"id": cid, "bundle": {"model": model, "account": "a", "effort": "high"}, "score": score,
            "excluded": list(excluded), "components": {"fit": 0.8, "ability": 1.0, "opportunity_cost": 0.01,
                                                       **components}}


PLAN = {"choice": "opus", "decision": {"margin": 0.05},
        "ranking": [candidate("opus", "claude-opus-5-5", 0.5, boost=0.2), candidate("opus_x", "claude-opus-5-5", 0.48),
                    candidate("sol", "gpt-6.1-sol", 0.45, reset_bonus=0.1, overqualification=0.02),
                    candidate("flash", "gemini-3.8-flash-high", None, excluded=["below ability floor"])]}


class DecisionBindingTests(unittest.TestCase):
    def test_breakdown_rows_carry_bonus_penalty_and_hard_gate_reasons(self):
        rows = {r["id"]: r for r in router_data.candidate_rows(PLAN)}
        self.assertEqual(rows["opus"]["bonus"], 0.2)
        self.assertEqual(rows["sol"]["bonus"], 0.1)
        self.assertEqual(rows["sol"]["penalty"], 0.02)
        self.assertEqual(rows["flash"]["excluded"], ["below ability floor"])

    def test_default_pair_is_winner_and_best_other_model(self):
        pair = router_data.decision_pair(PLAN)
        self.assertEqual([r["id"] for r in pair], ["opus", "sol"])
        series, raw = router_data.decision_series(pair)
        self.assertEqual(series[0]["label"], "opus · 0.500")
        self.assertEqual(set(series[0]["values"]), {key for key, _, _ in radar_chart.SCORE_AXES})
        self.assertEqual(raw[1]["economy"]["reset_bonus"], 0.1)

    def test_a_picked_pair_wins_and_excluded_candidates_cannot_be_picked(self):
        self.assertEqual([r["id"] for r in router_data.decision_pair(PLAN, "sol", "opus_x")], ["sol", "opus_x"])
        self.assertEqual([r["id"] for r in router_data.decision_pair(PLAN, "flash")], ["opus", "sol"])
        self.assertEqual(router_data.decision_pair({"ranking": []}), [])

    def test_dna_series_for_two_models(self):
        models = {"a": {"subjective": {"divergence": 1}}, "b": {}}
        series, raw = router_data.dna_series(models, "a", "b")
        self.assertEqual([s["label"] for s in series], ["a", "b"])
        self.assertEqual(series[0]["values"]["divergence"], 1.0)
        self.assertEqual(len(router_data.dna_series(models, "a", "missing")[0]), 1)

    def test_dry_run_binds_the_profile_and_never_writes_the_ledger(self):
        allocator = mock.Mock()
        allocator.infer_task_profile.return_value = {"reasoning_depth": 2}
        allocator.load_accounts.return_value = {"scoring": {}}
        allocator.dot_may_take.return_value = True
        allocator.plan.return_value = PLAN
        router = mock.Mock()
        router.read_ledger.return_value = []
        router.dot_unreachable.return_value = "refused"
        with mock.patch.object(router_data, "modules", return_value=(allocator, router)):
            result = router_data.simulate("修复竞态", 1.5, local_only=True, needs_visual=False)
        self.assertIs(result, PLAN)
        task = allocator.plan.call_args.args[0]
        self.assertEqual(task["expected_hours"], 1.5)
        self.assertTrue(task["needs"]["local_only"])
        self.assertTrue(task["needs"]["local_only_reason"])
        self.assertEqual(task["profile"], {"reasoning_depth": 2})
        self.assertEqual(allocator.plan.call_args.args[3], ["dot_text"])
        router.append_ledger.assert_not_called()

    def test_replay_reuses_the_recorded_profile_when_valid(self):
        allocator = mock.Mock()
        allocator.validate_profile.side_effect = lambda p: p
        with mock.patch.object(router_data, "modules", return_value=(allocator, mock.Mock())), \
                mock.patch.object(router_data, "plan_task", side_effect=lambda task: task):
            task = router_data.replay({"kind": "debug_review", "difficulty": "hard", "hours": 2.0,
                                       "profile": {"reasoning_depth": 3}, "objective": "x"})
        self.assertEqual(task["work_kind"], "debug_review")
        self.assertEqual(task["profile"], {"reasoning_depth": 3})
        allocator.infer_task_profile.assert_not_called()

    def test_ledger_summary(self):
        summary = router_data.ledger_summary({"at": "2026-10-26T08:02:08+00:00", "objective": "# Title\n\nbody",
                                              "kind": "tool_execution", "choice": "dot_text", "margin": 0.2})
        self.assertEqual(summary["objective"], "Title body")
        self.assertEqual(summary["choice"], "dot_text")

    def test_verdict_summary_is_structured_rows_with_the_reason_kept_for_the_tooltip(self):
        import i18n
        import router_window

        reason = "opus (start) scored 0.5; claude-weekly: 40% left; value/cost exchange 0.012 score per displaced USD"
        plan = {**PLAN, "decision": {"margin": 0.05, "reason": reason,
                                     "task": {"kind": "debug_review", "difficulty": "hard", "hours": 4.0}}}
        plan["ranking"] = [dict(plan["ranking"][0], expected_pct=3.456,
                                capacity={"window": "claude-weekly", "remaining": 40, "step_pct": 3.456}),
                           *plan["ranking"][1:]]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(router_data, "STATE_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        i18n.set_language("zh")
        rows, tooltip = router_window.verdict_summary(plan, router_data.decision_pair(plan))
        self.assertEqual(rows, [("胜出", "opus  0.500"), ("次选", "sol  0.450 · 领先 0.0500"),
                                ("任务", "debug_review · hard · 4h"),
                                ("额度", "claude-weekly 剩 40% · 本单 ~3.5% · 机会成本 0.010")])
        self.assertEqual(tooltip, reason)
        i18n.set_language("en")
        rows, _ = router_window.verdict_summary(plan, router_data.decision_pair(plan))
        self.assertEqual(rows[1], ("Runner-up", "sol  0.450 · lead 0.0500"))
        self.assertEqual(rows[3][1], "claude-weekly 40% left · this task ~3.5% · opportunity cost 0.010")

    def test_verdict_summary_marks_ties_and_falls_back_to_a_short_reason(self):
        import i18n
        import router_window

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(router_data, "STATE_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        i18n.set_language("en")
        plan = {"choice": "a", "decision": {"reason": "a (start) scored 0.3; free usage window", "task": {}},
                "ranking": [candidate("a", "m1", 0.3), candidate("b", "m2", 0.3)]}
        rows, _ = router_window.verdict_summary(plan, router_data.decision_pair(plan))
        self.assertEqual(rows[1], ("Runner-up", "b  0.300 · tied"))
        self.assertEqual(rows[2], ("Task", "— · — · —"))
        self.assertEqual(rows[3], ("Quota", "a (start) scored 0.3"))
        self.assertEqual(router_window.verdict_summary({"decision": {}}, []), ([("", "No eligible candidates")], ""))


class AccountCardTests(unittest.TestCase):
    def cards(self, window):
        allocator = mock.Mock()
        allocator.load_accounts.return_value = {"accounts": {"claude-lab": {"windows": {"five_hour": {"hours": 5}}}}}
        allocator.canonical_window_id.side_effect = lambda spec, wid: wid
        allocator.window_capacity_usd.return_value = None
        allocator.host_policy.load_policy.return_value = None
        router = mock.Mock()
        router.usage_snapshot.return_value = {"accounts": {"claude-lab": {"status": "fresh", "windows": [window]}}}
        inventory = mock.Mock()
        inventory.current.return_value = {"accounts": {}}
        inventory.quota_sources.return_value = {}
        with tempfile.TemporaryDirectory() as directory,                 mock.patch.object(router_data, "modules", return_value=(allocator, router)),                 mock.patch.dict(sys.modules, {"provider_inventory": inventory}),                 mock.patch.object(router_data, "STATE_DIR", Path(directory)),                 mock.patch.object(router_data.plugins, "load", return_value={}),                 mock.patch.object(router_data, "account_badge", return_value=("", "")):
            return router_data.account_cards()[0]["windows"][0]

    def test_a_window_past_its_reset_shows_refilled_pending_a_new_reading(self):
        window = self.cards({"id": "five_hour", "used_pct": 100, "reset_time_passed": True,
                             "seconds_until_reset": 0, "observed_remaining_pct": 0,
                             "remaining_pct_before_reset": None})
        self.assertEqual((window["remaining_pct"], window["reset_in"], window["stale"]), (100.0, "已重置", True))

    def test_a_current_window_keeps_its_reading(self):
        window = self.cards({"id": "five_hour", "used_pct": 90, "reset_time_passed": False,
                             "seconds_until_reset": 3600, "observed_remaining_pct": 10,
                             "remaining_pct_before_reset": 10})
        self.assertEqual(window["remaining_pct"], 10)
        self.assertFalse(window["stale"])
        self.assertNotEqual(window["reset_in"], "已重置")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.accounts = self.dir / 'fixture-accounts.json'
        shutil.copy(allocator.reference_fleet_path(), self.accounts)
        patcher = mock.patch.object(router_data, "STATE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        policy = self.dir / "host-policy-fixture.json"
        names = list(json.loads(self.accounts.read_text(encoding="utf-8-sig"))["accounts"])
        policy.write_text(json.dumps({"allowed_accounts": names}), encoding="utf-8")
        environment = mock.patch.dict("os.environ", {
            "ROUTER_ACCOUNTS_OVERRIDE": str(self.dir / "accounts-override.json"),
            "ROUTER_HOST_POLICY": str(policy),
            "ROUTER_ACCOUNTS_FILE": str(self.accounts),
            "ROUTER_STATE_DIR": str(self.dir),
            "ROUTER_CAPACITY": str(self.dir / 'capacity.json'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(router_data, 'modules', return_value=(allocator, mock.Mock())),
            mock.patch.object(router_data, '_accounts_path', return_value=self.accounts),
            mock.patch.object(allocator.provider_inventory, 'apply', return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tuning_is_changed_in_place_backed_up_and_restored_byte_for_byte(self):
        target = self.dir / "accounts.json"
        shutil.copy(self.accounts, target)
        original = target.read_bytes()
        message = router_data.save_tuning({"safety_margin_pct": 7, "subjective.weight": 0.6,
                                           "subjective.route_weight": 0.06}, target)
        self.assertIn("3", message)
        changed = target.read_bytes().splitlines()
        self.assertEqual(sum(a != b for a, b in zip(original.splitlines(), changed)), 3)
        self.assertEqual(router_data.read_tuning(target)["subjective.weight"], 0.6)
        self.assertEqual(len(list((self.dir / "backups").iterdir())), 1)
        self.assertEqual(router_data.save_tuning({"safety_margin_pct": 7}, target), "参数未变化")
        router_data.restore_tuning(target)
        self.assertEqual(target.read_bytes(), original)

    def test_tuning_rejects_out_of_range_and_unbalanced_weights(self):
        with self.assertRaises(ValueError):
            router_data.validate_tuning({"safety_margin_pct": 80})
        with self.assertRaises(ValueError):
            router_data.validate_tuning({"subjective.weight": 0.9})
        with self.assertRaises(ValueError):
            router_data.validate_tuning({"bogus": 1})
        self.assertEqual(router_data.validate_tuning({"rotate_after_carried_tokens": "1500000"}),
                         {"rotate_after_carried_tokens": 1500000})

    def test_host_policy_writes_a_subset_and_removes_the_file_when_everything_is_allowed(self):
        allocator, _ = router_data.modules()
        with mock.patch.object(router_data, "modules", return_value=(allocator, mock.Mock())):
            path = self.dir / "host-policy.json"
            router_data.save_host_policy(["codex", "claude"], ["claude", "codex", "dot"], path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"allowed_accounts": ["claude", "codex"]})
            router_data.save_host_policy(["claude", "codex", "dot"], ["claude", "codex", "dot"], path)
            self.assertFalse(path.exists())
            router_data.save_host_policy(["claude", "codex", "dot"], ["claude", "codex", "dot"], path,
                                         {"allowed_accounts": ["claude"], "judges": ["codex"]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["judges"], ["codex"])
            with self.assertRaises(ValueError):
                router_data.save_host_policy([], ["claude"], path)

    def test_auto_discovery_policy_preserves_manual_disable_and_concurrent_new_accounts(self):
        path = self.dir / 'host-policy.json'
        original = {'allowed_accounts': ['codex', 'grok-bot'],
                    'known_accounts': ['codex', 'grok-bot', 'removed-slot'], 'judges': ['codex']}
        # The panel opened before a separate discovery refresh admitted codex-work.
        current = {**original, 'allowed_accounts': [*original['allowed_accounts'], 'codex-work'],
                   'known_accounts': [*original['known_accounts'], 'codex-work']}
        path.write_text(json.dumps(current))
        # Even a dialog opened before migration must retain the on-disk marker.
        router_data.save_host_policy(['codex'], ['codex', 'grok-bot', 'codex-work'], path,
                                     {'allowed_accounts': original['allowed_accounts']})
        saved = json.loads(path.read_text())
        self.assertEqual(saved['allowed_accounts'], ['codex', 'codex-work'])
        self.assertIn('grok-bot', saved['known_accounts'])
        self.assertIn('removed-slot', saved['known_accounts'])
        self.assertEqual(saved['judges'], ['codex'])
        # Selecting every currently shown account must not delete this bounded policy.
        router_data.save_host_policy(['codex', 'grok-bot', 'codex-work'],
                                     ['codex', 'grok-bot', 'codex-work'], path, saved)
        self.assertTrue(path.exists())
        self.assertIn('removed-slot', json.loads(path.read_text())['known_accounts'])

    def test_unrelated_setting_change_does_not_deny_unready_newcomer(self):
        allocator, _ = router_data.modules()
        path = self.dir / 'host-policy.json'
        account_file = self.dir / 'accounts.json'
        account_file.write_text('{"accounts": {}}')
        roster = ['codex', 'grok', 'codex-new', 'claude-lab']

        def change(allowed, account, mode):
            existing = json.loads(path.read_text())
            if mode == 'dialog':
                router_data.save_host_policy(allowed, roster, path, existing)
            else:
                state = {'accounts': roster, 'allowed': set(existing['allowed_accounts']),
                         'path': path, 'policy': existing}
                with mock.patch.object(router_data, 'host_policy_state', return_value=state), \
                     mock.patch.object(router_data, '_user_accounts_path', return_value=account_file):
                    router_data.toggle_account_host(account)

        for mode in ('dialog', 'toggle'):
            with self.subTest(mode=mode), \
                 mock.patch.object(allocator.host_policy, 'policy_path', return_value=(path, False)):
                path.write_text(json.dumps({'allowed_accounts': ['codex', 'grok'],
                                            'known_accounts': ['codex', 'grok', 'claude-lab']}))
                change(['codex'], 'grok', mode)
                saved = json.loads(path.read_text())
                self.assertNotIn('codex-new', saved['known_accounts'])
                ready = {'_discovery': {'accounts': {'codex-new': {'status': 'ready'}}},
                         'accounts': {'codex-new': {'verified': True,
                                                   'bundles': {'worker': {'provider': 'codex-new'}}}}}
                self.assertEqual(allocator.host_policy.sync_discovered(ready)['added'], ['codex-new'])
                change(['codex'], 'codex-new', mode)
                self.assertEqual(allocator.host_policy.sync_discovered(ready)['added'], [])
                saved = json.loads(path.read_text())
                self.assertEqual(saved['allowed_accounts'], ['codex'])
                self.assertIn('codex-new', saved['known_accounts'])
                self.assertIn('claude-lab', saved['known_accounts'])

    def test_boost_rules_validate_range_and_duration_before_touching_the_store(self):
        with mock.patch.object(router_data, "modules", side_effect=AssertionError("store touched")):
            with self.assertRaises(ValueError):
                router_data.add_boost("claude", None, 0.8, 6, False)
            with self.assertRaises(ValueError):
                router_data.add_boost("claude", None, 0.2, 5, False)

    def test_usage_refresh_asks_usage_float_for_named_accounts_only(self):
        accounts = {'accounts': {}, '_discovery': {'accounts': {
            'team-main': {'family': 'claude'}, 'team-backup': {'family': 'claude'}, 'dot': {'family': 'dot'}}}}
        with mock.patch.object(allocator, 'load_accounts', return_value=accounts), \
                mock.patch.object(router_data, "collect_usage_now") as collect:
            self.assertIn("Team Main", router_data.refresh_usage(["team-main", "dot"]))
            collect.assert_called_once_with(["team-main"])
            collect.reset_mock()
            self.assertIn("Team Backup", router_data.refresh_usage(["team-backup", "dot"]))
            collect.assert_called_once_with(["team-backup"])
            self.assertEqual(router_data.refresh_usage(["dot"]), "该账号没有可刷新的额度来源")

    def test_countdown(self):
        self.assertEqual(router_data.countdown(90061), "1天1小时")
        self.assertEqual(router_data.countdown(3660), "1小时01分")
        self.assertEqual(router_data.countdown(None), "—")

    def test_normalize_weights_keeps_sum_equal_to_one(self):
        balanced = router_data.normalize_weights(
            "subjective.weight", 0.70,
            {"subjective.weight": 0.65, "subjective.vector_weight": 0.20, "subjective.route_weight": 0.15},
        )
        self.assertAlmostEqual(sum(balanced.values()), 1.0, places=2)
        self.assertEqual(balanced["subjective.weight"], 0.70)

    def test_save_model_subjective_and_toggle_bundle_write_to_override(self):
        msg = router_data.save_model_subjective("gpt-6.1-sol", 0.55, 0.60, 0.95)
        self.assertIn("Sol 6.1", msg)
        override = self.dir / "accounts-override.json"
        self.assertTrue(override.is_file())
        doc = json.loads(override.read_text(encoding="utf-8"))
        self.assertEqual(doc["models"]["gpt-6.1-sol"]["subjective"]["global_rigor"], 0.60)
        b_msg = router_data.toggle_bundle("claude", "opus55_xhigh")
        self.assertIn("已停用", b_msg)
        doc2 = json.loads(override.read_text(encoding="utf-8"))
        self.assertTrue(doc2["accounts"]["claude"]["bundles"]["opus55_xhigh"].get("disabled"))
        fake_alloc = mock.Mock()
        fake_alloc.load_accounts.return_value = {
            "accounts": {
                "claude-magpie": {
                    "bundles": {
                        "claude-magpie__sonnet55_high": {"model": "claude-sonnet-5-5", "provider": "claude-magpie"}
                    }
                }
            }
        }
        with mock.patch.object(router_data, "modules", return_value=(fake_alloc, mock.Mock())):
            dyn_msg = router_data.toggle_bundle("claude-magpie", "claude-magpie__sonnet55_high")
            self.assertIn("Claude Magpie", dyn_msg)
            self.assertIn("已停用", dyn_msg)
        doc3 = json.loads(override.read_text(encoding="utf-8"))
        self.assertTrue(doc3["accounts"]["claude-magpie"]["bundles"]["claude-magpie__sonnet55_high"].get("disabled"))

    def test_save_dot_repo_writes_dot_plugin_json(self):
        msg = router_data.save_dot_repo("owner/dot-transfer")
        self.assertIn("owner/dot-transfer", msg)
        self.assertEqual(router_data.dot_details()["transfer_repo"], "owner/dot-transfer")


class WorkerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.allocator = mock.Mock()
        self.allocator.load_accounts.return_value = {"scoring": {"rotate_after_carried_tokens": 2000000}}
        self.allocator.parse_time.side_effect = lambda v: (
            router_data.datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None)
        self.allocator.session_state.side_effect = lambda agent: {"compacted_tokens": 1000, "context_tokens": 500}
        self.allocator.worker_historical_tokens.side_effect = lambda s: s["compacted_tokens"] + s["context_tokens"]

    def iso(self, seconds_ago):
        return (router_data.now_utc() - router_data.timedelta(seconds=seconds_ago)).isoformat()

    def dispatch_db(self, rows):
        import sqlite3

        path = self.dir / "dispatch.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE dispatches (data TEXT)")
        connection.executemany("INSERT INTO dispatches VALUES (?)", [(json.dumps(r),) for r in rows])
        connection.commit()
        connection.close()
        return path

    def agent(self, agent_id, status, seconds_ago, **extra):
        folder = self.dir / "paseo" / "agents" / "ws"
        folder.mkdir(parents=True, exist_ok=True)
        doc = {"id": agent_id, "provider": "claude", "lastStatus": status, "title": f"task {agent_id}",
               "updatedAt": self.iso(seconds_ago), "lastActivityAt": self.iso(seconds_ago),
               "config": {"model": "claude-opus-5-5"}, **extra}
        (folder / f"{agent_id}.json").write_text(json.dumps(doc), encoding="utf-8")

    def test_active_dispatch_rows_and_live_paseo_agents_are_both_listed(self):
        future = (router_data.now_utc() + router_data.timedelta(hours=5)).isoformat()
        db = self.dispatch_db([
            {"agent_id": "a-started", "state": "started", "hard_until": future, "started_at": self.iso(60),
             "bundle": {"bundle_id": "opus55_high", "model": "claude-opus-5-5"}, "request": {"title": "from db"}},
            {"agent_id": "b-resumed", "state": "completed", "bundle": {"bundle_id": "sonnet55_high"}},
            {"agent_id": "c-done", "state": "completed"},
            {"agent_id": "d-expired", "state": "started", "hard_until": self.iso(10)}])
        self.agent("b-resumed", "running", 30)                       # completed dispatch, resumed by a prompt
        self.agent("c-done", "idle", 2 * 3600)                       # dispatched, idle within six hours
        self.agent("e-session", "initializing", 5)                   # a session the router never dispatched
        self.agent("f-old", "idle", 9 * 3600, labels={"router.dispatch_id": "f"})
        self.agent("g-archived", "running", 5, archivedAt=self.iso(1))
        self.agent("h-closed", "closed", 60, labels={"router.dispatch_id": "h"})
        with mock.patch.object(router_data, "modules", return_value=(self.allocator, mock.Mock())):
            rows = router_data.workers(db, self.dir / "paseo")
        by_id = {r["agent_id"]: r for r in rows}
        self.assertEqual(set(by_id), {"a-started", "b-resumed", "c-done", "e-session"})
        self.assertEqual(by_id["a-started"]["state"], "运行中")
        self.assertEqual(by_id["a-started"]["title"], "from db")
        self.assertEqual(by_id["b-resumed"]["state"], "运行中")
        self.assertEqual(by_id["b-resumed"]["bundle"], "sonnet55_high")
        self.assertEqual(by_id["c-done"]["state"], "近期待命")
        self.assertEqual(by_id["e-session"]["state"], "启动中")
        self.assertEqual(by_id["e-session"]["bundle"], "Claude · Opus 5.5")
        self.assertEqual(by_id["e-session"]["historical_tokens"], 1500)
        self.assertEqual(rows[-1]["agent_id"], "c-done")

    def test_an_explicit_dispatch_db_alone_skips_the_paseo_scan(self):
        db = self.dispatch_db([{"agent_id": "x", "state": "launched"}])
        with mock.patch.object(router_data, "modules", return_value=(self.allocator, mock.Mock())), \
                mock.patch.object(router_data, "_paseo_agents", side_effect=AssertionError("scanned")):
            rows = router_data.workers(db)
        self.assertEqual([(r["agent_id"], r["state"]) for r in rows], [("x", "启动中")])


class LanguageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        patcher = mock.patch.object(router_data, "STATE_DIR", self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_invalid_and_external_settings(self):
        import i18n

        path = self.directory / "ui-settings.json"
        self.assertEqual(i18n.get_language(), "zh")
        self.assertFalse(path.exists())
        for value in ('{"language":"xx"}', '[]', '{broken'):
            path.write_text(value, encoding="utf-8")
            self.assertEqual(i18n.get_language(), "zh")
        self.assertEqual(router_data.save_ui_language("en"), "en")
        self.assertEqual(json.loads(path.read_text()), {"language": "en"})
        # Cached lookups do not re-read JSON; an external atomic replacement is noticed.
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unexpected JSON read")):
            self.assertEqual(i18n.get_language(), "en")
        replacement = self.directory / "replacement.json"
        replacement.write_text('{"language":"zh"}', encoding="utf-8")
        replacement.replace(path)
        self.assertEqual(i18n.get_language(), "zh")
        self.assertEqual(router_data.save_ui_language("invalid"), "zh")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        with tempfile.TemporaryDirectory() as other, mock.patch.object(router_data, "STATE_DIR", Path(other)):
            self.assertEqual(i18n.get_language(), "zh")

    def test_dynamic_labels_presets_badges_and_statuses(self):
        import i18n
        import router_window
        import easytier
        import network_status
        import plugins
        import services
        import ui_theme

        i18n.set_language("en")
        self.assertEqual([p[1] for p in router_window.PAGES],
                         ["Decision Compass", "Quota Monitor", "Model Settings", "Integrations"])
        self.assertEqual(radar_chart.DNA_AXES[0][1], "Divergence")
        self.assertEqual(radar_chart.DNA_AXES[3][1], "Vision & Tools")
        self.assertEqual(radar_chart.axis_title(), "Capabilities & Quota")
        self.assertIn("divergence:", radar_chart.SCORE_AXES[0][2])
        for axes in (radar_chart.SCORE_AXES, radar_chart.DNA_AXES):
            for _, label, formula in axes:
                self.assertFalse(any('\u4e00' <= char <= '\u9fff' for char in label + formula))
        self.assertEqual(router_data.PRESETS[0][0], "Cross-module architecture refactor")
        self.assertEqual(router_data.strategy_preset_key("Save Quota"), "省额度")
        self.assertIn("省额度", router_data.STRATEGY_PRESETS)
        self.assertEqual(router_data.countdown(3600), "1h 00m")
        self.assertEqual(router_window.short_badge("Paused: disabled"), "Paused")
        self.assertEqual(router_window.short_badge("Blocked by Host"), "Blocked by Host")
        self.assertEqual(easytier.label({"state": easytier.RUNNING, "peers": [{"cost": "p2p"}, {"cost": "p2p"}]}),
                         "Running (2 peers)")
        self.assertEqual(network_status.peer_values({"cost": "Local"})[2], "Local")
        self.assertEqual(plugins.PLUGINS[0].description, "Desktop quota floating window")
        self.assertEqual(ui_theme.status_kind("Running (2 peers)"), "ok")
        with mock.patch.object(services, "chatgpt_ready", return_value=False), \
                mock.patch.object(services, "chatgpt_main_pid", return_value=None):
            self.assertEqual(services.chatgpt_label(), "Stopped")
        i18n.set_language("zh")
        self.assertEqual(router_window.PAGES[0][1], "决策罗盘")
        self.assertEqual(radar_chart.DNA_AXES[0][1], "发散创新")
        self.assertEqual(plugins.PLUGINS[0].description, "桌面额度悬浮窗")

    def test_panel_switch_rebuilds_navigation_and_current_settings(self):
        import router_window
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        # No service probes, network calls or workers are needed to test real Tk labels.
        with mock.patch.object(router_window.Panel, "run"), \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(router_window, "log_error") as errors:
            panel = router_window.Panel(root, "policy")
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            panel.show_setting("system")
            self.assertEqual(panel.nav["scoring"][2].cget("text"), "决策罗盘")
            panel.language_box.set("English")
            panel.language_box.event_generate("<<ComboboxSelected>>")
            root.update_idletasks()
            self.assertEqual(json.loads((self.directory / "ui-settings.json").read_text()), {"language": "en"})
            self.assertEqual([panel.nav[key][2].cget("text") for key, _, _ in router_window.PAGES],
                             ["Decision Compass", "Quota Monitor", "Model Settings", "Integrations"])
            self.assertEqual((panel.current, panel.current_setting), ("policy", "system"))
            self.assertEqual(panel.status.get(), "Language switched to English")
            self.assertIn("Custom OpenAI-compatible endpoint", panel.scorer_type_box.cget("values"))
            panel.change_language("zh")
            root.update_idletasks()
            self.assertEqual(panel.nav["policy"][2].cget("text"), "模型设置")
            self.assertEqual(panel.language_box.get(), "简体中文")
            self.assertEqual(panel.status.get(), "已切换界面语言为简体中文")
            # A result from the previous widget generation must not touch destroyed controls.
            stale = mock.Mock()
            panel.busy_jobs = 1
            panel.results.put((panel._generation - 1, stale, "old result", None))
            root.after_cancel(panel._poll_id)
            panel.poll()
            stale.assert_not_called()
            self.assertEqual(panel.busy_jobs, 0)
            errors.assert_not_called()

    def test_english_pages_build_and_radar_redraws_after_switch(self):
        import i18n
        import router_window
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        i18n.set_language("en")
        with mock.patch.object(router_window.Panel, "run"), \
                mock.patch.object(router_window.Panel, "prefill"), \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(router_window.plugins, "load", return_value=router_window.plugins.defaults()), \
                mock.patch.object(router_window, "log_error") as errors:
            panel = router_window.Panel(root)
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            panel.models_loaded({'claude-opus-5-5': {'usd_per_hour': 15}, 'gemini-4-argon-high': {}})
            self.assertEqual(panel.mode.get(), 'dna')
            self.assertEqual(len(panel.radar.series), 2)
            self.assertIn('Static price', panel.radar_source.cget('text'))
            panel.plan_loaded(PLAN)
            self.assertEqual(panel.mode.get(), 'score')
            self.assertIn('Quota costs', panel.radar_source.cget('text'))
            # A later catalog refresh must not overwrite the active decision.
            panel.models_loaded(panel.models)
            self.assertEqual(panel.radar.series[0]['label'], 'opus · 0.500')
            self.assertEqual(panel.radar.axes[0][1], "Divergence")
            self.assertEqual(panel.radar_title.cget("text"), "Capabilities & Quota")
            self.assertFalse(hasattr(panel, "mode_switch"))
            for page in ("quota", "plugins", "policy"):
                panel.show(page)
            for setting, _ in router_window.SETTINGS:
                panel.show_setting(setting)
                if setting == "strategy":
                    self.assertEqual(set(panel.dna_vars),
                                     {"divergence", "global_rigor", "module_precision", "capability"})
                    self.assertEqual(panel.dna_vars["capability"].get(), 0)
            panel.show("scoring")
            panel.change_language("zh")
            panel.render_radar()
            self.assertEqual(panel.radar.axes[0][1], "发散创新")
            self.assertEqual(panel.radar_title.cget("text"), "能力与额度五维")
            root.update_idletasks()
            errors.assert_not_called()


class WindowHelperTests(unittest.TestCase):
    def test_layout_keeps_the_tray_page_keys(self):
        import router_window

        self.assertEqual([p[0] for p in router_window.PAGES], ["scoring", "quota", "policy", "plugins"])
        self.assertEqual([s[0] for s in router_window.SETTINGS], ["models", "strategy", "system"])

    def test_duplicate_bundle_labels_fall_back_to_ids(self):
        import router_window

        bundles = [{"id": "sol_xhigh", "label": "Sol 6.1·xhigh"}, {"id": "sol", "label": "Sol 6.1"},
                   {"id": "work_sol_xhigh", "label": "Sol 6.1·xhigh"}]
        self.assertEqual(router_window.bundle_labels(bundles), ["sol_xhigh", "Sol 6.1", "work_sol_xhigh"])

    def test_matching_preset_marks_the_saved_strategy(self):
        import router_window

        presets = router_data.STRATEGY_PRESETS
        self.assertEqual(router_window.matching_preset(dict(presets["省额度"]), presets), "省额度")
        self.assertIsNone(router_window.matching_preset({**presets["省额度"], "safety_margin_pct": 13}, presets))
        self.assertIsNone(router_window.matching_preset({}, presets))

    def test_badges_and_long_text_are_shortened(self):
        import router_window

        self.assertEqual(router_window.short_badge("已暂停：disabled"), "已暂停")
        self.assertEqual(router_window.clip("abcdef", 4), "abc…")
        self.assertEqual(router_window.clip(None), "—")

    @desktop_test
    def test_filled_scroll_frame_keeps_the_table_rows_visible(self):
        """Tall content above the worker table scrolls instead of squeezing the table to its headings."""
        import tkinter as tk
        from tkinter import ttk
        import router_window
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.geometry("+-3000+-3000")  # mapped (heights are real) but off screen
        root.deiconify()
        holder = tk.Frame(root, width=400, height=300)
        holder.pack_propagate(False)
        holder.pack()
        scroll = router_window.ScrollFrame(holder, fill=True)
        cards = tk.Frame(scroll.body, height=250)
        cards.pack(fill="x")
        cards.bind("<Configure>", scroll.fit, add="+")
        table = ttk.Treeview(scroll.body, columns=("a",), show="headings", height=4)
        table.pack(fill="both", expand=True)
        for row in range(6):
            table.insert("", "end", values=(row,))
        root.update()
        self.assertEqual(table.winfo_height(), table.winfo_reqheight())
        self.assertGreater(scroll.body.winfo_height(), scroll.canvas.winfo_height())

        before = scroll.canvas.yview()
        scroll.wheel(mock.Mock(widget=table, delta=-120))
        self.assertEqual(scroll.canvas.yview(), before)
        scroll.wheel(mock.Mock(widget=scroll.canvas, delta=-120))
        self.assertNotEqual(scroll.canvas.yview(), before)

        cards.configure(height=20)  # short content: the table stretches to the visible height
        root.update()
        self.assertEqual(scroll.body.winfo_height(), scroll.canvas.winfo_height())
        self.assertGreater(table.winfo_height(), table.winfo_reqheight())

    def test_plain_scroll_frame_keeps_its_natural_height(self):
        import tkinter as tk
        import router_window
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        scroll = router_window.ScrollFrame(root)
        tk.Frame(scroll.body, height=40).pack()
        root.update()
        self.assertEqual(scroll.canvas.itemcget(scroll.window, "height"), "0")

    def test_quota_columns_and_masonry_placements(self):
        import router_window

        self.assertEqual(router_window.quota_columns(650, count=10, step=200, hysteresis=8), 3)
        self.assertEqual(router_window.quota_columns(820, count=10, step=200, hysteresis=8), 4)
        self.assertEqual(router_window.quota_columns(1060, count=10, step=200, hysteresis=8), 5)
        # Hysteresis prevents column oscillation around the boundary (e.g. 800px between 3 and 4 cols).
        self.assertEqual(router_window.quota_columns(795, count=10, current=4, step=200, hysteresis=8), 4)
        self.assertEqual(router_window.quota_columns(805, count=10, current=3, step=200, hysteresis=8), 3)

        cards = [
            {"account": "claude", "windows": [{"id": "w1"}, {"id": "w2"}, {"id": "w3"}]},
            {"account": "claude-lab", "windows": [{"id": "w1"}, {"id": "w2"}]},
            {"account": "codex", "windows": [{"id": "w1"}]},
            {"account": "antigravity", "windows": [{"id": "w1"}, {"id": "w2"}, {"id": "w3"}, {"id": "w4"}]},
            {"account": "opencode", "windows": []},
            {"account": "grok", "windows": [{"id": "w1"}]},
        ]
        placements = router_window.quota_card_placements(cards, 4)
        # First 4 metered cards occupy top of cols 0..3; 5th metered card (grok) goes under shortest col (codex, col 2).
        self.assertEqual([placements[i][:2] for i in range(4)], [(0, 0), (0, 1), (0, 2), (0, 3)])
        self.assertEqual(placements[5], (5, 2, 5))
        # Unmetered opencode follows metered cards into the shortest remaining column (claude-lab, col 1).
        self.assertEqual(placements[4], (7, 1, 3))

    @desktop_test
    def test_quota_page_regrids_without_rebuilding_and_keeps_worker_table_visible(self):
        import sqlite3
        import router_window
        import tk_test_support

        self.assertEqual(router_window.short_countdown("2天1小时"), "2d 1h")
        self.assertEqual(router_window.short_countdown("3小时05分"), "3h 5m")
        self.assertEqual(router_window.short_window_name({"id": "seven_day", "hours": 168}), "7d")
        self.assertEqual(router_window.short_window_name({"id": "five_hour", "hours": 5}), "5h")
        self.assertEqual(router_window.short_window_name({"id": "gemini-weekly", "hours": 168}), "gemini 7d")

        # Verify pool_reservations aggregates active dispatch leases per (account, window_id).
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dispatch.sqlite3"
            future = (router_data.now_utc() + router_data.timedelta(hours=2)).isoformat()
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE dispatches (data TEXT)")
            conn.execute("INSERT INTO dispatches VALUES (?)", (json.dumps({
                "dispatch_id": "d-12345678",
                "agent_id": "a1b2c3d4-9999",
                "state": "started",
                "lease_until": future,
                "expected_usd": 20.0,
                "bundle": {"account": "claude", "bundle_id": "opus55_high", "model": "claude-opus-5-5"},
                "request": {"title": "in-flight task"},
            }),))
            conn.commit()
            conn.close()
            # The repo definition, not this host's accounts: other hosts may lack a claude account.
            res = router_data.pool_reservations(db=db, accounts=allocator.load_accounts(allocator.reference_fleet_path()))
            self.assertIn(("claude", "seven_day"), res)
            self.assertGreater(res[("claude", "seven_day")]["pct"], 0.0)
            self.assertEqual(res[("claude", "seven_day")]["usd"], 20.0)
            self.assertEqual(res[("claude", "seven_day")]["workers"][0]["agent_id"], "a1b2c3d4")

        sample_cards = [
            {"account": "claude", "label": "Claude", "badge": "就绪", "kind": "ok", "plan": "max20",
             "usage_status": "fresh", "windows": [
                 {"id": "seven_day", "hours": 168, "remaining_pct": 80.0, "remaining_usd": 500.0,
                  "reserved_pct": 12.5, "reserved_usd": 78.0,
                  "reserved_workers": [{"dispatch_id": "d1", "agent_id": "a1b2c3d4", "bundle": "opus55_high",
                                        "usd": 78.0, "pct": 12.5, "title": "task 1"}],
                  "rate_3h": 1.2, "rate_24h": None, "reset_in": "2天1小时", "stale": False},
                 {"id": "five_hour", "hours": 5, "remaining_pct": 30.0, "remaining_usd": 50.0,
                  "reserved_pct": 0.0, "reserved_usd": 0.0, "reserved_workers": [],
                  "rate_3h": None, "rate_24h": None, "reset_in": "3小时10分", "stale": False},
             ]},
            {"account": "codex", "label": "Codex", "badge": "就绪", "kind": "ok", "plan": "pro",
             "usage_status": "fresh", "windows": [
                 {"id": "usage", "hours": 168, "remaining_pct": 90.0, "remaining_usd": 300.0,
                  "reserved_pct": 0.0, "reserved_usd": 0.0, "reserved_workers": [],
                  "rate_3h": None, "rate_24h": None, "reset_in": "5天2小时", "stale": False},
             ]},
            {"account": "opencode", "label": "OpenCode", "badge": "就绪", "kind": "ok", "plan": "free",
             "usage_status": None, "windows": []},
            {"account": "grok", "label": "Grok", "badge": "就绪", "kind": "ok", "plan": "subscription",
             "usage_status": "stale", "windows": [
                 {"id": "credits", "hours": 168, "remaining_pct": 10.0, "remaining_usd": 20.0,
                  "reserved_pct": 0.0, "reserved_usd": 0.0, "reserved_workers": [],
                  "rate_3h": 0.5, "rate_24h": None, "reset_in": "1天2小时", "stale": True},
             ]},
            {"account": "claude-lab", "label": "Claude 2", "badge": "就绪", "kind": "ok", "plan": "max5",
             "usage_status": "fresh", "windows": [
                 {"id": "seven_day", "hours": 168, "remaining_pct": 55.0, "remaining_usd": 120.0,
                  "reserved_pct": 0.0, "reserved_usd": 0.0, "reserved_workers": [],
                  "rate_3h": None, "rate_24h": None, "reset_in": "4小时10分", "stale": False},
             ]},
        ]
        root = tk_test_support.shared_tk_root(self)
        root.geometry("+-3000+-3000")
        root.deiconify()
        with mock.patch.object(router_window.Panel, "run"), \
                mock.patch.object(router_window.Panel, "cached_account_cards", return_value=sample_cards), \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(router_window, "log_error") as errors:
            panel = router_window.Panel(root, "quota")
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            root.update()
            # 4 metered accounts become 4 account cards; unmetered opencode is in the compact bottom strip.
            self.assertEqual(len(panel.quota_cards), 4)
            self.assertEqual(len(panel.quota_unmetered_cards), 1)
            outers_before = [w["outer"] for w in panel.quota_cards]
            # Short 1-window cards (Codex) are top-aligned (sticky='new'), not stretched to taller multi-window cards (Claude).
            self.assertLess(panel.quota_cards[1]["outer"].winfo_height(),
                            panel.quota_cards[0]["outer"].winfo_height())
            self.assertEqual(panel.quota_cards[0]["name"].cget("text"), "Claude")
            w0_name, w0_share, w0_bar, w0_detail = panel.quota_cards[0]["windows"][0]
            w1_name, w1_share, w1_bar, w1_detail = panel.quota_cards[0]["windows"][1]
            self.assertEqual((w0_name.cget("text"), w0_detail.cget("text"), w0_share.cget("text")), ("7d", "2d 1h", "80%"))
            self.assertEqual((w1_name.cget("text"), w1_detail.cget("text"), w1_share.cget("text")), ("5h", "3h 10m", "30%"))
            self.assertEqual(panel.quota_cards[2]["windows"][0][0].cget("text"), "credits")
            # Segmented bar splits 100% into used (20%), reserved (12.5%), and free remaining (67.5%).
            self.assertEqual(w0_bar.segments, {"used_pct": 20.0, "reserved_pct": 12.5, "remaining_pct": 67.5})
            # Secondary details ($, burn rate, plan, in-flight workers) live in the hover tooltip.
            self.assertIn("max20", panel.quota_cards[0]["tooltip_text"])
            self.assertIn("≈$500", panel.quota_cards[0]["tooltip_text"])
            self.assertIn("1.20%/h", panel.quota_cards[0]["tooltip_text"])
            self.assertIn("a1b2c3d4", panel.quota_cards[0]["tooltip_text"])
            self.assertIn("opus55_high", panel.quota_cards[0]["tooltip_text"])
            self.assertEqual(panel.quota_unmetered_cards[0]["name"].cget("text"), "OpenCode")
            # Worker table is visible without scrolling and shows at least 4 rows of height.
            self.assertGreaterEqual(panel.worker_table.winfo_height(), panel.worker_table.winfo_reqheight())
            # Refreshing with identical shape updates text in-place without destroying widgets.
            updated = [{**c, "plan": "updated"} if i == 0 else c for i, c in enumerate(sample_cards)]
            panel.quota_cards_loaded(updated)
            self.assertEqual([w["outer"] for w in panel.quota_cards], outers_before)
            self.assertIn("updated", panel.quota_cards[0]["tooltip_text"])
            # Resizing the window changes column count via <Configure> and re-grids without recreating widgets.
            root.geometry(f"{router_window.theme.px(880)}x{router_window.theme.px(560)}+-3000+-3000")
            root.update()
            self.assertEqual(panel.quota_cols, 3)
            self.assertEqual([w["outer"] for w in panel.quota_cards], outers_before)
            errors.assert_not_called()


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "route-agent-work" / "scripts"
SECRET = "test-key-0000000000000000"


class ScorerConfigTests(unittest.TestCase):
    """The settings card's System-One scorer config: read (masked), save (placeholder keeps the key), test, simulate."""

    def setUp(self):
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import allocator
        import route_judge

        self.allocator, self.router = allocator, route_judge
        self.temp = tempfile.TemporaryDirectory(prefix="tray-scorer-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scorer-config.json"
        for patcher in (mock.patch.object(router_data, "modules", return_value=(allocator, route_judge)),
                        mock.patch.dict(route_judge.os.environ, {"ROUTER_SCORER_CONFIG": str(self.path),
                                                                 "ROUTER_SCORER_TYPE": ""}),
                        mock.patch.object(route_judge, "user_env", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_types_match_route_judge(self):
        self.assertEqual([row[0] for row in router_data.SCORER_TYPES], list(self.router.SCORER_TYPES))
        self.assertEqual(router_data.SCORER_TYPES[0][0], "agy_flash_low")
        self.assertEqual({row[0] for row in router_data.SCORER_TYPES if row[2]}, {"jev", "openai_compatible"})

    def test_read_and_save_keep_masked_keys(self):
        config = router_data.read_scorer_config()
        self.assertEqual((config["scorer_type"], config["fields"]["jev"]["base_url"], config["fields"]["jev"]["model"],
                          config["fields"]["jev"]["key_hint"]), ("agy_flash_low", "https://api.typesafe.ai", "jev-latest", "尚未配置 API Key"))
        self.assertIn("Jev", router_data.save_scorer_config("jev", SECRET, "https://api.typesafe.ai", "jev-latest"))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored, {"scorer_type": "jev", "jev_api_key": SECRET})
        config = router_data.read_scorer_config()
        masked = config["fields"]["jev"]["api_key_masked"]
        self.assertEqual((config["scorer_type"], masked, config["fields"]["jev"]["key_source"]), ("jev", "test…0000", "file"))
        self.assertNotIn(SECRET, json.dumps(config, ensure_ascii=False))
        # Saving the untouched placeholder (or None) keeps the key; a new value replaces it; '' clears it
        router_data.save_scorer_config("jev", masked, "", "")
        router_data.save_scorer_config("jev", None, "https://jev.example.com", "jev-2")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((stored["jev_api_key"], stored["jev_base_url"], stored["jev_model"]), (SECRET, "https://jev.example.com", "jev-2"))
        router_data.save_scorer_config("jev", "ts-new-key-99998888", "https://api.typesafe.ai", "jev-latest")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((stored["jev_api_key"], "jev_base_url" in stored), ("ts-new-key-99998888", False))
        router_data.save_scorer_config("openai_compatible", "sk-custom-1234567890", "https://llm.example.com/v1", "qwen3-4b")
        router_data.save_scorer_config("heuristic")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((stored["scorer_type"], stored["jev_api_key"], stored["api_key"], stored["model"]),
                         ("heuristic", "ts-new-key-99998888", "sk-custom-1234567890", "qwen3-4b"))
        router_data.save_scorer_config("jev", "", "", "")
        self.assertNotIn("jev_api_key", json.loads(self.path.read_text(encoding="utf-8")))
        with self.assertRaises(ValueError):
            router_data.save_scorer_config("openai_compatible", "k", "", "")
        with self.assertRaises(ValueError):
            router_data.save_scorer_config("nope")
        with mock.patch.object(self.router, "user_env", side_effect=lambda n: "env-key-123456789" if n == "TYPESAFE_API_KEY" else None):
            field = router_data.read_scorer_config()["fields"]["jev"]
        self.assertEqual((field["env_key"], field["key_hint"], field["api_key_masked"]), (True, "使用环境变量 TYPESAFE_API_KEY", ""))

    def test_test_scorer_reports_success_and_clear_failures(self):
        router_data.save_scorer_config("jev", SECRET)
        response = {"model": "jev-latest", "answers": {
            "work_kind": {"choice": "general_implementation"}, "interaction_style": {"choice": "engineering"},
            "reasoning_depth": {"score": 1.2}, "code_complexity": {"score": 2.1}, "context_scale": {"score": 0.1},
            "divergence": {"score": 0.0}, "hallucination_risk": {"score": 0.4}, "error_cost": {"score": 1.0},
            "needs_visual": {"noul": 0.02}, "needs_gui": {"noul": 0.01}, "local_only": {"noul": 0.9}}}
        seen = []
        with mock.patch.object(self.router, "post_json", side_effect=lambda url, body, key, timeout: seen.append(key) or response):
            message = router_data.test_scorer_config()
            router_data.test_scorer_config("jev", "unsaved-key-000011112222")
        self.assertRegex(message, r"^测试成功（Jev · \d+ms）：general_implementation · D2 C2 S1$")
        self.assertEqual(seen, [SECRET, "unsaved-key-000011112222"])
        router_data.save_scorer_config("jev", "")
        with mock.patch.object(self.router, "post_json", side_effect=AssertionError("no call without a key")), \
                mock.patch.object(self.router, "scorer_prefix", side_effect=AssertionError("no agy fallback in a test")):
            message = router_data.test_scorer_config()
        self.assertTrue(message.startswith("测试失败（Jev"), message)
        self.assertIn("TypeSafe API key not configured", message)
        self.assertRegex(router_data.test_scorer_config("heuristic"), r"^测试成功（规则直判")

    def test_simulate_can_profile_with_the_live_scorer(self):
        router_data.save_scorer_config("heuristic")
        with mock.patch.object(router_data, "plan_task", side_effect=lambda task: {"task": task}):
            fast = router_data.simulate("修复解析器的 bug", 1.0)
            live = router_data.simulate("修复解析器的 bug", 1.0, live_scorer=True)
        self.assertNotIn("scorer", fast)
        self.assertEqual(live["scorer"]["source"], "heuristic_configured")
        self.assertEqual(live["task"]["profile"], fast["task"]["profile"])
        profile = dict(fast["task"]["profile"], work_kind="debug_review", reasoning_depth=3)
        with mock.patch.object(router_data, "plan_task", side_effect=lambda task: {"task": task}), \
                mock.patch.object(self.router, "score_task", return_value=(profile, {"source": "system_one", "model": "m"})):
            live = router_data.simulate("修复解析器的 bug", 1.0, live_scorer=True)
        self.assertEqual((live["task"]["work_kind"], live["task"]["difficulty"], live["scorer"]["model"]),
                         ("debug_review", "coupled", "m"))


if __name__ == "__main__":
    unittest.main()
