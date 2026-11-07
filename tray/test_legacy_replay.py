"""A ledger row written before the classifier answered the split question has no profile that can be replayed honestly."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/agent-compass/scripts"))
import router_data
from router_test_support import isolated_router_state

LEGACY = {"reasoning_depth": 4, "code_complexity": 3, "context_scale": 3, "hallucination_risk": 3, "error_cost": 3,
          "needs_visual": False, "needs_gui": False, "local_only": True, "interaction_style": "engineering",
          "work_kind": "debug_review"}
CURRENT = dict(LEGACY, multi_task=False, task_count="1")


def row_of(profile, objective="fix the flaky lock in worker pool"):
    return {"stage": "plan", "at": "2026-10-01T00:00:00+00:00", "objective": objective, "hours": 1.0, "kind": "debug_review",
            "difficulty": "frontier", "choice": "opus55_xhigh", "profile": profile}


class LegacyReplayTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        # router_priority finds the skill under CODEX_HOME; this checkout's layout is the same.
        home = mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(__file__).resolve().parents[1])})
        home.start()
        self.addCleanup(home.stop)

    def test_no_keyword_inference_and_no_invented_answer(self):
        allocator, _ = router_data.modules()
        for profile in (LEGACY, dict(LEGACY, multi_task=False), dict(LEGACY, task_count="1"), dict(LEGACY, multi_task="yes", task_count="2")):
            with self.subTest(profile=profile), \
                    mock.patch.object(allocator, "infer_task_profile") as infer, \
                    mock.patch.object(allocator, "plan") as plan:
                result = router_data.replay(row_of(profile))
                infer.assert_not_called()
                plan.assert_not_called()
                self.assertEqual(result["theatre_source"], "unreplayable")
                self.assertEqual(result["replay_note"], router_data.LEGACY_REPLAY_NOTE)
                self.assertFalse(result.get("choice"))
                self.assertEqual(result["ranking"], [])

    def test_the_row_is_marked_in_the_ledger_list(self):
        self.assertIn(router_data.LEGACY_REPLAY_NOTE, router_data.ledger_summary(row_of(LEGACY))["objective"])
        self.assertTrue(router_data.ledger_summary(row_of(LEGACY))["unreplayable"])
        self.assertFalse(router_data.ledger_summary(row_of(CURRENT))["unreplayable"])

    def test_build_task_refuses_rather_than_reclassifies(self):
        allocator, _ = router_data.modules()
        with mock.patch.object(allocator, "infer_task_profile") as infer, self.assertRaises(router_data.LegacyProfile):
            router_data.build_task("open the GUI and click the button", 1.0, profile=LEGACY)
        infer.assert_not_called()

    def test_a_row_that_answered_still_replays(self):
        allocator, _ = router_data.modules()
        with mock.patch.object(router_data, "plan_task", side_effect=lambda task: {"choice": "x", "task": task}) as plan, \
                mock.patch.object(allocator, "infer_task_profile") as infer:
            result = router_data.replay(row_of(CURRENT))
        infer.assert_not_called()
        plan.assert_called_once()
        self.assertEqual(result["theatre_source"], "recomputed")
        self.assertEqual(result["task"]["profile"]["reasoning_depth"], 4)

    def test_a_row_with_a_saved_snapshot_is_shown_as_saved(self):
        with mock.patch("decision_theatre.snapshot_result", return_value={"choice": "saved", "theatre_source": "snapshot"}):
            self.assertEqual(router_data.replay(row_of(LEGACY))["choice"], "saved")
            self.assertFalse(router_data.ledger_summary(row_of(LEGACY))["unreplayable"])

    def test_the_panel_says_so_and_plays_nothing(self):
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, "run"), mock.patch.object(router_window.Panel, "prefill"), \
                mock.patch.object(router_window.theme, "set_window_icon"):
            panel = router_window.Panel(root, "scoring")
        with mock.patch.object(panel.theatre, "play") as play:
            panel.plan_loaded(router_data.replay(row_of(LEGACY)))
        play.assert_not_called()
        self.assertEqual(panel.status.get(), router_data.LEGACY_REPLAY_NOTE)
        self.assertEqual(panel.breakdown.get_children(), ())


class TrayRouterSourceTests(unittest.TestCase):
    def test_the_suite_runs_this_checkout_not_the_installed_router(self):
        """The whole suite imported route_judge from ~/.codex, which hid a failing fixture from every full run."""
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(os.environ.get("CODEX_HOME"), str(root))
        allocator, route_judge = router_data.modules()
        for module in (allocator, route_judge):
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(root), module.__file__)


if __name__ == "__main__":
    unittest.main()
