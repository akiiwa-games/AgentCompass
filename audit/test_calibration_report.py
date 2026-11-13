import datetime as dt
import hashlib
import json
import math
import sqlite3
import statistics
import tempfile
import unittest
from pathlib import Path

import calibration_report as report

SECRET = "SECRET-TASK-TEXT"
PRICE = {"input": 2.0, "output": 10.0, "cache_read": 0.5}
STAMP = "2026-11-05T10:00:00+00:00"
SINCE, UNTIL = dt.date(2026, 11, 1), dt.date(2026, 11, 10)


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
    (root / "router-ledger.jsonl").write_bytes("".join(json.dumps(line) + "\n" for line in ledger).encode("utf-8"))


def build(root, since=SINCE, until=UNTIL, price=pricing):
    return report.build(root / "dispatch.sqlite3", root / "router-ledger.jsonl", since, until, price)


class CalibrationReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def sub(self, name):
        path = self.root / name
        path.mkdir()
        return path

    # ---- cells, formulas

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
        ratios = [a / 3600 / 2.0 for a in actuals]
        self.assertAlmostEqual(cell["ratio_median"], statistics.median(ratios))
        self.assertAlmostEqual(cell["ratio_q1"], statistics.quantiles(ratios, n=4, method="inclusive")[0])
        self.assertAlmostEqual(cell["expected_hours_median"], 2.0)

    def test_demand_profile_is_the_dominant_category(self):
        tie = {"axis_demand": {"divergence": 0, "coding": 3, "aesthetics": 1, "tool_operation": 3}}
        self.assertEqual(report.demand_label(tie), "coding+tool_operation")
        self.assertEqual(report.demand_label({"axis_demand": dict.fromkeys(report.AXES, 0)}), "none")
        self.assertEqual(report.demand_label({}), "unclassified")

    def test_list_price_cost_follows_the_per_million_formula(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 500_000, "cache_read_tokens": 4_000_000}
        self.assertEqual(report.list_price_usd(usage, PRICE), (2.0 + 5.0 + 2.0, False))
        self.assertEqual(report.list_price_usd(usage, {"input": 2.0}), (None, False))
        self.assertEqual(report.list_price_usd(None, PRICE), (None, False))

    def test_cache_write_is_priced_when_listed_and_a_lower_bound_when_not(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 2_000_000}
        self.assertEqual(report.list_price_usd(usage, {**PRICE, "cache_write": 2.5}), (2.0 + 5.0, False))
        self.assertEqual(report.list_price_usd(usage, PRICE), (2.0, True))
        self.assertEqual(report.list_price_usd({**usage, "cache_write_tokens": 0}, PRICE), (2.0, False))
        self.assertEqual(report.list_price_usd({"cache_write_tokens": 5}, PRICE), (None, False))

    def test_lower_bound_cells_show_the_marker_and_cache_write_tokens(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 700}
        write_state(self.root, [dispatch(1, tokens=usage)])
        out = build(self.root)
        cell = out["spend"]["by_model_effort"]["m1@high"]
        self.assertEqual((cell["lower_bound_rows"], cell["cache_write_tokens_median"]), (1, 700.0))
        self.assertIn(">=2.00 (1/1 rows)", report.render(out))

    def test_window_share_is_cost_over_capacity(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0}
        write_state(self.root, [dispatch(1, tokens=usage)])
        cell = build(self.root)["spend"]["by_model_effort"]["m1@high"]
        self.assertAlmostEqual(cell["window_share_pct_median"], 100 * 2.0 / 100.0)
        self.assertEqual(cell["window"], "weekly")

    def test_the_rate_check_does_not_depend_on_the_hours_estimate(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0}   # 2.0 USD over 1 active hour
        rows = [dispatch(1, tokens=usage, hours=0.1), dispatch(2, tokens=usage, hours=10.0)]
        write_state(self.root, rows)
        cell = build(self.root, price=lambda b: {**pricing(b), "usd_per_hour": 4.0})["spend"]["by_model_effort"]["m1@high"]
        self.assertAlmostEqual(cell["actual_usd_per_active_hour_median"], 2.0)
        self.assertAlmostEqual(cell["rate_ratio_median"], 2.0 / 4.0)

    def test_spend_cells_carry_medians_quartiles_and_the_too_small_mark(self):
        costs = [1, 2, 3, 4, 5]
        rows = [dispatch(i, tokens={"input_tokens": c * 500_000, "output_tokens": 0, "cache_read_tokens": 0})
                for i, c in enumerate(costs)]
        write_state(self.root, rows)
        cell = build(self.root)["spend"]["by_model_effort"]["m1@high"]
        ratios = [c / 2.0 for c in costs]
        self.assertFalse(cell["small"])
        self.assertAlmostEqual(cell["ratio_median"], statistics.median(ratios))
        self.assertAlmostEqual(cell["ratio_q1"], statistics.quantiles(ratios, n=4, method="inclusive")[0])
        self.assertAlmostEqual(cell["actual_usd_median"], statistics.median(costs))
        small_root = self.sub("small")
        write_state(small_root, rows[: report.MIN_CELL - 1])
        small = build(small_root)["spend"]["by_model_effort"]["m1@high"]
        self.assertTrue(small["small"] and small["tokens_small"])
        self.assertIsNone(small["ratio_q1"])

    # ---- groups, endings, joins

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

    def test_the_rotated_away_worker_is_counted_as_rotated(self):
        old = dispatch(1, agent_id="worn", created="2026-11-04T10:00:00+00:00")
        new = dispatch(2, decision="rot")
        write_state(self.root, [old, new], ledger=[{"stage": "plan", "decision_id": "rot", "action": "rotate", "at": STAMP}])
        connection = sqlite3.connect(self.root / "dispatch.sqlite3")
        connection.execute("INSERT INTO decisions VALUES ('rot', ?)", (json.dumps(
            {"decision_id": "rot", "allocation_task": {"chain": {"worker": {"agent_id": "worn"}}}}),))
        connection.commit()
        connection.close()
        out = build(self.root)
        cell = out["endings"]["by_model_effort"]["m1@high"]
        self.assertEqual((cell["started_by_rotation"], cell["rotated_away"]), (1, 1))
        self.assertEqual(out["coverage"]["rotations_without_predecessor"], 0)

    def test_a_rotation_without_a_findable_predecessor_is_counted(self):
        write_state(self.root, [dispatch(1, decision="rot")],
                    ledger=[{"stage": "plan", "decision_id": "rot", "action": "rotate", "at": STAMP}])
        self.assertEqual(build(self.root)["coverage"]["rotations_without_predecessor"], 1)

    def test_a_retried_tasks_later_attempt_is_left_out_of_hours_and_spend(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0}
        write_state(self.root, [dispatch(1, tokens=usage), dispatch(2, tokens=usage, attempt_of="x", attempt=2)])
        out = build(self.root)
        hours, spend = out["hours"]["by_work_kind"]["coding_task"], out["spend"]["by_model_effort"]["m1@high"]
        self.assertEqual((hours["n"], hours["retried_left_out"], spend["n"], spend["retried_left_out"]), (1, 1, 1, 1))

    # ---- ranges and coverage

    def test_range_boundaries_are_inclusive_at_both_ends_in_utc(self):
        rows = [dispatch(1, created="2026-10-31T23:59:59+00:00"), dispatch(2, created="2026-11-01T00:00:00+00:00"),
                dispatch(3, created="2026-11-10T23:59:59+00:00"), dispatch(4, created="2026-11-11T00:00:00+00:00"),
                dispatch(5, created="2026-11-01T02:00:00+08:00")]        # 2026-10-31T18:00Z: before the range
        write_state(self.root, rows)
        self.assertEqual(build(self.root)["coverage"]["dispatches"], 2)

    def test_a_plan_whose_dispatch_lands_after_the_range_end_is_not_lost(self):
        ledger = [{"stage": "plan", "decision_id": "late", "choice": "m1", "at": "2026-11-10T23:59:00+00:00"},
                  {"stage": "plan", "decision_id": "lost", "choice": "m1", "at": STAMP},
                  {"stage": "plan", "decision_id": "mine", "choice": "self", "at": STAMP},
                  {"stage": "plan", "decision_id": "split", "choice": "split", "at": STAMP}]
        write_state(self.root, [dispatch(1, decision="late", created="2026-11-11T00:00:30+00:00")], ledger=ledger)
        cov = build(self.root)["coverage"]
        self.assertEqual((cov["ledger_decisions"], cov["ledger_decisions_without_dispatch"],
                          cov["ledger_deliberate_non_dispatches"]), (2, 1, 2))

    def test_coverage_names_completed_dispatches_without_measurements(self):
        write_state(self.root, [dispatch(1, active=None, active_seconds_reason="grok_session_telemetry_unsupported",
                                         telemetry_harness="grok"), dispatch(2)])
        cov = build(self.root)["coverage"]
        self.assertEqual(cov["completed_without_active_time"], 1)
        self.assertEqual(cov["missing_active_time_reasons"], {"grok_session_telemetry_unsupported": 1})
        self.assertEqual(cov["completed_by_path"], {"paseo/codex": 1, "paseo/grok": 1})

    def test_the_summary_ranks_deviations_symmetrically_on_the_log_ratio(self):
        rows = [dispatch(i, kind="under", hours=5.0, active=3600.0) for i in range(5)]         # ratio 0.2
        rows += [dispatch(10 + i, kind="over", hours=1.0, active=7200.0) for i in range(5)]    # ratio 2.0
        write_state(self.root, rows)
        line = report.summary(build(self.root))
        self.assertIn(" under x0.20", line)
        self.assertGreater(abs(math.log(0.2)), abs(math.log(2.0)))

    # ---- errors and malformed input

    def test_missing_state_directory_is_an_error_and_nothing_is_created(self):
        missing = self.root / "typo"
        self.assertEqual(report.main(["--state-dir", str(missing)], pricing=pricing), 2)
        self.assertFalse(missing.exists())

    def test_missing_database_names_the_file(self):
        with self.assertRaisesRegex(report.ReportError, "dispatch.sqlite3"):
            build(self.root)

    def test_a_non_sqlite_file_and_a_missing_table_are_named_errors(self):
        (self.root / "dispatch.sqlite3").write_bytes(b"this is not sqlite" * 50)
        with self.assertRaisesRegex(report.ReportError, "not a readable SQLite database"):
            build(self.root)
        other = self.sub("other")
        sqlite3.connect(other / "dispatch.sqlite3").close()
        with self.assertRaisesRegex(report.ReportError, "dispatches table"):
            build(other)

    def test_unreadable_rows_and_ledger_lines_are_counted_not_fatal(self):
        write_state(self.root, [dispatch(1)])
        connection = sqlite3.connect(self.root / "dispatch.sqlite3")
        connection.execute("INSERT INTO dispatches VALUES ('bad','bad-dec','not json')")
        connection.execute("INSERT INTO dispatches VALUES ('list','list-dec','[1]')")
        connection.execute("INSERT INTO decisions VALUES ('d','[1]')")
        connection.commit()
        connection.close()
        (self.root / "router-ledger.jsonl").write_bytes(b'{"stage":"plan","decision_id":"x"}\n\xff\xfe broken\n[1]\n')
        out = build(self.root)
        self.assertEqual(out["coverage"]["skipped"], {"dispatch_rows_unreadable": 2, "decision_rows_unreadable": 1,
                                                      "ledger_lines_unreadable": 2})
        self.assertEqual(out["coverage"]["dispatches"], 1)

    def test_a_missing_ledger_is_counted_not_fatal(self):
        write_state(self.root, [dispatch(1)])
        (self.root / "router-ledger.jsonl").unlink()
        self.assertEqual(build(self.root)["coverage"]["skipped"], {"ledger_missing": 1})

    # ---- read-only and privacy

    def test_output_holds_no_task_text_paths_or_ids(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1}
        write_state(self.root, [dispatch(i, tokens=usage) for i in range(3)], decisions=[("x", "user_override")])
        out = self.root / "calibration" / "run"
        code = report.main(["--state-dir", str(self.root), "--since", "2026-11-01", "--until", "2026-11-10",
                            "--out", str(out)], pricing=pricing)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["report.json", "report.md"])
        for name in ("report.md", "report.json"):
            text = (out / name).read_bytes().decode("utf-8")
            self.assertNotIn(SECRET, text)
            self.assertNotIn("\r\n", text)

    def test_a_run_changes_no_state_file(self):
        write_state(self.root, [dispatch(1)], ledger=[{"stage": "plan", "decision_id": "a", "choice": "m1", "at": STAMP}])
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}
        out = self.sub("out")
        report.main(["--state-dir", str(self.root), "--out", str(out)], pricing=pricing)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir() if p.name != "out"}
        self.assertEqual(after, before)

    def test_the_real_pricing_path_writes_nothing_outside_the_report_output(self):
        state, out = self.sub("state"), self.sub("out")
        write_state(state, [dispatch(1, tokens={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1})])
        (state / "provider-inventory.json").write_text("{not json", encoding="utf-8")

        def snapshot():
            return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                    for base in (Path.home(), state) for p in base.rglob("*") if p.is_file()}

        before = snapshot()
        self.assertEqual(report.main(["--state-dir", str(state), "--out", str(out)]), 0)
        self.assertEqual(snapshot(), before)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["report.json", "report.md"])

    def test_the_real_pricing_path_reads_the_shipped_list_price(self):
        scores = json.loads((report.SCRIPTS.parent / "references" / "five-axis-scores.json").read_text(encoding="utf-8"))
        name, row = next((n, r) for n, r in scores["models"].items() if (r.get("price") or {}).get("value"))
        price = report.accounts_pricing(self.root)({"model": name, "account": "none", "effort": "high"})
        self.assertEqual(price["list_price"], row["price"]["value"])


if __name__ == "__main__":
    unittest.main()
