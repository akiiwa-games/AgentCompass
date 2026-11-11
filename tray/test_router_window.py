import json
import math
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))

import allocator
from tk_test_support import desktop_test, require_tk, import_theatre_without_user_diagnostics
require_tk()
decision_theatre = import_theatre_without_user_diagnostics()
import radar_chart
import router_data
from router_test_support import profile

CATALOG_JSON = Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "references" / "accounts.json"


class ScoreDimensionTests(unittest.TestCase):
    def test_four_categories_and_the_economy_axis(self):
        values = radar_chart.score_dimensions({"share_divergence": 0.3, "share_coding": 0.7, "share_aesthetics": 0.0,
                                               "share_tool_operation": 0.2, "opportunity_cost": 0.2, "boost": 0.05})
        self.assertEqual((values["divergence"], values["coding"], values["aesthetics"],
                          values["tool_operation"]), (0.3, 0.7, 0.0, 0.2))
        self.assertAlmostEqual(values["economy"], 1.0 - 0.2 + 0.05)

    def test_everything_is_clamped_and_a_missing_category_is_zero(self):
        self.assertEqual(radar_chart.score_dimensions({})["economy"], 1.0)
        self.assertEqual(radar_chart.score_dimensions({})["divergence"], 0.0)
        self.assertEqual(radar_chart.score_dimensions({"opportunity_cost": 2})["economy"], 0)
        self.assertEqual(radar_chart.score_dimensions({"boost": 2})["economy"], 1)
        self.assertEqual(radar_chart.clamp(float("nan")), 0.0)
        self.assertEqual(radar_chart.clamp("x"), 0.0)

    def test_raw_terms_for_the_hover_text(self):
        raw = radar_chart.score_raw({"tool_operation": -0.5, "share_tool_operation": 0.8,
                                     "opportunity_cost": 0.08, "boost": 0.35, "fit": 1.0})
        self.assertEqual(raw["tool_operation"], {"tool_operation": -0.5, "share_tool_operation": 0.8})
        self.assertEqual(raw["divergence"], {})
        self.assertEqual(raw["economy"], {"opportunity_cost": 0.08, "boost": 0.35})


class DnaDimensionTests(unittest.TestCase):
    def test_model_table_carries_cited_prices_only_and_the_economy_is_relative_to_the_dearest(self):
        from types import SimpleNamespace
        models = {'priced': {'usd_per_hour': 15}, 'free': {'usd_per_hour': 0}, 'dear': {'usd_per_hour': 30}, 'unpriced': {}}
        fake = SimpleNamespace(REFERENCES=Path('unused'), load_accounts=lambda _: {'models': models},
                               five_axis_overlay=lambda accounts: accounts,
                               models_table=lambda accounts: accounts['models'])
        with tempfile.TemporaryDirectory() as directory,                 mock.patch.object(router_data, 'modules', return_value=(fake, None)),                 mock.patch.object(router_data, 'STATE_DIR', Path(directory)):
            table = router_data.model_table()
            series, _ = router_data.dna_series(table, 'priced', 'unpriced')
        self.assertNotIn('usd_per_hour', table['unpriced'])
        self.assertEqual([s['values']['economy'] for s in series], [.5, 0])
        self.assertEqual([radar_chart.dna_dimensions(table[name], 30)['economy'] for name in models], [.5, 1, 0, 0])

    def test_model_dna_from_category_shares_and_price(self):
        info = {"five_axis": {"scores": {"divergence": -1.2, "coding": -0.1, "aesthetics": -0.3, "tool_operation": -1.0},
                              "shares": {"divergence": 0.62, "coding": 0.98, "aesthetics": 0.92, "tool_operation": 0.55},
                              "tier": {"value": 3.2}}, "usd_per_hour": 15}
        self.assertEqual(radar_chart.dna_dimensions(info, 30), {
            "divergence": 0.62, "coding": 0.98, "aesthetics": 0.92, "tool_operation": 0.55, "economy": .5})
        raw = radar_chart.dna_raw(info)
        self.assertEqual(raw["tool_operation"], {"tool_operation": -1.0, "share_tool_operation": .55, "tier": 3.2})
        self.assertEqual(raw["economy"], {"usd_per_hour": 15})

    def test_a_model_without_data_has_zero_everywhere_never_a_neutral_default(self):
        self.assertEqual(set(radar_chart.dna_dimensions({}, 30).values()), {0.0})
        self.assertEqual(radar_chart.dna_dimensions({"usd_per_hour": 15}, None)["economy"], 0.0)

    def test_estimated_categories_carry_their_provenance(self):
        estimates = {"divergence.creative_writing": {"method": "sibling_shift", "base_model": "claude-opus-5-5",
                                                     "base_value": 0.0, "shift": -0.2, "tier": 4.1, "base_tier": 4.6},
                     "tool_operation.image_recognition": {"method": "tier_fit", "tier": 3.0, "r": 0.8, "n": 20}}
        notes = radar_chart.dna_estimated({"five_axis": {"estimates": estimates}})
        self.assertEqual(set(notes), {"divergence", "tool_operation"})
        self.assertIn("claude-opus-5-5", notes["divergence"])
        self.assertIn("fit on tier 3.0", notes["tool_operation"])
        self.assertEqual(radar_chart.dna_estimated({"five_axis": {"estimates": {}}}), {})

    def test_axis_keys_are_the_four_categories_and_the_economy(self):
        expected = ["divergence", "coding", "aesthetics", "tool_operation", "economy"]
        self.assertEqual([key for key, _, _ in radar_chart.SCORE_AXES], expected)
        self.assertEqual([key for key, _, _ in radar_chart.DNA_AXES], expected)
        for result in (radar_chart.score_dimensions({}), radar_chart.score_raw({}),
                       radar_chart.dna_dimensions({}), radar_chart.dna_raw({})):
            self.assertEqual(list(result), expected)

    def test_real_model_table_values_stay_in_range(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(router_data, 'STATE_DIR', Path(directory)):
            models = router_data.model_table()
        self.assertTrue(models)
        ceiling = max((m["usd_per_hour"] for m in models.values() if m.get("usd_per_hour") is not None), default=None)
        for name, info in models.items():
            for value in radar_chart.dna_dimensions(info, ceiling).values():
                self.assertTrue(0 <= value <= 1, name)
        self.assertTrue(any(radar_chart.dna_dimensions(info, ceiling)["divergence"] > 0 for info in models.values()))


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
            "excluded": list(excluded), "components": {"fit": 0.8, "divergence": 0.0, "coding": 0.0, "aesthetics": 0.0,
                                                       "tool_operation": 0.0, "share_divergence": 0.8, "share_coding": 0.7,
                                                       "share_aesthetics": 0.6, "share_tool_operation": 0.5,
                                                       "opportunity_cost": 0.01, **components}}


PLAN = {"choice": "opus", "decision": {"margin": 0.05},
        "ranking": [candidate("opus", "claude-opus-5-5", 0.5, boost=0.2), candidate("opus_x", "claude-opus-5-5", 0.48),
                    candidate("sol", "gpt-6.1-sol", 0.45, boost=0.1),
                    candidate("flash", "gemini-3.8-flash-high", None, excluded=["below ability floor"])]}


class DecisionBindingTests(unittest.TestCase):
    def test_theatre_and_static_strip_integrate_without_showing_a_window(self):
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, 'run'), mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'):
            panel = router_window.Panel(root, 'scoring')
            panel.plan_loaded(PLAN)
            self.assertEqual(panel.theatre.scene.winner.id,'opus')
            self.assertTrue(panel.verdict_strip.canvas.grid_info())
            self.assertIn('Opus 5.5',panel.verdict.get())
            panel.theatre_enabled.set(False)
            panel.toggle_theatre()
            self.assertEqual(panel.theatre.elapsed,decision_theatre.DURATION)
            panel.render_radar()
            self.assertTrue(panel.radar.canvas.pack_info())
            self.assertEqual(panel.theatre.canvas.winfo_manager(), "")

    def test_a_quota_record_the_view_cannot_read_does_not_stop_the_decision_from_loading(self):
        import router_window
        import panel_scoring
        import panel_widgets
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        window = {"label": "Claude", "capacity_usd": "n/a", "used_usd": "?", "elapsed_hours": 166, "reset_in_hours": 2}
        ranking = [{**row, "quota_line": {"window": "claude", "hours": "two", "spend_usd": "?"}} for row in PLAN["ranking"][:3]]
        loaded = lambda panel: (panel.theatre.scene.winner.id, bool(panel.breakdown_rows), 'Opus 5.5' in panel.verdict.get())
        with mock.patch.object(router_window.Panel, 'run'), mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'):
            panel = router_window.Panel(root, 'scoring')
            # Fields of another type are read one by one: the window is left out and nothing is raised.
            with mock.patch.object(panel_widgets, "log_error") as logged:
                panel.plan_loaded({**PLAN, "ranking": ranking, "quota_line": {"windows": {"claude": window}}})
            logged.assert_not_called()
            self.assertEqual((panel.quota_line.data["windows"], panel.quota_line.data["dropped"]), ([], ["claude"]))
            self.assertEqual(loaded(panel), ('opus', True, True))
            # Whatever else goes wrong inside the quota view stays there: it is logged, and the film, the
            # candidates and the verdict of the decision load as usual.
            panel.breakdown_rows = []
            with mock.patch.object(panel_scoring.quota_line, "model", side_effect=ValueError("boom")), \
                    mock.patch.object(panel_widgets, "log_error") as logged:
                panel.plan_loaded({**PLAN, "quota_line": {"windows": {"claude": {**window, "used_usd": 240}}}})
            logged.assert_called_once()
            self.assertIsInstance(panel.quota_line.error, ValueError)
            self.assertIsNone(panel.quota_line.data)
            self.assertEqual(loaded(panel), ('opus', True, True))
            panel.plan_loaded(PLAN)
            self.assertIsNone(panel.quota_line.error)

    def test_quota_line_tab_shows_the_loaded_decision_and_follows_the_film(self):
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        window = {"label": "Claude", "capacity_usd": 400, "used_usd": 240, "elapsed_hours": 166, "reset_in_hours": 2}
        ranking = [{**row, "quota_line": {"window": "claude", "hours": 2, "spend_usd": 40, "over_line_usd": 0}}
                   for row in PLAN["ranking"][:3]]
        with mock.patch.object(router_window.Panel, 'run'), mock.patch.object(router_window.Panel, 'prefill'),                 mock.patch.object(router_window.theme, 'set_window_icon'):
            panel = router_window.Panel(root, 'scoring')
            panel.plan_loaded(PLAN)
            # A decision recorded without quota-line fields shows the empty state, never invented numbers.
            self.assertIsNone(panel.quota_line.data)
            self.assertEqual(panel.theatre.on_frame, panel.quota_line.follow)
            panel.show_list("quota")
            self.assertTrue(panel.quota_frame.pack_info())
            self.assertEqual(panel.ledger_frame.winfo_manager(), "")
            self.assertEqual(panel.list_switch.value, "quota")
            panel.plan_loaded({**PLAN, "ranking": ranking, "quota_line": {"windows": {"claude": window}}})
            self.assertEqual([item["key"] for item in panel.quota_line.data["windows"]], ["claude"])
            self.assertEqual(panel.quota_line.data["winner"]["window"], "claude")
            # The film's clock reaches the view on every frame it draws, and stopping the film settles it.
            panel.theatre.elapsed = 3.0
            panel.theatre.draw()
            self.assertEqual(panel.quota_line.story, decision_theatre.story_time(3.0))
            panel.render_radar()
            self.assertEqual(panel.quota_line.story, decision_theatre.DURATION)
            # Hover: the mark under the pointer in words, else how to read the picture. A click replays.
            for sequence in ("<Motion>", "<Leave>", "<Button-1>"):
                self.assertTrue(panel.quota_line.canvas.bind(sequence))
            bound, tips = {}, iter(["", "Claude: $240", "Claude: $240", ""])
            view = SimpleNamespace(canvas=SimpleNamespace(bind=lambda sequence, handler, add=None: bound.update({sequence: handler})),
                                   tip_at=lambda x, y: next(tips))
            shown = []
            with mock.patch.object(panel, "attach_tooltip", return_value=(lambda event: shown.append(view.canvas._tooltip_text),
                                                                          lambda: shown.append(None))):
                panel.attach_quota_tooltip(view, "legend")
            for _ in range(4):
                bound["<Motion>"](SimpleNamespace(x=5, y=5, x_root=5, y_root=5))
            # Shown again only when the text under the pointer changes.
            self.assertEqual([text for text in shown if text], ["legend", "Claude: $240", "legend"])
            with mock.patch.object(panel, "replay_theatre") as replay:
                bound["<Button-1>"](None)
                replay.assert_called_once_with()
            panel.show_list("ledger")
            self.assertEqual(panel.quota_frame.winfo_manager(), "")

    def test_static_radar_shows_the_pair_s_values_and_nothing_about_the_task(self):
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        fit = {"divergence": {"demand": 1, "score": -.7, "target": -3.1}, "coding": {"demand": 3, "score": 0.0, "target": 0.0}}
        ranking = [{**row, "five_axis_fit": fit} if row["id"] == "opus" else row for row in PLAN["ranking"]]
        with mock.patch.object(router_window.Panel, 'run'), mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'):
            panel = router_window.Panel(root, 'scoring')
            panel.plan_loaded({**PLAN, "ranking": ranking})
            with mock.patch.object(panel.radar, "show") as show:
                panel.render_radar()
            # The axes, the two series and their raw values for the hover text: no need, no cost.
            (axes, series, raw), keywords = show.call_args
            self.assertEqual(keywords, {})
            self.assertEqual(len(series), 2)
            self.assertFalse(hasattr(panel.radar, "need") or hasattr(panel.radar, "cost_axis"))

    def test_opening_decision_skips_a_newest_row_that_replays_to_none(self):
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, 'run'), mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'):
            panel = router_window.Panel(root, 'scoring')
        none = {"choice": None, "ranking": PLAN["ranking"]}
        rows = [{"id": "empty"}, {"id": "real"}]
        summary = {"at": "t", "kind_label": "k", "choice_label": "c", "objective": "o"}
        with mock.patch.object(router_data, 'replay', side_effect=lambda row: none if row["id"] == "empty" else PLAN), \
                mock.patch.object(router_data, 'ledger_summary', return_value=summary):
            panel.ledger_loaded(rows)
        self.assertEqual(panel.ledger_table.selection(), ("1",))
        self.assertIs(panel.plan_result, PLAN)
        self.assertEqual(panel.theatre.scene.winner.id, 'opus')
        self.assertTrue(panel._suppress_next_select)

    def test_breakdown_rows_carry_fit_economy_boost_and_hard_gate_reasons(self):
        rows = {r["id"]: r for r in router_data.candidate_rows(PLAN)}
        self.assertEqual(rows["opus"]["boost"], 0.2)
        self.assertEqual(rows["sol"]["boost"], 0.1)
        self.assertEqual(rows["sol"]["opportunity_cost"], 0.01)
        self.assertNotIn("penalty", rows["sol"])
        self.assertEqual(rows["flash"]["excluded"], ["below ability floor"])

    def test_default_pair_is_winner_and_best_other_model(self):
        pair = router_data.decision_pair(PLAN)
        self.assertEqual([r["id"] for r in pair], ["opus", "sol"])
        series, raw = router_data.decision_series(pair)
        self.assertEqual(series[0]["label"], f"Opus 5.5 · {router_data.display_names.effort_text('high')} · 0.500")
        self.assertEqual(set(series[0]["values"]), {key for key, _, _ in radar_chart.SCORE_AXES})
        self.assertEqual(raw[1]["economy"]["boost"], 0.1)

    def test_a_picked_pair_wins_and_excluded_candidates_cannot_be_picked(self):
        self.assertEqual([r["id"] for r in router_data.decision_pair(PLAN, "sol", "opus_x")], ["sol", "opus_x"])
        self.assertEqual([r["id"] for r in router_data.decision_pair(PLAN, "flash")], ["opus", "sol"])
        self.assertEqual(router_data.decision_pair({"ranking": []}), [])

    def test_dna_series_for_two_models(self):
        models = {"a": {"five_axis": {"shares": {"divergence": 1}}, "usd_per_hour": 0}, "b": {}}
        series, raw = router_data.dna_series(models, "a", "b")
        self.assertEqual([s["label"] for s in series], ["a", "b"])
        self.assertEqual(series[0]["values"]["divergence"], 1.0)
        self.assertEqual(len(router_data.dna_series(models, "a", "missing")[0]), 1)

    def test_dry_run_binds_the_profile_and_never_writes_the_ledger(self):
        allocator = mock.Mock()
        allocator.load_accounts.return_value = {"scoring": {}}
        allocator.dot_may_take.return_value = True
        allocator.plan.return_value = PLAN
        router = mock.Mock()
        router.score_task.return_value = ({"reasoning_depth": 2}, {"source": "system_one", "model": "jev-latest"})
        router.read_ledger.return_value = []
        router.dot_unreachable.return_value = "refused"
        with mock.patch.object(router_data, "modules", return_value=(allocator, router)):
            result = router_data.simulate("修复竞态", 1.5, local_only=True, needs_visual=False)
        self.assertEqual({k: v for k, v in result.items() if k not in ("task", "scorer")}, PLAN)
        self.assertEqual(result["task"]["objective"], "修复竞态")
        self.assertNotIn("task", PLAN)  # adding replay metadata must not mutate allocator output
        task = allocator.plan.call_args.args[0]
        self.assertEqual(task["expected_hours"], 1.5)
        self.assertTrue(task["needs"]["local_only"])
        self.assertTrue(task["needs"]["local_only_reason"])
        self.assertEqual(task["profile"], {"reasoning_depth": 2})
        self.assertEqual(allocator.plan.call_args.args[3], ["dot_text"])
        allocator.infer_task_profile.assert_not_called()
        router.append_ledger.assert_not_called()

    def test_replay_reuses_the_recorded_profile_when_valid(self):
        allocator = mock.Mock()
        allocator.ledger_profile.side_effect = lambda p: p
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
                                              "kind": "tool_execution", "choice": "dot_text", "margin": 0.2,
                                              "profile": profile()})
        self.assertEqual(summary["objective"], "Title body")
        self.assertEqual(summary["choice"], "dot_text")

    def test_verdict_summary_is_structured_rows_with_the_reason_kept_for_the_tooltip(self):
        import i18n
        import panel_scoring

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
        rows, tooltip = panel_scoring.verdict_summary(plan, router_data.decision_pair(plan))
        self.assertEqual(rows, [("胜出", "Opus 5.5 · 高  0.500"), ("次选", "Sol 6.1 · 高  0.450 · 领先 0.0500"),
                                ("任务", "调试评审 · 困难 · 4h"),
                                ("额度", "claude-weekly 剩 40% · 本单 ~3.5% · 机会成本 0.010")])
        self.assertEqual(tooltip, reason)
        i18n.set_language("en")
        rows, _ = panel_scoring.verdict_summary(plan, router_data.decision_pair(plan))
        self.assertEqual(rows[1], ("Runner-up", "Sol 6.1 · High  0.450 · lead 0.0500"))
        self.assertEqual(rows[2], ("Task", "Debug & review · Hard · 4h"))
        self.assertEqual(rows[3][1], "claude-weekly 40% left · this task ~3.5% · opportunity cost 0.010")

    def test_verdict_summary_marks_ties_and_falls_back_to_a_short_reason(self):
        import i18n
        import panel_scoring

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(router_data, "STATE_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        i18n.set_language("en")
        plan = {"choice": "a", "decision": {"reason": "a (start) scored 0.3; free usage window", "task": {}},
                "ranking": [candidate("a", "m1", 0.3), candidate("b", "m2", 0.3)]}
        rows, _ = panel_scoring.verdict_summary(plan, router_data.decision_pair(plan))
        self.assertEqual(rows[1], ("Runner-up", "m2 · High  0.300 · tied"))
        self.assertEqual(rows[2], ("Task", "— · — · —"))
        self.assertEqual(rows[3], ("Quota", "a (start) scored 0.3"))
        self.assertEqual(panel_scoring.verdict_summary({"decision": {}}, []), ([("", "No eligible candidates")], ""))


class AccountCardTests(unittest.TestCase):
    def cards(self, window):
        allocator = mock.Mock()
        allocator.load_accounts.return_value = {"accounts": {"claude-lab": {"windows": {"five_hour": {"hours": 5}}}}}
        allocator.canonical_window_id.side_effect = lambda spec, wid: wid
        allocator.capacity_usd.return_value = None
        allocator.host_policy.load_policy.return_value = None
        router = mock.Mock()
        router.usage_snapshot.return_value = {"accounts": {"claude-lab": {"status": "fresh", "windows": [window]}}}
        inventory = mock.Mock()
        inventory.current.return_value = {"accounts": {}}
        inventory.quota_sources.return_value = {}
        with tempfile.TemporaryDirectory() as directory,                 mock.patch.object(router_data, "modules", return_value=(allocator, router)),                 mock.patch.dict(sys.modules, {"provider_inventory": inventory}),                 mock.patch.object(router_data, "STATE_DIR", Path(directory)),                 mock.patch.object(router_data.plugins, "load", return_value={}),                 mock.patch.object(router_data, "account_badge", return_value=("", "")):
            return router_data.account_cards()[0]["windows"][0]

    def test_a_window_past_its_reset_waits_for_a_new_reading(self):
        window = self.cards({"id": "five_hour", "used_pct": 100, "reset_time_passed": True,
                             "seconds_until_reset": 0, "observed_remaining_pct": 0,
                             "remaining_pct_before_reset": None})
        self.assertEqual((window["remaining_pct"], window["reset_in"], window["stale"]), (None, "已重置", True))

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
        message = router_data.save_tuning({"safety_margin_pct": 7, "short_window_floor_pct": 12}, target)
        self.assertIn("2", message)
        changed = target.read_bytes().splitlines()
        self.assertEqual(sum(a != b for a, b in zip(original.splitlines(), changed)), 2)
        self.assertEqual(router_data.read_tuning(target)["short_window_floor_pct"], 12)
        self.assertEqual(len(list((self.dir / "backups").iterdir())), 1)
        self.assertEqual(router_data.save_tuning({"safety_margin_pct": 7}, target), "参数未变化")
        router_data.restore_tuning(target)
        self.assertEqual(target.read_bytes(), original)

    def test_tuning_rejects_out_of_range_and_unknown_keys(self):
        with self.assertRaises(ValueError):
            router_data.validate_tuning({"safety_margin_pct": 80})
        with self.assertRaises(ValueError):
            router_data.validate_tuning({"no_such_parameter": 0.6})
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
                         'accounts': {'codex-new': {'verified': True, 'billing': 'subscription',
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

    def test_toggle_bundle_writes_to_override(self):
        override = self.dir / "accounts-override.json"
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
        import panel_widgets
        import easytier
        import network_status
        import plugins
        import services
        import ui_theme

        i18n.set_language("en")
        self.assertEqual([p[1] for p in router_window.PAGES],
                         ["Decision Compass", "Quota Monitor", "Model Settings", "Integrations"])
        self.assertEqual(radar_chart.DNA_AXES[0][1], "Divergence")
        self.assertEqual([a[1] for a in radar_chart.DNA_AXES[:4]],
                         ["Divergence", "Coding", "Aesthetics", "Tool Operation"])
        self.assertEqual(radar_chart.axis_title(), "Capabilities & Quota")
        self.assertIn("divergence", radar_chart.SCORE_AXES[0][2])
        for axes in (radar_chart.SCORE_AXES, radar_chart.DNA_AXES):
            for _, label, formula in axes:
                self.assertFalse(any('\u4e00' <= char <= '\u9fff' for char in label + formula))
        self.assertEqual(router_data.PRESETS[0][0], "Cross-module architecture refactor")
        self.assertEqual(router_data.strategy_preset_key("Save Quota"), "省额度")
        self.assertIn("省额度", router_data.STRATEGY_PRESETS)
        self.assertEqual(router_data.countdown(3600), "1h 00m")
        self.assertEqual(panel_widgets.short_badge("Paused: disabled"), "Paused")
        self.assertEqual(panel_widgets.short_badge("Blocked by Host"), "Blocked by Host")
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
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        # No service probes, network calls or workers are needed to test real Tk labels.
        with mock.patch.object(router_window.Panel, "run"), \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(panel_widgets, "log_error") as errors:
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
        import panel_plugins
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        i18n.set_language("en")
        with mock.patch.object(router_window.Panel, "run"), \
                mock.patch.object(router_window.Panel, "prefill"), \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(panel_plugins.plugins, "load", return_value=panel_plugins.plugins.defaults()), \
                mock.patch.object(panel_widgets, "log_error") as errors:
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
            self.assertEqual(panel.radar.series[0]['label'], 'Opus 5.5 · High · 0.500')
            # Short names in the candidates table and pickers; two identical Opus bundles keep their raw ids.
            labels = [panel.breakdown.item(i, 'values')[0] for i in panel.breakdown.get_children()]
            self.assertEqual(labels, ['opus', 'opus_x', 'Sol 6.1 · High', 'Flash 3.8 · High'])
            self.assertIn('Sol 6.1 · High', panel.pick_a.cget('values'))
            self.assertEqual(panel.breakdown_row_tip('2'), 'sol · a')
            self.assertNotIn('ledger', panel.status.get())
            self.assertEqual(panel.radar.axes[0][1], "Divergence")
            self.assertEqual(panel.radar_title.cget("text"), "Capabilities & Quota")
            self.assertFalse(hasattr(panel, "mode_switch"))
            for page in ("quota", "plugins", "policy"):
                panel.show(page)
            for setting, _ in panel_widgets.SETTINGS:
                panel.show_setting(setting)
                if setting == "strategy":
                    self.assertEqual(set(panel.tuning_vars),
                                     {"safety_margin_pct", "short_window_floor_pct", "rotate_after_carried_tokens"})
            panel.show("scoring")
            panel.change_language("zh")
            panel.render_radar()
            self.assertEqual(panel.radar.axes[0][1], "发散创新")
            self.assertEqual(panel.radar_title.cget("text"), "能力与额度五维")
            root.update_idletasks()
            errors.assert_not_called()


    def test_page_switches_cover_pages_and_idle_prebuild_builds_the_rest_underneath(self):
        import router_window
        import panel_plugins
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, "run"),                 mock.patch.object(router_window.Panel, "prefill"),                 mock.patch.object(router_window.theme, "set_window_icon"),                 mock.patch.object(panel_plugins.plugins, "load", return_value=panel_plugins.plugins.defaults()),                 mock.patch.object(panel_widgets, "log_error") as errors:
            panel = router_window.Panel(root)
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            self.assertEqual(panel.built, {"scoring"})
            self.assertFalse(panel.prebuild)  # only main() starts it, once the window is on screen
            with mock.patch.object(panel, "schedule_prebuild") as again:
                for _ in range(10):
                    panel.prebuild_next()
            # One page or setting per idle slot, each laid out under the page on screen; then it stops.
            self.assertEqual(panel.built, {key for key, _, _ in router_window.PAGES})
            self.assertEqual(panel.settings_built, {key for key, _ in panel_widgets.SETTINGS})
            self.assertEqual(again.call_count, 5)
            self.assertEqual(panel.current, "scoring")
            self.assertTrue(all(panel.pages[key]._page_hidden for key in ("quota", "policy", "plugins")))
            panel.show("quota")
            panel.show("scoring")
            # Covered, not unmapped: coming back never re-places and re-shows every native window of the page.
            self.assertEqual(panel.pages["quota"].winfo_manager(), "place")
            self.assertTrue(panel.pages["quota"]._page_hidden)
            panel.show("policy")
            panel.show_setting("strategy")
            self.assertEqual(panel.setting_frames["models"].winfo_manager(), "place")
            self.assertTrue(panel.setting_frames["models"]._page_hidden)
            errors.assert_not_called()

    def test_a_page_built_underneath_reads_its_data_off_the_ui_thread(self):
        # A covered page can't show empty, so its reads (account files, CLI probes, process scans) must not stall
        # the page on screen; the page in front still fills before it shows.
        import router_window
        import panel_plugins
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, "run") as run, \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(panel_plugins.plugins, "load", return_value=panel_plugins.plugins.defaults()), \
                mock.patch.object(panel_widgets, "log_error"):
            with mock.patch.object(router_window.Panel, "prefill"):
                panel = router_window.Panel(root)
            self.addCleanup(lambda: root.after_cancel(panel._poll_id))
            run.reset_mock()
            with mock.patch.object(panel, "cached_account_cards") as read, \
                    mock.patch.object(panel, "schedule_prebuild"):
                panel.prebuild_next()
            self.assertIn("quota", panel.built)
            read.assert_not_called()
            self.assertIn(read, [c.args[0] for c in run.call_args_list])
            filled = []
            panel.prefill(lambda: "rows", filled.append)
            self.assertEqual(filled, ["rows"])


class WindowHelperTests(unittest.TestCase):
    def test_layout_keeps_the_tray_page_keys(self):
        import router_window
        import panel_widgets

        self.assertEqual([p[0] for p in router_window.PAGES], ["scoring", "quota", "policy", "plugins"])
        self.assertEqual([s[0] for s in panel_widgets.SETTINGS], ["models", "strategy", "system"])

    def test_duplicate_bundle_labels_fall_back_to_ids(self):
        import panel_scoring

        bundles = [{"id": "sol_xhigh", "label": "Sol 6.1·xhigh"}, {"id": "sol", "label": "Sol 6.1"},
                   {"id": "work_sol_xhigh", "label": "Sol 6.1·xhigh"}]
        self.assertEqual(panel_scoring.bundle_labels(bundles), ["sol_xhigh", "Sol 6.1", "work_sol_xhigh"])

    def test_matching_preset_marks_the_saved_strategy(self):
        import panel_strategy

        presets = router_data.STRATEGY_PRESETS
        self.assertEqual(panel_strategy.matching_preset(dict(presets["省额度"]), presets), "省额度")
        self.assertIsNone(panel_strategy.matching_preset({**presets["省额度"], "safety_margin_pct": 13}, presets))
        self.assertIsNone(panel_strategy.matching_preset({}, presets))

    def test_badges_and_long_text_are_shortened(self):
        import panel_widgets

        self.assertEqual(panel_widgets.short_badge("已暂停：disabled"), "已暂停")
        self.assertEqual(panel_widgets.clip("abcdef", 4), "abc…")
        self.assertEqual(panel_widgets.clip(None), "—")

    @desktop_test
    def test_filled_scroll_frame_keeps_the_table_rows_visible(self):
        """Tall content above the worker table scrolls instead of squeezing the table to its headings."""
        import tkinter as tk
        from tkinter import ttk
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.geometry("+-3000+-3000")  # mapped (heights are real) but off screen
        root.deiconify()
        holder = tk.Frame(root, width=400, height=300)
        holder.pack_propagate(False)
        holder.pack()
        scroll = panel_widgets.ScrollFrame(holder, fill=True)
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
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        scroll = panel_widgets.ScrollFrame(root)
        tk.Frame(scroll.body, height=40).pack()
        root.update()
        self.assertEqual(scroll.canvas.itemcget(scroll.window, "height"), "0")

    def test_quota_columns_and_masonry_placements(self):
        import panel_quota

        self.assertEqual(panel_quota.quota_columns(650, count=10, step=200, hysteresis=8), 3)
        self.assertEqual(panel_quota.quota_columns(820, count=10, step=200, hysteresis=8), 4)
        self.assertEqual(panel_quota.quota_columns(1060, count=10, step=200, hysteresis=8), 5)
        # Hysteresis prevents column oscillation around the boundary (e.g. 800px between 3 and 4 cols).
        self.assertEqual(panel_quota.quota_columns(795, count=10, current=4, step=200, hysteresis=8), 4)
        self.assertEqual(panel_quota.quota_columns(805, count=10, current=3, step=200, hysteresis=8), 3)

        cards = [
            {"account": "claude", "windows": [{"id": "w1"}, {"id": "w2"}, {"id": "w3"}]},
            {"account": "claude-lab", "windows": [{"id": "w1"}, {"id": "w2"}]},
            {"account": "codex", "windows": [{"id": "w1"}]},
            {"account": "antigravity", "windows": [{"id": "w1"}, {"id": "w2"}, {"id": "w3"}, {"id": "w4"}]},
            {"account": "opencode", "windows": []},
            {"account": "grok", "windows": [{"id": "w1"}]},
        ]
        placements = panel_quota.quota_card_placements(cards, 4)
        # First 4 metered cards occupy top of cols 0..3; 5th metered card (grok) goes under shortest col (codex, col 2).
        self.assertEqual([placements[i][:2] for i in range(4)], [(0, 0), (0, 1), (0, 2), (0, 3)])
        self.assertEqual(placements[5], (5, 2, 5))
        # Unmetered opencode follows metered cards into the shortest remaining column (claude-lab, col 1).
        self.assertEqual(placements[4], (7, 1, 3))

    def test_pool_hover_is_one_row_per_account_with_its_own_reset(self):
        import router_data
        import panel_quota

        week, five = {"id": "seven_day", "hours": 168}, {"id": "five_hour", "hours": 5}
        pool = {"account": "claude-2", "windows": [dict(week, accounts=2), dict(five, accounts=2)]}
        pool["windows"][0].update(remaining_pct=150, reset_in="5天7小时")
        pool["windows"][1].update(remaining_pct=None, reset_in="已重置")
        pool.update(label="Claude 2", plan="pro ×2", kind="ok", usage_status="fresh", pool_members=[
            {"label": "k***@gmail.com", "windows": {"seven_day": {"used_pct": 59}, "five_hour": {"used_pct": 5}}},
            {"label": "a***@proton.me", "windows": {"seven_day": {"used_pct": 0}}, "counted": False}])
        # Usage Float's hover: the table alone (the card shows the pooled windows); exceptions follow it.
        self.assertEqual(panel_quota.quota_tooltip_text(pool).splitlines(), [
            "      7d    5h", "k***  41%   95%", "a***  100% (不计入)"])
        pool["windows"][0]["reserved_pct"] = 12
        pool.update(kind="warn", badge="额度紧张")
        self.assertEqual(panel_quota.quota_tooltip_text(pool).splitlines()[3:], ["[额度紧张]", "7d×2 在途 12%"])

        # A pool counts down to its next partial refill; the hover table gives each account's own reset.
        now = router_data.datetime(2026, 11, 6, 12, 0, tzinfo=router_data.timezone.utc)
        at = lambda hours: (now + router_data.timedelta(hours=hours)).isoformat()
        rows = router_data.pool_member_rows([
            {"label": "k***@gmail.com", "read_at": at(-0.1), "windows": {
                "seven_day": {"used_pct": 80, "resets_at": at(2)}, "five_hour": {"used_pct": 40, "resets_at": at(-0.05)}}},
            {"label": "k***@pm.me", "read_at": at(-3), "windows": {
                "seven_day": {"used_pct": 30, "resets_at": at(100)}, "five_hour": {"used_pct": 2, "resets_at": at(1)}}},
            {"label": "a***@qq.com", "read_at": at(-0.1), "counted": False, "windows": {
                "seven_day": {"used_pct": 90, "resets_at": at(0.5)}}}], now)
        self.assertEqual(rows[0]["windows"]["five_hour"], {"used_pct": 0.0, "refilled": True, "reset_seconds": None})
        self.assertEqual(router_data.pool_next_refill_seconds(rows, "seven_day"), 7200)
        self.assertIsNone(router_data.pool_next_refill_seconds(rows, "five_hour"))  # nothing spent enough to refill
        self.assertEqual(panel_quota.pool_member_lines(rows), [  # same initials: the domains tell them apart
            "                7d        5h",
            "k***@gmail.com  20% 2h    100%",
            "k***@pm.me      70% 4d4h  98% 1h 3小时前读",
            "a***@qq.com     10% 30m (不计入)"])


    @desktop_test
    def test_quota_page_regrids_without_rebuilding_and_keeps_worker_table_visible(self):
        import sqlite3
        import router_window
        import panel_quota
        import panel_widgets
        import tk_test_support

        self.assertEqual(panel_quota.short_countdown("2天1小时"), "2d 1h")
        self.assertEqual(panel_quota.short_countdown("3小时05分"), "3h 5m")
        self.assertEqual(panel_quota.short_window_name({"id": "seven_day", "hours": 168}), "7d")
        self.assertEqual(panel_quota.short_window_name({"id": "five_hour", "hours": 5}), "5h")
        self.assertEqual(panel_quota.short_window_name({"id": "gemini-weekly", "hours": 168}), "gemini 7d")
        name = panel_quota.card_window_name
        week, five, fable = {"id": "seven_day", "hours": 168}, {"id": "five_hour", "hours": 5}, {"id": "weekly_fable", "hours": 168}
        ultra = {"account": "claude", "windows": [fable, week]}
        self.assertEqual([name(ultra, w) for w in ultra["windows"]], ["fable 7d", ""])  # week-only, like Codex
        pro = {"account": "claude", "windows": [week, five]}
        self.assertEqual([name(pro, w) for w in pro["windows"]], ["7d", "5h"])
        pool = {"account": "claude-2", "windows": [dict(week, accounts=2), dict(five, accounts=2)]}
        self.assertEqual([name(pool, w) for w in pool["windows"]], ["7d×2", "5h×2"])
        agy = {"account": "antigravity", "windows": [{"id": "gemini-weekly", "hours": 168}]}
        self.assertEqual(name(agy, agy["windows"][0]), "gemini 7d")

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
                mock.patch.object(panel_widgets, "log_error") as errors:
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
            self.assertEqual((w0_bar.value, w1_bar.value), (80.0, 30.0))
            self.assertEqual((w0_bar.colour, w1_bar.colour), ("Green", "Amber"))
            self.assertEqual(panel.quota_cards[2]["windows"][0][0].cget("text"), "")
            for c in panel.quota_cards:
                self.assertGreaterEqual(c["name"].winfo_width(), c["name"].winfo_reqwidth())
            # Segmented bar splits 100% into used (20%), reserved (12.5%), and free remaining (67.5%).
            self.assertEqual(w0_bar.segments, {"used_pct": 20.0, "reserved_pct": 12.5, "remaining_pct": 67.5})
            # Hover tooltip shows plan, remaining %, countdown, and in-flight reservation %.
            self.assertIn("max20", panel.quota_cards[0]["tooltip_text"])
            self.assertIn("7d 80% · 2d 1h · 在途 12%", panel.quota_cards[0]["tooltip_text"])
            self.assertIn("5h 30% · 3h 10m", panel.quota_cards[0]["tooltip_text"])
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

    def test_quota_bar_and_card_show_remaining_percentage_and_thresholds(self):
        import tkinter as tk
        import router_window
        import panel_quota
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        holder = tk.Frame(root)
        bar = panel_quota.QuotaBar(tk, holder)
        bar.set_segments(80.0, reserved_pct=12.5, colour="Green", stale=False)
        self.assertEqual(bar.value, 80.0)
        self.assertEqual(bar["value"], 80.0)
        self.assertEqual(bar.segments, {"used_pct": 20.0, "reserved_pct": 12.5, "remaining_pct": 67.5})
        rem_items = bar.canvas.find_withtag("seg_remaining")
        self.assertEqual(len(rem_items), 1)
        self.assertEqual(len(bar.canvas.find_withtag("seg_used")), 0)
        coords = bar.canvas.coords(rem_items[0])
        track_coords = bar.canvas.coords(bar.canvas.find_withtag("usage_track")[0])
        self.assertAlmostEqual(coords[2] / track_coords[2], 0.80, places=2)

        # Verify colour thresholds follow remaining percentage (<=10% Red, <=30% Amber, >30% Green)
        for rem, expected_colour, expected_fg in (
            (80.0, "Green", router_window.theme.GREEN),
            (30.0, "Amber", router_window.theme.AMBER),
            (10.0, "Red", router_window.theme.RED),
        ):
            w_name = tk.Label(holder)
            w_share = tk.Label(holder)
            w_detail = tk.Label(holder)
            w_bar = panel_quota.QuotaBar(tk, holder)
            widgets = {"windows": [(w_name, w_share, w_bar, w_detail)], "refreshable": False}
            card = {
                "account": "claude",
                "label": "Claude",
                "kind": "ok",
                "usage_status": "fresh",
                "windows": [{"id": "seven_day", "hours": 168, "remaining_pct": rem, "reset_in": "2天1小时", "stale": False}],
            }
            panel_quota.QuotaPage.fill_quota_card(widgets, card)
            self.assertEqual(w_share.cget("text"), f"{rem:.0f}%")
            self.assertEqual(w_share.cget("fg"), expected_fg)
            self.assertEqual(w_bar.value, rem)
            self.assertEqual(w_bar.colour, expected_colour)

    def test_quota_page_reentering_after_data_update_shows_fresh_readings_and_marks_failed_accounts(self):
        import router_window
        import panel_plugins
        import panel_widgets
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        t1 = "2026-11-08T01:00:00+00:00"
        t2 = "2026-11-08T05:30:00+00:00"
        t3 = "2026-11-08T06:15:00+00:00"
        t_fail_last = "2026-11-07T02:00:00+00:00"

        def make_cards(claude_rem, claude_at, codex_failing=False):
            cards = [{
                "account": "claude",
                "label": "Claude",
                "badge": "就绪",
                "kind": "ok",
                "status": "ready",
                "plan": "max20",
                "fetched_at": claude_at,
                "usage_status": "fresh",
                "usage_age_seconds": 60,
                "refreshable": True,
                "windows": [{
                    "id": "seven_day",
                    "hours": 168,
                    "remaining_pct": claude_rem,
                    "reserved_pct": 0.0,
                    "reset_in": "2天1小时",
                    "stale": False,
                }],
            }]
            if codex_failing:
                cards.append({
                    "account": "codex",
                    "label": "Codex",
                    "badge": f"读数失败：read_failed（上次成功 {router_data.read_time_text(t_fail_last)}）",
                    "kind": "warn",
                    "status": "usage_failing",
                    "status_reason": "read_failed",
                    "last_success_at": t_fail_last,
                    "fetched_at": t_fail_last,
                    "plan": "pro",
                    "usage_status": "stale",
                    "usage_age_seconds": 86400,
                    "refreshable": True,
                    "windows": [{
                        "id": "usage",
                        "hours": 168,
                        "remaining_pct": 75.0,
                        "reserved_pct": 0.0,
                        "reset_in": "4天2小时",
                        "stale": True,
                    }],
                })
            else:
                cards.append({
                    "account": "codex",
                    "label": "Codex",
                    "badge": "就绪",
                    "kind": "ok",
                    "status": "ready",
                    "plan": "pro",
                    "fetched_at": claude_at,
                    "usage_status": "fresh",
                    "usage_age_seconds": 60,
                    "refreshable": True,
                    "windows": [{
                        "id": "usage",
                        "hours": 168,
                        "remaining_pct": 75.0,
                        "reserved_pct": 0.0,
                        "reset_in": "4天2小时",
                        "stale": False,
                    }],
                })
            return cards

        current_cards = [make_cards(85.0, t1)]

        def sync_run(work, done=None, status=None, long=False):
            if status:
                panel.status.set(status)
            res = work()
            if done:
                done(res)

        with mock.patch.object(router_data, "account_cards", side_effect=lambda: current_cards[0]), \
                mock.patch.object(router_data, "workers", return_value=[]), \
                mock.patch.object(router_data, "collect_usage_now") as http_collect, \
                mock.patch.object(router_window.theme, "set_window_icon"), \
                mock.patch.object(panel_plugins.plugins, "load", return_value=panel_plugins.plugins.defaults()), \
                mock.patch.object(panel_widgets, "log_error") as errors:
            with mock.patch.object(router_window.Panel, "run"), mock.patch.object(router_window.Panel, "prefill"):
                panel = router_window.Panel(root, "scoring")
            self.addCleanup(lambda: panel._poll_id and root.after_cancel(panel._poll_id))

            with mock.patch.object(panel, "run", side_effect=sync_run), \
                    mock.patch.object(panel, "schedule_prebuild"):
                # Prebuild builds "quota" in the background with initial snapshot (85% at t1).
                panel.prebuild_next()
                self.assertIn("quota", panel.built)
                self.assertIn("quota", panel.stale)
                self.assertEqual(panel.quota_cards[0]["windows"][0][1].cget("text"), "85%")
                self.assertEqual(panel.quota_cards[0]["windows"][0][2].value, 85.0)

                # Collector updates cache to 42% at t2 while window is on "scoring"; first visit to "quota" must refresh.
                current_cards[0] = make_cards(42.0, t2, codex_failing=True)
                panel.show("quota")
                self.assertEqual(panel.quota_cards[0]["windows"][0][1].cget("text"), "42%")
                self.assertEqual(panel.quota_cards[0]["windows"][0][2].value, 42.0)
                self.assertIn(router_data.read_time_text(t2), panel.quota_updated.cget("text"))
                self.assertEqual(panel.quota_cards[0]["read_at"].cget("text"), router_data.read_time_text(t2))

                # Failing account is explicitly flagged and its old reading is marked stale, never fresh.
                codex_card = panel.quota_cards[1]
                self.assertEqual(codex_card["name"].cget("text"), "Codex · 读数失败")
                self.assertEqual(int(panel.quota_cards[0]["read_at"].grid_info()["row"]), 0)
                self.assertEqual(int(codex_card["read_at"].grid_info()["row"]), 1)
                self.assertIn(router_data.read_time_text(t_fail_last), codex_card["read_at"].cget("text"))
                self.assertIn("旧读数", codex_card["read_at"].cget("text"))
                self.assertTrue(codex_card["windows"][0][2].stale)
                self.assertEqual(codex_card["windows"][0][1].cget("fg"), "#64748B")

                # Switch away to "scoring", collector updates cache to 18% at t3, then switch back to "quota".
                panel.show("scoring")
                current_cards[0] = make_cards(18.0, t3, codex_failing=True)
                panel.show("quota")
                self.assertEqual(panel.quota_cards[0]["windows"][0][1].cget("text"), "18%")
                self.assertEqual(panel.quota_cards[0]["windows"][0][2].value, 18.0)
                self.assertEqual(panel.quota_cards[0]["windows"][0][2].colour, "Amber")
                self.assertIn(router_data.read_time_text(t3), panel.quota_updated.cget("text"))
                self.assertEqual(panel.quota_cards[0]["read_at"].cget("text"), router_data.read_time_text(t3))
                http_collect.assert_not_called()
            errors.assert_not_called()


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agent-compass" / "scripts"
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
                                                                 "ROUTER_SCORER_TYPE": "",
                                                                 "ROUTER_SCORE_CACHE": "off"}),
                        mock.patch.object(route_judge, "user_env", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_types_match_route_judge(self):
        self.assertEqual([row[0] for row in router_data.SCORER_TYPES], list(self.router.SCORER_TYPES))
        self.assertEqual([row[0] for row in router_data.SCORER_TYPES[:4]], ["jev", "claude_haiku_low", "codex_terra_low", "agy_flash_low"])
        self.assertEqual({row[0] for row in router_data.SCORER_TYPES if row[2]}, {"jev", "openai_compatible"})

    def test_read_and_save_keep_masked_keys(self):
        config = router_data.read_scorer_config()
        self.assertEqual((config["scorer_type"], config["fields"]["jev"]["base_url"], config["fields"]["jev"]["model"],
                          config["fields"]["jev"]["key_hint"]), ("claude_haiku_low", "https://api.typesafe.ai", "jev-latest", "尚未配置 API Key"))
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
        router_data.save_scorer_config("claude_haiku_low")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((stored["scorer_type"], stored["jev_api_key"], stored["api_key"], stored["model"]),
                         ("claude_haiku_low", "ts-new-key-99998888", "sk-custom-1234567890", "qwen3-4b"))
        router_data.save_scorer_config("jev", "", "", "")
        self.assertNotIn("jev_api_key", json.loads(self.path.read_text(encoding="utf-8")))
        with self.assertRaises(ValueError):
            router_data.save_scorer_config("openai_compatible", "k", "", "")
        with self.assertRaises(ValueError):
            router_data.save_scorer_config("heuristic")
        with self.assertRaises(ValueError):
            router_data.save_scorer_config("nope")
        with mock.patch.object(self.router, "user_env", side_effect=lambda n: "env-key-123456789" if n == "TYPESAFE_API_KEY" else None):
            field = router_data.read_scorer_config()["fields"]["jev"]
        self.assertEqual((field["env_key"], field["key_hint"], field["api_key_masked"]), (True, "使用环境变量 TYPESAFE_API_KEY", ""))

    def test_test_scorer_reports_success_and_clear_failures(self):
        router_data.save_scorer_config("jev", SECRET)
        response = {"model": "jev-latest", "answers": {
            "work_kind": {"choice": "general_implementation"}, "interaction_style": {"choice": "engineering"},
            "reasoning_depth": {"score": 1.2}, "code_complexity": {"score": 2.1},
            "demand_divergence": {"score": 0.9}, "demand_coding": {"score": 2.2}, "demand_aesthetics": {"score": 0.0},
            "demand_tool_operation": {"score": 0.0},
            "needs_visual": {"noul": 0.02}, "needs_gui": {"noul": 0.01}, "local_only": {"noul": 0.9},
            "wants_fast": {"noul": 0.05}, "read_only_review": {"noul": 0.02},
            "access": {"choice": "write"}, "multi_task": {"noul": 0.05}, "task_count": {"choice": "1"}}}
        seen = []
        with mock.patch.object(self.router, "post_json", side_effect=lambda url, body, key, timeout: seen.append(key) or response):
            message = router_data.test_scorer_config()
            router_data.test_scorer_config("jev", "unsaved-key-000011112222")
        self.assertRegex(message, r"^测试成功（Jev · \d+ms）：general_implementation · D2 C2$")
        self.assertEqual(seen, [SECRET, "unsaved-key-000011112222"])
        router_data.save_scorer_config("jev", "")
        with mock.patch.object(self.router, "post_json", side_effect=AssertionError("no call without a key")), \
                mock.patch.object(self.router, "scorer_prefix", side_effect=AssertionError("no agy fallback in a test")):
            message = router_data.test_scorer_config()
        self.assertTrue(message.startswith("测试失败（Jev"), message)
        self.assertIn("TypeSafe API key not configured", message)

    def test_simulate_can_profile_with_the_live_scorer(self):
        with mock.patch.object(router_data, "plan_task", side_effect=AssertionError("should not plan without scorer")):
            no_model = router_data.simulate("修复解析器的 bug", 1.0, live_scorer=False)
        self.assertEqual((no_model["status"], no_model["choice"], no_model["error"]), ("blocked", "none", "scorer_unavailable"))
        profile = {
            "work_kind": "debug_review", "reasoning_depth": 3, "code_complexity": 2, 
            
            "needs_visual": True, "needs_gui": False, "local_only": True, "wants_fast": False, "read_only_review": False, "interaction_style": "engineering",
            "multi_task": False, "task_count": "1",
        }
        with mock.patch.object(router_data, "plan_task", side_effect=lambda task: {"task": task}), \
                mock.patch.object(self.router, "score_task", return_value=(profile, {"source": "system_one", "model": "m"})):
            live = router_data.simulate("修复解析器的 bug", 1.0, live_scorer=True)
        self.assertEqual((live["task"]["work_kind"], live["task"]["difficulty"], live["scorer"]["model"]),
                         ("debug_review", "coupled", "m"))
        self.assertEqual(live["task"]["needs"], {})
        allocator, _ = router_data.modules()
        self.assertEqual(allocator.resolve_modality_needs(live["task"]["needs"], live["task"]["profile"], {}),
                         {"visual": True, "local_only": True})
        with mock.patch.object(router_data, "plan_task", side_effect=AssertionError("should not plan when scorer unavailable")), \
                mock.patch.object(self.router, "score_task", return_value=(None, {"source": "scorer_unavailable", "fallback_detail": "timed out"})):
            unavail = router_data.simulate("修复解析器的 bug", 1.0, live_scorer=True)
        self.assertEqual((unavail["status"], unavail["choice"], unavail["error"]), ("blocked", "none", "scorer_unavailable"))
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        with mock.patch.object(router_window.Panel, "run"), mock.patch.object(router_window.Panel, "prefill"), \
                mock.patch.object(router_window.theme, "set_window_icon"):
            panel = router_window.Panel(root, "scoring")
            option_labels = [w.cget("text") for w in panel._option_items[1:]]
            self.assertNotIn("小模型打分", option_labels)
            panel.plan_loaded(unavail)
            self.assertIn("未完成评分：小模型分类器不可用（timed out）", panel.status.get())
            self.assertNotIn("胜出 none", panel.status.get())


class ResetEditorTests(unittest.TestCase):
    def test_editor_uses_time_choices_and_submits_dynamic_rule(self):
        import router_window
        import tk_test_support

        root = tk_test_support.shared_tk_root(self)
        with mock.patch.object(router_window.Panel, 'run'), mock.patch.object(router_window.Panel, 'prefill'), \
                mock.patch.object(router_window.theme, 'set_window_icon'):
            panel = router_window.Panel(root, 'policy')
            panel.open_boost_editor()
            self.assertEqual(tuple(panel.boost_hours.cget('values')), ('1h', '3h', '6h', '12h', '24h'))
            self.assertFalse(hasattr(panel, 'boost_value'))
            self.assertFalse(hasattr(panel, 'boost_reset'))
            panel.boost_pairs = [('codex', 'gpt-6-astra')]
            panel.boost_target.configure(values=['Astra'])
            panel.boost_target.current(0)
            panel.boost_hours.set('1h')
            panel.add_boost()
            work = panel.run.call_args.args[0]
            with mock.patch.object(router_data, 'add_boost') as save:
                work()
            save.assert_called_once_with('codex', 'gpt-6-astra', hours=1)


class SystemVersionCardTests(unittest.TestCase):
    def test_system_settings_shows_version_mismatch_update_available_and_one_click_update(self):
        import router_window
        import tk_test_support
        import updates

        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, "run"), mock.patch.object(router_window.Panel, "prefill"), \
                mock.patch.object(router_window.theme, "set_window_icon"):
            panel = router_window.Panel(root, "policy")
            panel.show_setting("system")
            self.assertIn("AgentCompass", panel.ac_version_var.get())
            panel.ac_version_loaded({
                "ok": True,
                "tray_version": "0.3.8",
                "shape": "git",
                "display_version": "AgentCompass 0.3.8 · 命令行 0.3.5（版本不一致）",
                "version_mismatch": True,
                "update_available": True,
                "update_label": "可更新到 v0.3.9",
                "status_line": "可更新到 v0.3.9",
                "installs": {"separate_routers": [{"installer": "uv", "version": "0.3.5", "root": "C:/uv/site-packages"}]},
            })
            self.assertEqual(panel.ac_version_var.get(), "AgentCompass 0.3.8 · 命令行 0.3.5（版本不一致）")
            self.assertEqual(panel.ac_version_pill.cget("text").strip(), "可更新到 v0.3.9")
            self.assertIn("Router (uv 0.3.5)", panel.ac_version_detail_var.get())

            # Clicking "更新 AgentCompass" dispatches updates.update_agentcompass
            panel.update_agentcompass_now()
            work, done = panel.run.call_args.args[0], panel.run.call_args.args[1]
            with mock.patch.object(updates, "update_agentcompass", return_value="已完成更新") as upd, \
                    mock.patch.object(updates, "version_status", return_value={
                        "ok": True,
                        "tray_version": "0.3.9",
                        "shape": "git",
                        "display_version": "AgentCompass 0.3.9",
                        "version_mismatch": False,
                        "update_available": False,
                        "status_line": "已是最新版本（v0.3.9）",
                        "installs": {"separate_routers": []},
                    }):
                res = work()
                done(res)
            upd.assert_called_once()
            self.assertEqual(panel.ac_version_var.get(), "AgentCompass 0.3.9")
            self.assertEqual(panel.ac_version_pill.cget("text").strip(), "已是最新")


if __name__ == "__main__":
    unittest.main()
