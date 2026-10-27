import datetime as dt
import unittest

import router_audit as audit

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


def row(minutes, stage="select", choice="x", reason="", bundle=None, usage=None, **extra):
    return {"at": T0 + dt.timedelta(minutes=minutes), "stage": stage, "choice": choice, "reason": reason,
            "bundle": bundle, "usage": usage or {}, "source": "subscription_model_judgment", "judge": "gpt-6.1-sol",
            **extra}


class FakeRouter:
    @staticmethod
    def tightest_window(row, model=""):
        return None


def context(rows, **extra):
    base = {"rows": rows, "usage_now": {"accounts": {}}, "router": FakeRouter, "pinned": {}, "now_row": {"usage": {}},
            "heavy_by_day": {}, "heavy_detail": {}, "sessions": [], "dot": None, "ledger_rows": [row(0)], "peaks": {}}
    base.update(extra)
    return base


class ClassifyTests(unittest.TestCase):
    def test_constraint_record_and_specialist_routes_are_fixed(self):
        self.assertEqual(audit.classify({"source": "constraint_record", "constraint": "retained_owner"}),
                         "fixed:retained_owner")
        pro = {"provider": "chatgpt-web", "model": "gpt-6-pro"}
        self.assertEqual(audit.classify(row(0, bundle=pro)), "specialist:pro")
        self.assertEqual(audit.classify(row(0, bundle={"provider": "codex", "model": "gpt-6-astra"})), "specialist:astra")

    def test_instruction_words_mark_a_fixed_route_and_their_absence_marks_an_open_one(self):
        codex = {"provider": "codex", "model": "gpt-6.1-sol"}
        self.assertEqual(audit.classify(row(0, reason="Honors the explicit model requirement", bundle=codex)),
                         "fixed:instruction")
        self.assertEqual(audit.classify(row(0, reason="Codex is nearly empty, so Opus is cheaper here",
                                           bundle={"provider": "claude", "model": "claude-opus-5-5"})), "open")


class DecisionPlumbingTests(unittest.TestCase):
    def test_pairs_join_a_delegate_with_the_select_that_follows(self):
        rows = [row(0, "delegate"), row(2, "select"), row(60, "select"), row(120, "delegate")]
        pairs = audit.pair_decisions(rows)
        self.assertEqual([(d is not None, s is not None) for d, s in pairs],
                         [(True, True), (False, True), (True, False)])

    def test_ledger_record_supersedes_its_transcript_twin_only_within_minutes(self):
        ledger = [row(3, origin="ledger")]
        transcript = [row(1, origin="transcript"), row(30, origin="transcript")]
        merged = audit.merge(ledger, transcript)
        self.assertEqual([r["origin"] for r in merged], ["ledger", "transcript"])
        self.assertEqual(merged[1]["at"], T0 + dt.timedelta(minutes=30))

    def test_sliding_window_counts_the_densest_burst(self):
        times = [T0 + dt.timedelta(hours=h) for h in (0, 1, 2, 3, 20, 21)]
        self.assertEqual(audit.sliding_max(times, 12), 4)


class QuotaSeriesTests(unittest.TestCase):
    def usage(self, minutes, used):
        stamp = (T0 + dt.timedelta(minutes=minutes)).isoformat()
        return {"codex": {"fetched_at": stamp, "windows": [{"id": "usage", "used_pct": used}]}}

    def test_series_restarts_after_a_reset(self):
        rows = [row(0, usage=self.usage(0, 60)), row(60, usage=self.usage(60, 90)),
                row(120, usage=self.usage(120, 2)), row(180, usage=self.usage(180, 10))]
        self.assertEqual([p[1] for p in audit.quota_series(rows)], [2, 10])

    def test_stripped_usage_falls_back_to_the_percentage_in_the_reason(self):
        codex = {"provider": "codex", "account": "codex", "model": "gpt-6.1-sol"}
        rows = [row(0, usage=self.usage(0, 10)),
                row(60, reason="Fresh Codex telemetry shows 86% remaining", bundle=codex)]
        series = audit.quota_series(rows)
        self.assertEqual([(round(u), d) for _, u, d in series], [(10, False), (14, True)])


class FindingTests(unittest.TestCase):
    def keys(self, ctx):
        return {f["key"] for f in audit.build_findings(ctx)}

    def test_a_gate_that_always_says_delegate_is_reported(self):
        rows = [row(i, "delegate", "delegate") for i in range(25)] + [row(99, "delegate", "self")]
        self.assertIn("gate", self.keys(context(rows)))
        quiet = [row(i, "delegate", "delegate" if i % 2 else "self") for i in range(26)]
        self.assertNotIn("gate", self.keys(context(quiet)))

    def test_fixed_routes_dominating_selects_is_reported_with_the_open_count(self):
        codex = {"provider": "codex", "account": "codex", "model": "gpt-6.1-sol"}
        rows = [row(i, reason="explicit retained owner", bundle=codex) for i in range(9)]
        rows.append(row(50, reason="Opus has headroom", bundle={"provider": "claude", "model": "claude-opus-5-5"}))
        found = {f["key"]: f for f in audit.build_findings(context(rows))}
        self.assertIn("fixed", found)
        self.assertIn("open 1", found["fixed"]["evidence"])

    def test_planned_dispatches_are_not_counted_as_pinned(self):
        self.assertEqual(audit.classify(row(0, source="deterministic_allocator")), "planned")
        planned = [row(i, "plan", source="deterministic_allocator", bundle={"account": "claude"}, hours=1) for i in range(12)]
        found = {f["key"]: f for f in audit.build_findings(context(planned))}
        self.assertNotIn("fixed", found)
        self.assertNotIn("override", found)
        self.assertIn("claude 12.0h", found["balance"]["evidence"])

    def test_overrides_above_one_percent_are_reported_with_what_the_router_would_have_done(self):
        planned = [row(i, "plan", source="deterministic_allocator", bundle={"account": "claude"}, hours=1) for i in range(10)]
        pinned = row(99, "record", "constraint", source="constraint_record", constraint="user_override", origin="ledger",
                     agrees=False, router_choice="flash_high", bundle={"account": "codex", "bundle_id": "sol_max"})
        found = {f["key"]: f for f in audit.build_findings(context(planned + [pinned]))}
        self.assertIn("override", found)
        self.assertIn("sol_max", found["override"]["evidence"])
        self.assertIn("flash_high", found["override"]["evidence"])

    def capacity(self, minutes_old=5):
        return {"v": 2, "generated_at": (T0 - dt.timedelta(minutes=minutes_old)).isoformat(),
                "worker_hours": {"codex": {"24h": 6.5, "7d": 40.0}}, "active_workers": {"codex": {"count": 2, "by_bundle": {}}}}

    def capacity_ctx(self, capacity):
        return context([], capacity=capacity, now=T0)

    def test_missing_or_stale_capacity_is_reported_and_a_fresh_one_is_not(self):
        found = {f["key"]: f for f in audit.build_findings(self.capacity_ctx(None))}
        self.assertEqual(found["capacity"]["level"], "warn")
        self.assertIn("没有产出", found["capacity"]["title"])
        found = {f["key"]: f for f in audit.build_findings(self.capacity_ctx(self.capacity(minutes_old=6 * 60)))}
        self.assertIn("过期", found["capacity"]["title"])
        self.assertNotIn("capacity", {f["key"] for f in audit.build_findings(self.capacity_ctx(self.capacity()))})

    def test_work_hours_are_listed_and_no_usage_fit_is_judged(self):
        found = {f["key"]: f for f in audit.build_findings(self.capacity_ctx(self.capacity()))}
        self.assertIn("codex 6.5h/24h", found["workhours"]["evidence"])
        self.assertIn("codex 2", found["workhours"]["evidence"])
        self.assertFalse({"uncalibrated", "burn-codex-usage"} & set(found))

    def test_flash_agent_counts_are_listed(self):
        ctx = context([], capacity=self.capacity(), now=T0,
                      agent_days={"2026-10-08": {"antigravity": {"agents": 4, "peak_hour_concurrency": 2.5}},
                                  "2026-10-09": {"antigravity": {"agents": 6, "peak_hour_concurrency": 3.0}, "codex": {"agents": 9, "peak_hour_concurrency": 4.0}}})
        found = {f["key"]: f for f in audit.build_findings(ctx)}
        evidence = found["flash-agents"]["evidence"]
        for part in ("10-08 4 个代理、峰值并发 2.5", "10-09 6 个代理、峰值并发 3.0"):
            self.assertIn(part, evidence)
        self.assertNotIn("codex 9", evidence)
        self.assertNotIn("flash-agents", {f["key"] for f in audit.build_findings(self.capacity_ctx(self.capacity()))})

    def test_loaders_tolerate_missing_and_foreign_files(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.json"
            self.assertIsNone(audit.load_capacity(path))
            path.write_text("[1]", encoding="utf-8")
            self.assertIsNone(audit.load_capacity(path))

    def test_pro_burst_and_bloated_long_session_are_reported(self):
        pro = {"provider": "chatgpt-web", "model": "gpt-6-pro"}
        rows = [row(i * 30, bundle=pro) for i in range(6)]
        session = {"name": "rollout-2026-10-03T19-58-05-0001", "model": "gpt-6.1-sol", "effort": "xhigh", "calls": 2000,
                   "input": 2000 * 150_000, "cached": 2000 * 140_000, "output": 2000 * 900, "total": 3e8,
                   "peak_ctx": 240_000}
        keys = self.keys(context(rows, sessions=[session]))
        self.assertTrue({"pro", "bloat"} <= keys)
        self.assertFalse(any(k.startswith("poll-") for k in keys))
        session["output"] = 2000 * 100
        self.assertTrue(any(k.startswith("poll-") for k in self.keys(context(rows, sessions=[session]))))

    def test_unrouted_subagent_spawns_are_reported_unless_preceded_by_route(self):
        spawn_time = T0 + dt.timedelta(days=5, minutes=10)
        spawns = [{"at": spawn_time, "kind": "paseo", "label": "paseo:codex/gpt-5.4-mini", "cwd": "/workspace/demo_project"}]
        found = {f["key"]: f for f in audit.build_findings(context([], spawns=spawns))}
        self.assertIn("unrouted", found)
        self.assertIn("paseo:codex/gpt-5.4-mini", found["unrouted"]["evidence"])

        routed = [row(5 * 1440 + 5, "plan", choice="sol_high", source="deterministic_allocator")]
        self.assertNotIn("unrouted", self.keys(context(routed, spawns=spawns)))


if __name__ == "__main__":
    unittest.main()
