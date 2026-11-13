import datetime as dt
import hashlib
import json
import sqlite3
import statistics
import tempfile
import unittest
from pathlib import Path

import calibration_report as report

SECRET = "SECRET-TASK-TEXT"
PRICE = {"input": 2.0, "output": 10.0, "cache_read": 0.5}
STAMP = "2026-11-05T10:00:00+00:00"


def pricing(bundle):
    return {"list_price": PRICE, "window_id": "weekly", "capacity_usd": 100.0}


def dispatch(n, *, state="completed", kind="coding_task", hours=1.0, active=3600.0, model="m1", effort="high",
             decision=None, demand=None, tokens=None, created=STAMP, **extra):
    row = {"dispatch_id": f"{SECRET}-d{n}", "decision_id": decision or f"{SECRET}-dec{n}", "state": state,
           "created_at": created, "cwd": f"/{SECRET}/dir", "brief_path": f"/{SECRET}/brief.md",
           "bundle": {"model": model, "effort": effort, "account": "acct"}, "expected_usd": 2.0,
           "profile": {"work_kind": kind, "expected_hours": hours,
                       "axis_demand": demand or {"divergence": 0, "coding": 2, "aesthetics": 0, "tool_operation": 1}},
           "ground_truth_recorded_at": created, "telemetry_harness": "codex"}
    if active is not None:
        row["active_seconds"] = active
    if tokens is not None:
        row["token_usage"] = tokens
    row.update(extra)
    return row


def write_state(root: Path, dispatches, decisions=(), ledger=()):
    connection = sqlite3.connect(root / "dispatch.sqlite3")
    connection.execute("CREATE TABLE dispatches (id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
    connection.execute("CREATE TABLE decisions (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    for row in dispatches:
        connection.execute("INSERT INTO dispatches VALUES (?,?,?)", (row["dispatch_id"], row["decision_id"], json.dumps(row)))
    for decision_id, constraint in decisions:
        connection.execute("INSERT INTO decisions VALUES (?,?)", (decision_id, json.dumps(
            {"decision_id": decision_id, "constraint": constraint, "task": {"objective": SECRET}})))
    connection.commit()
    connection.close()
    (root / "router-ledger.jsonl").write_text("".join(json.dumps(line) + "\n" for line in ledger), encoding="utf-8")


def build(root, since=dt.date(2026, 11, 1), until=dt.date(2026, 11, 10)):
    return report.build(root / "dispatch.sqlite3", root / "router-ledger.jsonl", since, until, pricing)


class CalibrationReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_small_cells_are_marked_at_the_stated_minimum(self):
        enough = [dispatch(i) for i in range(report.MIN_CELL)]
        few = [dispatch(100 + i, kind="other_or_mixed") for i in range(report.MIN_CELL - 1)]
        write_state(self.root, enough + few)
        cells = build(self.root)["hours"]["by_work_kind"]
        self.assertFalse(cells["coding_task"]["small"])
        self.assertTrue(cells["other_or_mixed"]["small"])
        self.assertEqual(cells["other_or_mixed"]["n"], report.MIN_CELL - 1)

    def test_hours_ratio_is_the_median_of_actual_over_expected(self):
        actuals = [1800.0, 3600.0, 7200.0, 10800.0, 14400.0]
        write_state(self.root, [dispatch(i, active=a, hours=2.0) for i, a in enumerate(actuals)])
        cell = build(self.root)["hours"]["by_work_kind"]["coding_task"]
        self.assertAlmostEqual(cell["ratio_median"], statistics.median(a / 3600 / 2.0 for a in actuals))
        self.assertAlmostEqual(cell["expected_hours_median"], 2.0)

    def test_demand_profile_is_the_dominant_category(self):
        tie = {"axis_demand": {"divergence": 0, "coding": 3, "aesthetics": 1, "tool_operation": 3}}
        self.assertEqual(report.demand_label(tie), "coding+tool_operation")
        self.assertEqual(report.demand_label({"axis_demand": dict.fromkeys(report.AXES, 0)}), "none")
        self.assertEqual(report.demand_label({}), "unclassified")

    def test_list_price_cost_follows_the_per_million_formula(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 500_000, "cache_read_tokens": 4_000_000, "cache_write_tokens": 9}
        self.assertAlmostEqual(report.list_price_usd(usage, PRICE), 2.0 + 5.0 + 2.0)
        self.assertIsNone(report.list_price_usd(usage, {"input": 2.0}))
        self.assertIsNone(report.list_price_usd(None, PRICE))

    def test_window_share_is_cost_over_capacity(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0}
        write_state(self.root, [dispatch(1, tokens=usage)])
        cell = build(self.root)["spend"]["by_model_effort"]["m1@high"]
        self.assertAlmostEqual(cell["window_share_pct_median"], 100 * 2.0 / 100.0)
        self.assertEqual(cell["window"], "weekly")

    def test_pinned_and_keep_whole_are_their_own_groups_outside_the_routed_tables(self):
        rows = [dispatch(1, decision="pin"), dispatch(2, decision="kept"), dispatch(3, decision="plain")]
        write_state(self.root, rows, decisions=[("pin", "user_override"), ("kept", None)],
                    ledger=[{"stage": "plan", "decision_id": "kept", "keep_whole": "why", "at": STAMP}])
        out = build(self.root)
        self.assertEqual({g: out["groups"][g] for g in report.GROUPS}, {"routed": 1, "pinned": 1, "keep_whole": 1})
        self.assertEqual(out["hours"]["by_work_kind"]["coding_task"]["n"], 1)
        self.assertEqual(set(out["own_groups"]), {"pinned", "keep_whole"})

    def test_endings_separate_relaunched_from_failed_and_count_rotation(self):
        first = dispatch(1, state="failed")
        rows = [first, dispatch(2, state="failed"), dispatch(3, state="canceled"), dispatch(4, state="started"),
                dispatch(5, state="expired"), dispatch(6, previous_attempt=first["dispatch_id"], decision="rot")]
        write_state(self.root, rows, ledger=[{"stage": "plan", "decision_id": "rot", "action": "rotate", "at": STAMP}])
        cell = build(self.root)["endings"]["by_model_effort"]["m1@high"]
        self.assertEqual({k: cell[k] for k in report.ENDINGS},
                         {"completed": 1, "failed": 1, "relaunched": 1, "canceled": 1, "other_terminal": 1, "open": 1})
        self.assertEqual(cell["started_by_rotation"], 1)

    def test_dispatches_outside_the_date_range_are_left_out(self):
        write_state(self.root, [dispatch(1), dispatch(2, created="2026-10-01T10:00:00+00:00")])
        self.assertEqual(build(self.root)["coverage"]["dispatches"], 1)

    def test_coverage_names_completed_dispatches_without_measurements(self):
        write_state(self.root, [dispatch(1, active=None, active_seconds_reason="grok_session_telemetry_unsupported",
                                         telemetry_harness="grok"), dispatch(2)])
        cov = build(self.root)["coverage"]
        self.assertEqual(cov["completed_without_active_time"], 1)
        self.assertEqual(cov["missing_active_time_reasons"], {"grok_session_telemetry_unsupported": 1})
        self.assertEqual(cov["completed_by_path"], {"paseo/codex": 1, "paseo/grok": 1})

    def test_output_holds_no_task_text_paths_or_ids_and_the_database_is_untouched(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1}
        write_state(self.root, [dispatch(i, tokens=usage) for i in range(3)], decisions=[("x", "user_override")])
        before = hashlib.sha256((self.root / "dispatch.sqlite3").read_bytes()).hexdigest()
        out = self.root / "calibration" / "run"
        code = report.main(["--state-dir", str(self.root), "--since", "2026-11-01", "--until", "2026-11-10",
                            "--out", str(out)], pricing=pricing)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["report.json", "report.md"])
        for name in ("report.md", "report.json"):
            text = (out / name).read_bytes().decode("utf-8")
            self.assertNotIn(SECRET, text)
            self.assertNotIn("\r\n", text)
        self.assertEqual(hashlib.sha256((self.root / "dispatch.sqlite3").read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), ["dispatch.sqlite3", "router-ledger.jsonl"])

    def test_missing_state_gives_an_empty_report(self):
        out = report.build(self.root / "none.sqlite3", self.root / "none.jsonl", dt.date(2026, 11, 1), dt.date(2026, 11, 2), None)
        self.assertEqual(out["coverage"]["dispatches"], 0)
        self.assertIn("no work kind has a readable hours sample", report.summary(out))


if __name__ == "__main__":
    unittest.main()
