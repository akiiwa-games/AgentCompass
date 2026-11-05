"""Moving plugin clones out of a checkout; everything runs in temporary directories with fake processes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plugin_migration as migration

WINDOWS = os.name == "nt"


class Proc:
    def __init__(self, pid):
        self.pid = pid


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.checkout = base / "checkout"
        self.plugins = base / "home" / ".agent-compass" / "plugins"
        self.home = base / "home"
        self.plugins.mkdir(parents=True)
        for folder in ("usage-float", "codex-chatgpt-web", "gemini-web", "dot2api"):
            clone = self.checkout / folder
            (clone / ".git").mkdir(parents=True)
            (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (clone / "tracked.py").write_text("patched locally\n")
            (clone / "untracked.txt").write_text("not in git\n")
        (self.checkout / "codex-chatgpt-web" / "node_modules" / "pkg").mkdir(parents=True)
        (self.checkout / "codex-chatgpt-web" / "node_modules" / "pkg" / "index.js").write_text("x")
        (self.checkout / "dot2api" / ".venv").mkdir()
        (self.checkout / "dot2api" / ".venv" / "pyvenv.cfg").write_text("home = C:\\Python\n")
        self.running = {"chatgpt-web"}
        self.events = []

    def find(self, paths):
        for plugin_id in self.running:
            folder = migration.plugins.BY_ID[plugin_id].legacy_dir
            if any(Path(path).name in (folder, plugin_id) for path in paths):
                return [Proc(4242)]
        return []

    def stop(self, plugin_id):
        self.events.append(("stop", plugin_id))
        self.running.discard(plugin_id)
        return "stopped"

    def start(self, plugin_id):
        self.events.append(("start", plugin_id))
        return "started"

    def rebuild(self, target):
        self.events.append(("rebuild", target.name))
        return "rebuilt .venv"

    def run_migration(self, **kwargs):
        lines = []
        code = migration.migrate(self.checkout, self.plugins, home=self.home, stop=self.stop, start=self.start,
                                 find=self.find, end=lambda procs: self.events.append(("kill", len(procs))),
                                 rebuild=self.rebuild, log=lines.append, **kwargs)
        return code, "\n".join(lines)

    def test_dry_run_reports_the_plan_and_changes_nothing(self):
        code, output = self.run_migration(dry_run=True)
        self.assertEqual(code, 0)
        for plugin_id in migration.MIGRATABLE:
            self.assertIn(f"{plugin_id}: move", output)
        self.assertIn("would stop and later restart: 4242", output)
        self.assertIn("would rebuild its .venv", output)
        self.assertEqual(self.events, [])
        self.assertEqual(sorted(path.name for path in self.plugins.iterdir()), [])
        self.assertTrue((self.checkout / "dot2api" / ".git").is_dir())

    def test_clones_move_with_local_changes_and_only_running_plugins_restart(self):
        code, _ = self.run_migration()
        self.assertEqual(code, 0)
        for plugin_id in migration.MIGRATABLE:
            target = self.plugins / plugin_id
            self.assertEqual((target / "tracked.py").read_text(), "patched locally\n")
            self.assertEqual((target / "untracked.txt").read_text(), "not in git\n")
            self.assertFalse((self.checkout / migration.plugins.BY_ID[plugin_id].legacy_dir).exists())
        self.assertTrue((self.plugins / "chatgpt-web" / "node_modules" / "pkg" / "index.js").is_file())
        self.assertEqual(self.events, [("stop", "chatgpt-web"), ("start", "chatgpt-web"), ("rebuild", "dot2api")])
        marker = json.loads((self.plugins / migration.MARKER).read_text())
        self.assertEqual(Path(marker["dot2api"]["from"]), (self.checkout / "dot2api").resolve())

    def test_a_second_run_is_a_no_op(self):
        self.run_migration()
        self.events.clear()
        code, output = self.run_migration()
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [])
        for plugin_id in migration.MIGRATABLE:
            self.assertIn(f"{plugin_id}: done", output)

    def test_leftover_processes_are_ended_after_a_polite_stop(self):
        self.stop = lambda plugin_id: self.events.append(("stop", plugin_id)) or "asked"     # keeps running
        self.run_migration(ids=("chatgpt-web",))
        self.assertIn(("kill", 1), self.events)

    def test_a_different_volume_copies_verifies_then_deletes(self):
        real_rename = os.rename
        calls = []

        def rename(source, target):
            calls.append(Path(target).name)
            if not str(target).endswith(".migrating") and not str(source).endswith(".migrating"):
                error = OSError("different volume")
                error.winerror = migration.ERROR_NOT_SAME_DEVICE
                raise error
            return real_rename(source, target)

        with mock.patch.object(migration.os, "rename", side_effect=rename):
            code, output = self.run_migration(ids=("usage-float",))
        self.assertEqual(code, 0, output)
        self.assertIn("moved (copied)", output)
        self.assertEqual((self.plugins / "usage-float" / "untracked.txt").read_text(), "not in git\n")
        self.assertFalse((self.checkout / "usage-float").exists())
        self.assertFalse((self.plugins / "usage-float.migrating").exists())

    def test_a_copy_that_does_not_match_removes_nothing(self):
        def short_copy(source, destination):
            destination.mkdir(parents=True)
            (destination / ".git").mkdir()

        def rename(source, target):
            error = OSError("different volume")
            error.winerror = migration.ERROR_NOT_SAME_DEVICE
            raise error

        with mock.patch.object(migration.os, "rename", side_effect=rename), \
                mock.patch.object(migration, "copy_tree", side_effect=short_copy):
            code, output = self.run_migration(ids=("chatgpt-web",))
        self.assertEqual(code, 1)
        self.assertIn("does not match", output)
        self.assertTrue((self.checkout / "codex-chatgpt-web" / "untracked.txt").is_file())
        self.assertEqual(self.events[-1], ("start", "chatgpt-web"))      # restarted from the old place

    def test_an_interrupted_run_finishes_removing_the_old_clone(self):
        target = self.plugins / "gemini-web"
        migration.copy_tree(self.checkout / "gemini-web", target)
        migration.write_marker(self.plugins, "gemini-web", (self.checkout / "gemini-web").resolve())
        code, output = self.run_migration(ids=("gemini-web",))
        self.assertEqual(code, 0)
        self.assertIn("gemini-web: cleanup", output)
        self.assertFalse((self.checkout / "gemini-web").exists())
        self.assertTrue((target / "untracked.txt").is_file())

    def test_two_real_clones_are_a_conflict_and_both_stay(self):
        (self.plugins / "dot2api" / ".git").mkdir(parents=True)
        code, output = self.run_migration(ids=("dot2api",))
        self.assertEqual(code, 1)
        self.assertIn("dot2api: conflict", output)
        self.assertTrue((self.checkout / "dot2api" / "tracked.py").is_file())

    def test_old_paths_in_host_configs_are_reported_not_edited(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        text = f'cwd = "{(self.checkout / "codex-chatgpt-web").resolve()}"\n'.replace("\\", "\\\\")
        config.write_text(text)
        _, output = self.run_migration()
        self.assertIn(f"note: {config} still names an old checkout path", output)
        self.assertEqual(config.read_text(), text)


@unittest.skipUnless(WINDOWS, "junctions are a Windows feature")
class JunctionTests(Fixture):
    def test_a_junction_to_the_clone_is_replaced_by_the_clone(self):
        link = self.plugins / "usage-float"
        migration.make_link(link, self.checkout / "usage-float", True)
        code, output = self.run_migration(ids=("usage-float",))
        self.assertEqual(code, 0, output)
        self.assertFalse(migration.exists_link(link))
        self.assertEqual((link / "untracked.txt").read_text(), "not in git\n")
        self.assertFalse((self.checkout / "usage-float").exists())

    def test_a_failed_move_puts_the_junction_back(self):
        link = self.plugins / "chatgpt-web"
        migration.make_link(link, self.checkout / "codex-chatgpt-web", True)
        with mock.patch.object(migration, "move_tree", side_effect=OSError("file in use")):
            code, output = self.run_migration(ids=("chatgpt-web",))
        self.assertEqual(code, 1)
        self.assertIn("restored the junction", output)
        self.assertTrue(migration.exists_link(link))
        self.assertTrue((link / "untracked.txt").is_file())
        self.assertEqual(self.events[-1], ("start", "chatgpt-web"))

    def test_a_junction_to_somewhere_else_is_a_conflict(self):
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        migration.make_link(self.plugins / "gemini-web", elsewhere, True)
        code, output = self.run_migration(ids=("gemini-web",))
        self.assertEqual(code, 1)
        self.assertIn("gemini-web: conflict", output)

    def test_remove_tree_unlinks_junctions_without_entering_them(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        tree = Path(self.temp.name) / "tree"
        (tree / "deep").mkdir(parents=True)
        migration.make_link(tree / "deep" / "link", outside, True)
        migration.remove_tree(tree)
        self.assertFalse(tree.exists())
        self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_copy_re_points_internal_junctions_into_the_new_tree(self):
        source = self.checkout / "codex-chatgpt-web"
        migration.make_link(source / "node_modules" / "alias", source / "node_modules" / "pkg", True)
        destination = Path(self.temp.name) / "copy"
        migration.copy_tree(source, destination)
        self.assertTrue(migration.exists_link(destination / "node_modules" / "alias"))
        self.assertTrue(migration.same_path(migration.link_target(destination / "node_modules" / "alias"),
                                            destination / "node_modules" / "pkg"))
        self.assertEqual(migration.inventory(source), migration.inventory(destination))


if __name__ == "__main__":
    unittest.main()
