"""A ledger row whose profile the classifier's current schema rejects (an older row without the split answer or the category demand) cannot be replayed honestly."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/agent-compass/scripts"))
import router_data
from router_test_support import isolated_router_state

OLD = {"reasoning_depth": 4, "code_complexity": 3, 
          "needs_visual": False, "needs_gui": False, "local_only": True, "interaction_style": "engineering",
          "work_kind": "debug_review"}
CURRENT = dict(OLD, multi_task=False, task_count="1", wants_fast=False, read_only_review=False, access="write", expected_hours=1.0, hours_confidence="medium", urgency="normal",
               axis_demand={"divergence": 1, "coding": 3, "aesthetics": 0, "tool_operation": 0})
MISSING_FLAGS = {k: v for k, v in CURRENT.items() if k not in ("wants_fast", "read_only_review")}
MISSING_DEMAND = {k: v for k, v in CURRENT.items() if k != "axis_demand"}


def row_of(profile, objective="fix the flaky lock in worker pool"):
    return {"stage": "plan", "at": "2026-10-01T00:00:00+00:00", "objective": objective, "hours": 1.0, "kind": "debug_review",
            "difficulty": "frontier", "choice": "opus55_xhigh", "profile": profile}


class UnreplayableRowTests(unittest.TestCase):
    def setUp(self):
        isolated_router_state(self.addCleanup)
        # router_priority finds the skill under CODEX_HOME; this checkout's layout is the same.
        home = mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(__file__).resolve().parents[1])})
        home.start()
        self.addCleanup(home.stop)

    def test_no_keyword_inference_and_no_invented_answer(self):
        allocator, _ = router_data.modules()
        for profile in (
            OLD,
            MISSING_FLAGS,
            MISSING_DEMAND,
            dict(OLD, multi_task=False),
            dict(OLD, task_count="1"),
            dict(OLD, multi_task="yes", task_count="2"),
        ):
            with self.subTest(profile=profile), \
                    mock.patch.object(allocator, "infer_task_profile", create=True) as infer, \
                    mock.patch.object(allocator, "plan") as plan:
                result = router_data.replay(row_of(profile))
                infer.assert_not_called()
                plan.assert_not_called()
                self.assertEqual(result["theatre_source"], "unreplayable")
                self.assertEqual(result["replay_note"], router_data.UNREPLAYABLE_NOTE)
                self.assertFalse(result.get("choice"))
                self.assertEqual(result["ranking"], [])

    def test_the_row_is_marked_in_the_ledger_list(self):
        self.assertIn(router_data.UNREPLAYABLE_NOTE, router_data.ledger_summary(row_of(OLD))["objective"])
        self.assertTrue(router_data.ledger_summary(row_of(OLD))["unreplayable"])
        self.assertTrue(router_data.ledger_summary(row_of(MISSING_FLAGS))["unreplayable"])
        self.assertTrue(router_data.ledger_summary(row_of(MISSING_DEMAND))["unreplayable"])
        self.assertFalse(router_data.ledger_summary(row_of(CURRENT))["unreplayable"])

    def test_build_task_refuses_rather_than_reclassifies(self):
        allocator, _ = router_data.modules()
        for old_profile in (OLD, MISSING_FLAGS, MISSING_DEMAND):
            with self.subTest(profile=old_profile), \
                    mock.patch.object(allocator, "infer_task_profile", create=True) as infer, \
                    self.assertRaises(router_data.UnreplayableProfile):
                router_data.build_task("open the GUI and click the button", 1.0, profile=old_profile)
            infer.assert_not_called()

    def test_a_row_that_answered_still_replays(self):
        allocator, _ = router_data.modules()
        with mock.patch.object(router_data, "plan_task", side_effect=lambda task: {"choice": "x", "task": task}) as plan, \
                mock.patch.object(allocator, "infer_task_profile", create=True) as infer:
            result = router_data.replay(row_of(CURRENT))
        infer.assert_not_called()
        plan.assert_called_once()
        self.assertEqual(result["theatre_source"], "recomputed")
        self.assertEqual(result["task"]["profile"]["reasoning_depth"], 4)

    def test_a_row_with_a_saved_snapshot_is_shown_as_saved(self):
        with mock.patch("decision_theatre.snapshot_result", return_value={"choice": "saved", "theatre_source": "snapshot"}):
            self.assertEqual(router_data.replay(row_of(OLD))["choice"], "saved")
            self.assertFalse(router_data.ledger_summary(row_of(OLD))["unreplayable"])

    @pytest.mark.slow
    def test_the_panel_says_so_and_plays_nothing(self):
        import router_window
        import tk_test_support
        root = tk_test_support.shared_tk_root(self)
        root.withdraw()
        with mock.patch.object(router_window.Panel, "run"), mock.patch.object(router_window.Panel, "prefill"), \
                mock.patch.object(router_window.theme, "set_window_icon"):
            panel = router_window.Panel(root, "scoring")
        with mock.patch.object(panel.theatre, "play") as play:
            panel.plan_loaded(router_data.replay(row_of(OLD)))
        play.assert_not_called()
        self.assertEqual(panel.status.get(), router_data.UNREPLAYABLE_NOTE)
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
