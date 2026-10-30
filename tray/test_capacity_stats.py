import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import capacity_stats as cs

T0 = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600


def stamp(offset_seconds: float) -> str:
    return datetime.fromtimestamp(T0 + offset_seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def codex_call(offset, total, used=10.0, resets=T0 + 5 * 86400, tokens_in=1000, tokens_out=100, minutes=10080):
    return json.dumps({"timestamp": stamp(offset), "type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"total_tokens": total}, "last_token_usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}},
        "rate_limits": {"primary": {"used_percent": used, "window_minutes": minutes, "resets_at": resets}}}})


def turn_context(model="gpt-6.1-sol", effort="xhigh"):
    return json.dumps({"timestamp": stamp(0), "type": "turn_context", "payload": {"model": model, "effort": effort}})


def claude_call(offset, message_id, model="claude-opus-5-5", tokens_in=500, tokens_out=50):
    return json.dumps({"timestamp": stamp(offset), "message": {"id": message_id, "model": model, "usage": {
        "input_tokens": tokens_in, "cache_read_input_tokens": 0, "output_tokens": tokens_out}}})


def varint(value):
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def step_blob(seconds):
    """A step's protobuf metadata: a nested timestamp message, a later timestamp and an unrelated number."""
    inner = b"\x08" + varint(int(seconds)) + b"\x10" + varint(7)
    return b"\x0a" + varint(len(inner)) + inner + b"\x10" + varint(int(seconds) + 2) + b"\x18" + varint(300)


def make_conversation(path, offsets, models=(b"gemini-3.8-flash-high", b"gemini-3.8-flash", b"gemini-3.8-flash")):
    connection = sqlite3.connect(str(path))
    connection.execute("create table steps (idx integer primary key, metadata blob)")
    connection.execute("create table gen_metadata (idx integer primary key, data blob)")
    connection.execute("create table executor_metadata (idx integer primary key, data blob)")
    add_steps(connection, 0, offsets)
    for number, model in enumerate(models):
        connection.execute("insert into gen_metadata values (?, ?)", (number, b"\x0a" + varint(len(model)) + model))
    connection.commit()
    return connection


def add_steps(connection, first, offsets):
    for number, offset in enumerate(offsets):
        connection.execute("insert into steps values (?, ?)", (first + number, step_blob(T0 + offset)))
    connection.commit()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        activity_env = mock.patch.dict(cs.os.environ, {'AGENTCOMPASS_ACTIVITY_FILE': str(self.root / 'usage-activity.json')})
        activity_env.start()
        self.addCleanup(activity_env.stop)
        # Home overrides would point the scan at this machine's real logs.
        for key in ('PASEO_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            if key in cs.os.environ:
                cleared = mock.patch.dict(cs.os.environ)
                cleared.start()
                self.addCleanup(cleared.stop)
                del cs.os.environ[key]
        for name, value in {"HOME": self.root, "STATE_DIR": self.root, "STATE_FILE": self.root / "state.json", "OUTPUT": self.root / "capacity.json",
                            "STATS_FILE": self.root / "stats.json", "LOCK_FILE": self.root / "capacity.lock",
                            "SOURCES_FILE": self.root / "sources.json", "USAGE_CACHE": self.root / "usage-cache.json"}.items():
            patcher = mock.patch.object(cs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / ".codex" / "sessions").mkdir(parents=True)
        (self.root / ".codex" / "archived_sessions").mkdir(parents=True)
        (self.root / ".claude" / "projects" / "p").mkdir(parents=True)

    def rollout(self, name="rollout-a.jsonl", lines=(), archived=False):
        path = self.root / ".codex" / ("archived_sessions" if archived else "sessions") / name
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            for line in lines:
                stream.write(line + "\n")
        return path

    def entry(self):
        return cs.new_entry(Path("x.jsonl"), "codex")


class ScanTests(Fixture):
    def test_active_time_caps_long_gaps_and_ignores_the_first_call(self):
        path = self.rollout(lines=[turn_context(), codex_call(0, 100), codex_call(60, 200), codex_call(60 + 1800, 300)])
        entry = self.entry()
        cs.scan_codex(path, entry)
        slot = entry["buckets"][cs.hour_key(T0)]["gpt-6.1-sol/xhigh"]
        self.assertAlmostEqual(slot[0], 60 + cs.ACTIVE_GAP_SECONDS)
        self.assertEqual(slot[1], 3)

    def test_a_repeated_event_does_not_count_twice(self):
        path = self.rollout(lines=[turn_context(), codex_call(0, 100), codex_call(30, 100), codex_call(60, 250)])
        entry = self.entry()
        cs.scan_codex(path, entry)
        self.assertEqual(entry["buckets"][cs.hour_key(T0)]["gpt-6.1-sol/xhigh"][1], 2)

    def test_scanning_in_steps_equals_scanning_at_once_and_waits_for_a_whole_line(self):
        lines = [turn_context(), codex_call(0, 100), codex_call(60, 200), codex_call(120, 300), codex_call(180, 400)]
        whole = self.rollout("rollout-whole.jsonl", lines)
        once = self.entry()
        cs.scan_codex(whole, once)
        stepped_path = self.rollout("rollout-steps.jsonl", lines[:3])
        stepped = self.entry()
        cs.scan_codex(stepped_path, stepped)
        with open(stepped_path, "ab") as stream:
            stream.write((lines[3] + "\n" + lines[4]).encode())    # the last line is not finished yet
        cs.scan_codex(stepped_path, stepped)
        self.assertEqual(stepped["buckets"][cs.hour_key(T0)]["gpt-6.1-sol/xhigh"][1], 3)
        with open(stepped_path, "ab") as stream:
            stream.write(b"\n")
        cs.scan_codex(stepped_path, stepped)
        self.assertEqual(stepped["buckets"], once["buckets"])
        self.assertEqual(stepped["offset"], once["offset"])

    def test_a_truncated_file_is_read_again_from_the_start(self):
        path = self.rollout(lines=[turn_context(), codex_call(0, 100), codex_call(60, 200)])
        entry = self.entry()
        cs.scan_codex(path, entry)
        path.write_text(turn_context() + "\n" + codex_call(0, 50) + "\n", encoding="utf-8")
        cs.scan_codex(path, entry)
        self.assertEqual(entry["buckets"][cs.hour_key(T0)]["gpt-6.1-sol/xhigh"][1], 1)

    def test_rate_limit_readings_keep_changes_and_only_extend_repeats(self):
        path = self.rollout(lines=[turn_context(), codex_call(0, 100, used=10), codex_call(60, 200, used=10),
                                   codex_call(120, 300, used=11), codex_call(180, 400, used=11, minutes=300)])
        entry = self.entry()
        cs.scan_codex(path, entry)
        series = entry["samples"]["usage"]
        self.assertEqual([row[1] for row in series], [10.0, 11.0])
        self.assertEqual(series[0][3], T0 + 60)
        self.assertIn("usage-300m", entry["samples"])

    def test_a_rollout_archived_under_another_folder_counts_once(self):
        self.rollout("rollout-a.jsonl", [turn_context(), codex_call(0, 100)])
        self.rollout("rollout-a.jsonl", [turn_context(), codex_call(0, 100)], archived=True)
        state = {"schema": cs.SCHEMA, "files": {}, "usage_samples": {}}
        counts = cs.scan_all(state, T0 + 100, 14)
        self.assertEqual(counts["files"], 1)

    def test_calls_are_priced_at_their_models_list_price_with_cache_reads_cheap(self):
        opus = cs.list_usd("claude-opus-5-5", fresh=1_000_000, read=1_000_000, out=1_000_000)
        sonnet = cs.list_usd("claude-sonnet-5-5", fresh=1_000_000, read=1_000_000, out=1_000_000)
        self.assertAlmostEqual(opus, 4 + 0.2 + 20)
        self.assertAlmostEqual(opus, 2 * sonnet)
        self.assertAlmostEqual(cs.list_usd("gpt-6-astra/high", out=1_000_000), 5 * cs.list_usd("gpt-6.1-sol", out=1_000_000))
        self.assertEqual(cs.list_usd("gemini-3.8-flash-high", out=1_000_000), 0.0)     # unpriced: the token fit stays
        base = self.root / ".claude" / "projects" / "p"
        call = json.dumps({"timestamp": stamp(0), "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
            "input_tokens": 1000, "cache_creation_input_tokens": 3000, "cache_creation": {"ephemeral_1h_input_tokens": 1000},
            "cache_read_input_tokens": 100_000, "output_tokens": 500}}})
        (base / "c.jsonl").write_text(call + '\n', encoding="utf-8")
        state = {"schema": cs.SCHEMA, "files": {}, "usage_samples": {}}
        cs.scan_all(state, T0 + 1000, 14)
        slot = next(iter(next(iter(state["files"].values()))["buckets"].values()))["claude-opus-5-5"]
        self.assertEqual(slot[2], 104_000)
        self.assertAlmostEqual(slot[4], (1000 * 4 + 2000 * 5 + 1000 * 8 + 100_000 * 0.2 + 500 * 20) / 1e6)

    def test_codex_cached_input_is_priced_as_a_cache_read(self):
        line = json.dumps({"timestamp": stamp(0), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"total_tokens": 10},
                                           "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100}}}})
        self.rollout(lines=[turn_context(model="gpt-6-astra", effort="high"), line])
        state = {"schema": cs.SCHEMA, "files": {}, "usage_samples": {}}
        cs.scan_all(state, T0 + 1000, 14)
        slot = next(iter(next(iter(state["files"].values()))["buckets"].values()))["gpt-6-astra/high"]
        self.assertAlmostEqual(slot[4], (200 * 10 + 800 * 1 + 100 * 50) / 1e6)

    def test_a_state_scanned_before_prices_is_read_again_but_keeps_the_usage_samples(self):
        cs.STATE_FILE.write_text(json.dumps({"schema": cs.SCHEMA, "files": {"k": cs.new_entry(Path("x"), "claude")},
                                             "usage_samples": {"claude": {"seven_day": [[T0, 10, T0 + 86400, T0]]}}}), encoding="utf-8")
        state = cs.load_state()
        self.assertEqual(state["files"], {})
        self.assertEqual(state["usage_samples"]["claude"]["seven_day"], [[T0, 10, T0 + 86400, T0]])
        self.assertEqual(state["price_version"], cs.PRICE_VERSION)

    def test_claude_messages_are_counted_once_across_files(self):
        base = self.root / ".claude" / "projects" / "p"
        (base / "a.jsonl").write_text("\n".join([claude_call(0, "m1"), claude_call(60, "m1"), claude_call(120, "m2"),
                                                 claude_call(150, "m3", model="<synthetic>")]) + "\n", encoding="utf-8")
        (base / "b.jsonl").write_text(claude_call(200, "m2") + "\n" + claude_call(240, "m4", model="claude-sonnet-5-5") + "\n",
                                      encoding="utf-8")
        state = {"schema": cs.SCHEMA, "files": {}, "usage_samples": {}}
        cs.scan_all(state, T0 + 1000, 14)
        calls = {}
        for entry in state["files"].values():
            for bundles in entry["buckets"].values():
                for model, slot in bundles.items():
                    calls[model] = calls.get(model, 0) + slot[1]
        self.assertEqual(calls, {"claude-opus-5-5": 2, "claude-sonnet-5-5": 1})

    def test_pi_chatgpt_calls_count_as_codex_work_once_and_other_providers_are_skipped(self):
        base = self.root / ".pi" / "agent" / "sessions" / "--E--proj--"
        base.mkdir(parents=True)

        def pi_call(seconds, entry_id, provider="openai-codex"):
            return json.dumps({"type": "message", "id": entry_id, "timestamp": cs.iso(T0 + seconds), "message": {
                "role": "assistant", "provider": provider, "model": "gpt-6-astra", "timestamp": int((T0 + seconds) * 1000),
                "usage": {"input": 100, "output": 20, "cacheRead": 400, "cacheWrite": 0, "totalTokens": 520}}})
        level = json.dumps({"type": "thinking_level_change", "id": "t1", "timestamp": cs.iso(T0), "thinkingLevel": "high"})
        (base / "a.jsonl").write_text("\n".join([level, pi_call(0, "m1"), pi_call(60, "m2"), pi_call(90, "m3", "anthropic")]) + "\n",
                                      encoding="utf-8")
        (base / "fork.jsonl").write_text("\n".join([level, pi_call(0, "m1"), pi_call(120, "m4")]) + "\n", encoding="utf-8")
        state = {"schema": cs.SCHEMA, "files": {}, "usage_samples": {}}
        cs.scan_all(state, T0 + 1000, 14)
        pi_entries = [e for k, e in state["files"].items() if k.startswith("codex-pi|")]
        self.assertEqual({e["account"] for e in pi_entries}, {"codex"})
        calls = tokens = 0
        for entry in pi_entries:
            for bundles in entry["buckets"].values():
                self.assertEqual(set(bundles), {"gpt-6-astra/high"})
                calls += sum(slot[1] for slot in bundles.values())
                tokens += sum(slot[2] for slot in bundles.values())
        self.assertEqual((calls, tokens), (3, 1500))
        self.assertIn("gpt-6-astra/high", cs.usage_by_hour(state, "codex")[cs.hour_key(T0)])

    def test_extra_sources_can_be_added_from_a_file(self):
        extra = self.root / "second" / "sessions"
        extra.mkdir(parents=True)
        cs.SOURCES_FILE.write_text(json.dumps({"codex-work": {"kind": "codex", "roots": [str(extra)]}, "bad": {"kind": "x"}}),
                                   encoding="utf-8")
        found = cs.sources()
        self.assertEqual(found["codex-work"]["roots"], [str(extra)])
        self.assertNotIn("bad", found)

    def test_usage_cache_is_sampled_when_it_changes(self):
        def cache(fetched, used):
            cs.USAGE_CACHE.write_text(json.dumps({"providers": {
                "claude": {"fetched_at": stamp(fetched), "windows": [{"id": "seven_day", "used_pct": used, "resets_at": stamp(5 * 86400)}]},
                "codex": {"fetched_at": stamp(fetched), "windows": [{"id": "seven_day", "used_pct": used, "resets_at": stamp(5 * 86400)}]},
            }}), encoding="utf-8")
        state = {"schema": cs.SCHEMA, "files": {}, "usage_samples": {}}
        for fetched, used in ((0, 3.0), (300, 3.0), (600, 4.0)):
            cache(fetched, used)
            cs.sample_usage_cache(state)
        series = state["usage_samples"]["claude"]["seven_day"]
        self.assertEqual([(row[1], row[3] - row[0]) for row in series], [(3.0, 300.0), (4.0, 0.0)])
        self.assertEqual(state["usage_samples"]["codex"]["usage"], series)


class AntigravityTests(Fixture):
    def conversation(self, name="a.db", offsets=(0, 60, 60 + 1800), **kwargs):
        folder = self.root / ".gemini" / "antigravity-cli" / "conversations"
        folder.mkdir(parents=True, exist_ok=True)
        connection = make_conversation(folder / name, offsets, **kwargs)
        return folder / name, connection

    def test_steps_become_calls_gaps_over_the_cap_are_idle_and_the_suffixed_model_wins(self):
        path, connection = self.conversation()
        connection.close()
        entry = cs.new_entry(path, "antigravity")
        cs.scan_antigravity(path, entry)
        slot = entry["buckets"][cs.hour_key(T0)]["gemini-3.8-flash-high"]
        self.assertAlmostEqual(slot[0], 60 + cs.ACTIVE_GAP_SECONDS)
        self.assertEqual(slot[1], 3)
        self.assertEqual(entry["model"], "gemini-3.8-flash-high")

    def test_argon_conversation_ignores_embedded_transcript_models(self):
        huge_transcript = b"x" * 3000 + b"gemini-3.8-flash-high claude-opus-5-5" + b"y" * 3000 + b"gemini-4-argon"
        path, connection = self.conversation("argon.db", models=(b"gemini-4-argon", b"gemini-4-argon", huge_transcript))
        connection.close()
        entry = cs.new_entry(path, "antigravity")
        cs.scan_antigravity(path, entry)
        self.assertEqual(entry["model"], "gemini-4-argon")

    def test_new_steps_are_read_once_and_an_unchanged_database_is_skipped(self):
        path, connection = self.conversation(offsets=(0, 60))
        entry = cs.new_entry(path, "antigravity")
        cs.scan_antigravity(path, entry)
        add_steps(connection, 2, (120,))
        connection.close()
        cs.scan_antigravity(path, entry)
        stamp = entry["offset"]
        cs.scan_antigravity(path, entry)
        self.assertEqual(entry["offset"], stamp)
        slot = entry["buckets"][cs.hour_key(T0)]["gemini-3.8-flash-high"]
        self.assertEqual((slot[1], round(slot[0])), (3, 120))

    def test_a_database_that_cannot_be_read_records_nothing_and_is_tried_again(self):
        folder = self.root / ".gemini" / "antigravity-cli" / "conversations"
        folder.mkdir(parents=True)
        (folder / "bad.db").write_bytes(b"not a database" * 100)
        entry = cs.new_entry(folder / "bad.db", "antigravity")
        cs.scan_antigravity(folder / "bad.db", entry)
        self.assertEqual((entry["buckets"], entry["offset"]), ({}, 0))

    def test_the_default_sources_include_antigravity_and_a_recent_write_ahead_log_counts(self):
        import os
        path, connection = self.conversation()
        connection.close()
        spec = cs.sources()["antigravity"]
        old = T0 - 30 * 86400
        os.utime(path, (old, old))
        self.assertEqual(list(cs.discover(spec, T0)), [])
        Path(str(path) + "-wal").write_bytes(b"")
        self.assertEqual([found[1] for found in cs.discover(spec, T0)], [path])

    def test_daily_stats_count_agents_and_the_busiest_hours_concurrency(self):
        entries = {}
        for name, hours in (("a", {1: 0.5, 2: 0.25}), ("b", {1: 0.5})):
            entry = cs.new_entry(Path(name), "antigravity")
            for hour, worked in hours.items():
                entry["buckets"][cs.hour_key(T0 + hour * HOUR)] = {"gemini-3.8-flash-high": [worked * 3600, 5, 0, 0]}
            entries[name] = entry
        state = {"schema": cs.SCHEMA, "files": entries, "usage_samples": {}}
        stats = cs.daily_stats(state, T0 + 10 * HOUR)
        crowd = next(iter(stats["agents"].values()))["antigravity"]
        self.assertEqual(crowd, {"agents": 2, "peak_hour_concurrency": 1.0})
        hours = sum(v["worker_hours"] for day in stats["days"].values() for acc in day.values() for v in acc.values())
        self.assertAlmostEqual(hours, 1.25, places=3)


class MeasureTests(Fixture):
    def state_with(self, hours, readings, account="codex", tokens=(1000, 100)):
        entry = cs.new_entry(Path("x.jsonl"), account)
        for offset_hour, worker_hours in hours.items():
            entry["buckets"][cs.hour_key(T0 + offset_hour * HOUR)] = {"b/x": [worker_hours * 3600, 10, tokens[0], tokens[1]]}
        state = {"schema": cs.SCHEMA, "files": {"k": entry}, "usage_samples": {}}
        series = state["usage_samples"].setdefault(account, {}).setdefault("usage", [])
        for offset, used in readings:
            cs.add_reading(series, T0 + offset, used, T0 + 5 * 86400)
        return state

    def block(self, rise, hours, tokens_in, tokens_out, bundle="b/x", placeable=True, by=None):
        return {"rise": rise, "hours": hours, "tin": tokens_in, "tout": tokens_out, "placeable": placeable,
                "by": by or {bundle: [hours, tokens_in, tokens_out]}}

    def test_observed_rate_needs_a_fresh_reading_inside_the_current_period(self):
        series = []
        cs.add_reading(series, T0, 10, T0 + 86400)
        cs.add_reading(series, T0 + 2 * HOUR, 16, T0 + 86400)
        self.assertAlmostEqual(cs.rate_in(series, T0 + 2 * HOUR + 60, 2), 3.0, places=3)
        self.assertIsNone(cs.rate_in(series, T0 + 6 * HOUR, 2))               # reading is stale
        self.assertIsNone(cs.rate_in(series, T0 + 2 * HOUR + 60, 24))         # nothing 24 hours back
        self.assertIsNone(cs.rate_in(series, T0 + 2 * 86400, 2))              # the period has ended
        series[-1][3] = T0 + 6 * HOUR                                          # re-confirmed since: still current
        self.assertEqual(cs.rate_in(series, T0 + 6 * HOUR + 60, 6), 1.0)

    def test_running_workers_come_from_usage_float_activity_not_another_log_heuristic(self):
        busy = cs.new_entry(Path('a'), 'codex')
        busy['last_t'], busy['last_bundle'] = T0 + 100, 'gpt-6.1-sol/max'
        state = {'schema': cs.SCHEMA, 'files': {'a': busy}, 'usage_samples': {}}
        document = cs.measure(state, T0 + 200)
        self.assertEqual(document['active_workers'], {})
        self.assertEqual(document['activity_status'], 'unknown')
        source = cs.STATE_DIR / 'usage-activity.json'
        source.write_text(json.dumps({'schema': 'route-manager.activity.v1',
            'generated_at': cs.iso(T0 + 200), 'active_workers': {'claude-lab': {'count': 2}}}))
        document = cs.measure(state, T0 + 200)
        self.assertEqual(document['active_workers'], {'claude-lab': {'count': 2}})
        self.assertEqual(document['activity_status'], 'observed')
        self.assertEqual(cs.measure(state, T0 + 400)['activity_status'], 'unknown')

    def test_old_hours_and_readings_are_pruned(self):
        state = self.state_with({-24 * 20: 1.0, 1: 1.0}, [(-20 * 86400, 5), (0, 10)])
        cs.prune(state, T0 - 14 * 86400)
        self.assertEqual(list(state["files"]["k"]["buckets"]), [cs.hour_key(T0 + HOUR)])
        self.assertEqual(len(state["usage_samples"]["codex"]["usage"]), 1)

    def test_daily_stats_sum_hours_calls_and_tokens(self):
        state = self.state_with({1: 0.5, 2: 0.25}, [])
        stats = cs.daily_stats(state, T0 + 10 * HOUR)
        totals = [v for day in stats["days"].values() for acc in day.values() for v in acc.values()]
        self.assertAlmostEqual(sum(v["worker_hours"] for v in totals), 0.75, places=3)
        self.assertEqual(sum(v["calls"] for v in totals), 20)


class RunTests(Fixture):
    def test_refresh_writes_all_outputs_and_the_second_run_reads_nothing_new(self):
        self.rollout(lines=[turn_context(), codex_call(0, 100), codex_call(60, 200, used=11)])
        first = cs.refresh(T0 + 600)
        for path in (cs.OUTPUT, cs.STATS_FILE, cs.STATE_FILE):
            self.assertTrue(path.is_file(), path)
        self.assertEqual(first["scan"], {"files": 1, "scanned": 1})
        self.assertEqual(json.loads(cs.OUTPUT.read_text(encoding="utf-8"))["v"], cs.SCHEMA)
        second = cs.refresh(T0 + 900)
        self.assertEqual(second["scan"], {"files": 1, "scanned": 0})

    def test_no_write_leaves_the_disk_alone(self):
        self.rollout(lines=[turn_context(), codex_call(0, 100)])
        cs.refresh(T0 + 600, write=False)
        self.assertFalse(cs.OUTPUT.exists() or cs.STATE_FILE.exists())

    def test_files_older_than_the_lookback_are_not_read(self):
        path = self.rollout(lines=[turn_context(), codex_call(0, 100)])
        old = T0 - 30 * 86400
        import os
        os.utime(path, (old, old))
        document = cs.refresh(T0 + 600, write=False)
        self.assertEqual(document["scan"]["files"], 0)

    def test_lock_blocks_a_second_run_while_the_first_is_alive(self):
        self.assertTrue(cs.acquire_lock(T0))
        with mock.patch.object(cs, "pid_alive", return_value=True):
            self.assertFalse(cs.acquire_lock(T0 + 60))
            self.assertTrue(cs.acquire_lock(T0 + cs.LOCK_SECONDS + 1))      # a lock that old is abandoned
        with mock.patch.object(cs, "pid_alive", return_value=False):
            self.assertTrue(cs.acquire_lock(T0 + 60))

    def test_a_corrupt_state_file_starts_over(self):
        cs.STATE_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(cs.load_state()["files"], {})


if __name__ == "__main__":
    unittest.main()
