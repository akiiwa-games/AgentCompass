import base64
import gzip
import hashlib
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

import github_return


def dot_side(data, name, use_gzip=True):
    """What the recipe makes Dot do: gzip, base64 with 76-character lines, parts of at most 5200 lines."""
    payload = gzip.compress(data, 9) if use_gzip else data
    lines = base64.encodebytes(payload).splitlines(keepends=True)
    parts = [b"".join(lines[i:i + github_return.LINES_PER_PART]) for i in range(0, len(lines), github_return.LINES_PER_PART)]
    files = {f"returns/job1/{name}.part{n:03d}.b64.txt": part for n, part in enumerate(parts, 1)}
    line = f"FILE {name} {len(data)} {hashlib.sha256(data).hexdigest()} parts={len(parts)} gzip={'yes' if use_gzip else 'no'}"
    return files, line


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        rng = random.Random(5)
        self.noise = bytes(rng.randrange(256) for _ in range(700_000))        # incompressible: several parts
        self.text = b"open_time,close\n" + b"1,2\n" * 50_000

    def reply(self, *lines):
        path = self.base / "reply.md"
        path.write_text("EXECUTED: yes\r\nJOB: job1\r\n" + "\r\n".join(lines) + "\r\nNOTES: none\r\n", encoding="utf-8", newline="")
        return path

    def test_several_files_in_several_parts_come_back_exact_with_and_without_gzip(self):
        store = {}
        lines = []
        for name, data, use_gzip in (("noise.bin", self.noise, True), ("table.csv", self.text, True), ("raw.bin", b"abc" * 100, False)):
            files, line = dot_side(data, name, use_gzip)
            store.update(files)
            lines.append(line)
        self.assertGreater(sum(1 for key in store if key.startswith("returns/job1/noise.bin")), 1)
        out = self.base / "out"
        results = github_return.fetch(self.reply(*lines), out, get=lambda repo, branch, path: store[path])
        self.assertTrue(all(ok for _, _, ok in results))
        self.assertEqual((out / "noise.bin").read_bytes(), self.noise)
        self.assertEqual((out / "table.csv").read_bytes(), self.text)
        self.assertEqual((out / "raw.bin").read_bytes(), b"abc" * 100)

    def test_a_part_that_was_changed_or_a_wrong_reported_hash_is_not_written(self):
        files, line = dot_side(self.text, "table.csv")
        key = "returns/job1/table.csv.part001.b64.txt"
        files[key] = files[key][:-80] + b"A" * 76 + b"\n"
        out = self.base / "out1"
        results = github_return.fetch(self.reply(line), out, get=lambda repo, branch, path: files[path])
        self.assertFalse(results[0][2])
        self.assertFalse((out / "table.csv").exists())
        files, line = dot_side(self.text, "table.csv")
        out2 = self.base / "out2"
        bad = line.replace(hashlib.sha256(self.text).hexdigest(), "0" * 64)
        self.assertFalse(github_return.fetch(self.reply(bad), out2, get=lambda repo, branch, path: files[path])[0][2])

    def test_unsafe_names_a_missing_job_no_lines_and_a_busy_directory_are_refused(self):
        files, line = dot_side(b"x", "ok.bin")
        get = lambda repo, branch, path: files[path]
        with self.assertRaises(ValueError):
            github_return.fetch(self.reply("FILE ../evil 1 " + "0" * 64 + " parts=1 gzip=no"), self.base / "o1", get=get)
        with self.assertRaises(ValueError):
            github_return.fetch(self.reply("no file lines here"), self.base / "o2", get=get)
        path = self.base / "nojob.md"
        path.write_text(line + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            github_return.fetch(path, self.base / "o3", get=get)
        busy = self.base / "o4"
        busy.mkdir()
        (busy / "x").write_text("x")
        with self.assertRaises(ValueError):
            github_return.fetch(self.reply(line), busy, get=get)

    def test_the_recipe_names_the_branch_path_limits_and_reply_line(self):
        text = github_return.recipe("job1")
        for needle in ("branch `dot-returns`", "returns/job1/F.partNNN.b64.txt", "5200 lines", "never touch `main`",
                       "never typed or pasted", "60 seconds", "gzip=yes", "do not ask for or write any token or key"):
            self.assertIn(needle, text)
        self.assertIn("gzip=no", github_return.recipe("job1", use_gzip=False))
        with self.assertRaises(ValueError):
            github_return.recipe("a b")

    def test_purge_removes_the_job_directory_from_the_branch(self):
        remote, clone = self.base / "remote.git", self.base / "clone"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
        for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
            subprocess.run(["git", "-C", str(clone), "config", key, value], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "commit", "--allow-empty", "-q", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "dot-returns"], check=True, capture_output=True)
        for job in ("job1", "job2"):
            (clone / "returns" / job).mkdir(parents=True)
            (clone / "returns" / job / "f.part001.b64.txt").write_text("QQ==\n")
        subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "returns"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "dot-returns"], check=True, capture_output=True)
        github_return.purge(clone, "job1")
        listed = subprocess.run(["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "dot-returns"], check=True, capture_output=True, text=True).stdout
        self.assertNotIn("returns/job1", listed)
        self.assertIn("returns/job2/f.part001.b64.txt", listed)


if __name__ == "__main__":
    unittest.main()
