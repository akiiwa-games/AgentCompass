import base64
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile
import unittest

import github_send


def simulate_dot(repo_dir, xfer, packet_text):
    """Join the parts and decrypt the way the packet tells Dot to."""
    parts = sorted((Path(repo_dir) / xfer).glob("part_*.b64.txt"))
    payload = base64.b64decode(b"".join(p.read_bytes() for p in parts))
    match = re.search(r"printf '%s' '([^']+)'", packet_text)
    if not match:
        return payload
    with tempfile.TemporaryDirectory() as scratch:
        key = Path(scratch) / "key.txt"
        key.write_text(match.group(1), encoding="ascii")
        return subprocess.run([github_send.find_openssl(), "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", f"file:{key}"],
                              input=payload, check=True, capture_output=True).stdout


def reply(manifest, lines_for=None, executed="yes", with_file=True):
    """What a faithful Dot would answer: a PART line for each part asked, then the hashes."""
    wanted = lines_for if lines_for is not None else [p["n"] for p in manifest["parts"]]
    text = f"EXECUTED: {executed}\r\nROOT: /workspace/scratch/abc\r\n"
    for p in manifest["parts"]:
        if p["n"] in wanted:
            text += f"PART_{p['n']}: part_{p['n']:03d}.b64.txt {p['bytes']} {p['sha256']} FETCHED\r\n"
    if with_file:
        text += (f"PAYLOAD_BYTES: {manifest['payload_bytes']}\r\nPAYLOAD_SHA256: {manifest['payload_sha256']}\r\n"
                 f"FILE_BYTES: {manifest['file_bytes']}\r\nFILE_SHA256: {manifest['file_sha256']}\r\n")
    return text


class GithubSendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        rng = random.Random(3)
        self.data = bytes(rng.randrange(256) for _ in range(30000)) + b"xyz" * 20000
        self.source = self.base / "table.bin"
        self.source.write_bytes(self.data)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.packet = self.base / "packet.md"

    def write(self, name, text):
        path = self.base / name
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_parts_are_whole_ascii_lines_and_rebuild_the_original_encrypted_or_plain(self):
        for plain in (True, False):
            if not plain and not github_send.find_openssl():
                continue
            manifest = github_send.stage(self.source, self.repo, self.packet, xfer=f"t{int(plain)}", part_mb=0.02, plain=plain, push=False)
            self.assertGreater(len(manifest["parts"]), 1)
            for part in (self.repo / manifest["id"]).glob("part_*.b64.txt"):
                body = part.read_bytes()
                self.assertTrue(body.endswith(b"\n"))
                self.assertTrue(re.fullmatch(rb"[A-Za-z0-9+/=\n]*", body))
                self.assertTrue(all(len(line) <= 76 for line in body.splitlines()))
            packet = "\n".join((self.base / n).read_text(encoding="utf-8") for n in manifest["packets"])
            rebuilt = simulate_dot(self.repo, manifest["id"], packet)
            self.assertEqual(rebuilt, self.data)
            self.assertEqual(hashlib.sha256(rebuilt).hexdigest(), manifest["file_sha256"])

    def test_the_packet_lists_every_part_hash_and_never_asks_for_credentials(self):
        manifest = github_send.stage(self.source, self.repo, self.packet, xfer="job", part_mb=0.02, per_packet=99, plain=True, push=False)
        self.assertEqual(manifest["packets"], ["packet.md"])
        text = self.packet.read_text(encoding="utf-8")
        for part in manifest["parts"]:
            self.assertIn(part["sha256"], text)
        self.assertIn("fetch_blob", text)
        self.assertIn("do not ask for or write any token or key", text)
        self.assertNotIn("key.txt", text)               # plain: no passphrase step

    def test_every_part_is_listed_with_its_git_blob_id_so_dot_can_skip_fetch_file(self):
        manifest = github_send.stage(self.source, self.repo, self.packet, xfer="blob", part_mb=0.02, per_packet=99, plain=True, push=False)
        text = self.packet.read_text(encoding="utf-8")
        for part in manifest["parts"]:
            body = (self.repo / "blob" / f"part_{part['n']:03d}.b64.txt").read_bytes()
            self.assertEqual(part["blob"], subprocess.run(["git", "hash-object", "--stdin"], input=body, capture_output=True, check=True).stdout.decode().strip())
            self.assertIn(f"blob {part['blob']}", text)
        self.assertIn("do not call it", text)

    def test_a_real_push_is_checked_against_the_blob_ids_git_stored(self):
        remote, clone = self.base / "remote.git", self.base / "clone"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
        for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("core.autocrlf", "true")):
            subprocess.run(["git", "-C", str(clone), "config", key, value], check=True, capture_output=True)
        manifest = github_send.stage(self.source, clone, self.packet, xfer="pushed", part_mb=0.02, plain=True)
        listed = subprocess.run(["git", "-C", str(remote), "ls-tree", "-r", "main"], check=True, capture_output=True, text=True).stdout
        for part in manifest["parts"]:
            self.assertIn(part["blob"], listed)

    def test_the_default_root_is_verified_on_dots_side_without_a_legacy_fallback(self):
        github_send.stage(self.source, self.repo, self.packet, xfer="auto", plain=True, push=False)
        text = self.packet.read_text(encoding="utf-8")
        self.assertIn("DOT_WORK_ROOT", text)
        self.assertIn("TemporaryFile", text)
        self.assertNotIn("/workspace/shared", text)
        self.assertIn("ROOT:", text)
        self.assertIn("Leave `$ROOT/rm-xfer-auto` in place", text)       # the unpacked file is kept by default

    def test_a_pinned_root_is_used_literally_and_a_bad_one_is_refused(self):
        github_send.stage(self.source, self.repo, self.packet, xfer="pin", plain=True, push=False, root="/workspace/scratch/example-root")
        text = self.packet.read_text(encoding="utf-8")
        self.assertIn("ROOT=/workspace/scratch/example-root &&", text)
        self.assertNotIn("/workspace/scratch/*/", text)
        for bad in ("relative/dir", "/a b", "/x;rm -rf /"):
            with self.assertRaises(ValueError):
                github_send.stage(self.source, self.repo, self.packet, xfer="bad" + str(len(bad)), plain=True, push=False, root=bad)

    def test_cleanup_is_only_in_the_packet_when_asked_for(self):
        github_send.stage(self.source, self.repo, self.packet, xfer="keep", plain=True, push=False)
        self.assertNotIn("rm -rf", self.packet.read_text(encoding="utf-8"))
        github_send.stage(self.source, self.repo, self.packet, xfer="wipe", plain=True, push=False, cleanup=True)
        self.assertIn("(`rm -rf`)", self.packet.read_text(encoding="utf-8"))

    def test_a_long_transfer_is_split_into_independent_fetch_packets_and_one_assemble_packet(self):
        manifest = github_send.stage(self.source, self.repo, self.packet, xfer="long", part_mb=0.01, per_packet=2, plain=True, push=False)
        count = len(manifest["parts"])
        self.assertGreater(count, 4)
        fetches = [n for n in manifest["packets"] if ".fetch" in n]
        self.assertEqual(len(fetches), -(-count // 2))
        self.assertEqual(manifest["packets"][-1], "packet.assemble.md")
        seen = []
        for name in fetches:
            text = (self.base / name).read_text(encoding="utf-8")
            self.assertIn("already exists with exactly the listed size and sha256", text)     # resumable
            self.assertNotIn("payload.enc", text)                                              # fetch packets never assemble
            seen += [int(n) for n in re.findall(r"^   (\d+)\. `part_", text, re.M)]
        self.assertEqual(seen, list(range(1, count + 1)))
        assemble = (self.base / "packet.assemble.md").read_text(encoding="utf-8")
        self.assertIn("payload.enc", assemble)
        self.assertNotIn("fetch_blob", assemble)
        self.assertIn("ASSEMBLED: no", assemble)
        for part in manifest["parts"]:
            self.assertIn(part["sha256"], assemble)

    def test_repacket_changes_the_root_without_restaging_and_keeps_the_passphrase_working(self):
        if not github_send.find_openssl():
            self.skipTest("no openssl")
        manifest = github_send.stage(self.source, self.repo, self.packet, xfer="again", part_mb=0.02, per_packet=99, push=False)
        old = self.packet.read_text(encoding="utf-8")
        again = self.base / "again.md"
        github_send.repacket(str(self.packet) + ".json", again, root="/workspace/scratch/zzz")
        new = again.read_text(encoding="utf-8")
        self.assertIn("ROOT=/workspace/scratch/zzz &&", new)
        self.assertNotEqual(old, new)
        self.assertEqual(re.search(r"printf '%s' '([^']+)'", old).group(1), re.search(r"printf '%s' '([^']+)'", new).group(1))
        self.assertEqual(simulate_dot(self.repo, "again", new), self.data)
        self.assertEqual(json.loads((self.base / "again.md.json").read_text(encoding="utf-8"))["file_sha256"], manifest["file_sha256"])

    def test_check_needs_every_part_confirmed_across_all_replies_and_the_final_hashes(self):
        manifest = github_send.stage(self.source, self.repo, self.packet, xfer="chk", part_mb=0.01, per_packet=2, plain=True, push=False)
        numbers = [p["n"] for p in manifest["parts"]]
        path = str(self.packet) + ".json"
        replies = [self.write(f"r{k}.md", reply(manifest, numbers[k * 2:k * 2 + 2], with_file=False)) for k in range(-(-len(numbers) // 2))]
        final = self.write("final.md", reply(manifest, numbers))
        self.assertTrue(all(ok for _, ok in github_send.check(path, *replies, final)))
        self.assertFalse(all(ok for _, ok in github_send.check(path, *replies)))               # no payload / file hashes yet
        self.assertFalse(all(ok for _, ok in github_send.check(path, replies[0], final.with_name("r1.md"))))     # parts 5.. unconfirmed

    def test_check_rejects_failed_parts_wrong_hashes_and_unexecuted_replies(self):
        manifest = github_send.stage(self.source, self.repo, self.packet, xfer="bad", plain=True, push=False)
        path = str(self.packet) + ".json"
        good = reply(manifest)
        self.assertTrue(all(ok for _, ok in github_send.check(path, self.write("good.md", good))))
        wrong_hash = good.replace(manifest["file_sha256"], "0" * 64)
        self.assertFalse(all(ok for _, ok in github_send.check(path, self.write("w.md", wrong_hash))))
        failed = good.replace("FETCHED", "FAILED")
        self.assertFalse(all(ok for _, ok in github_send.check(path, self.write("f.md", failed))))
        self.assertFalse(all(ok for _, ok in github_send.check(path, self.write("n.md", reply(manifest, executed="no")))))

    def test_unsafe_names_and_an_existing_transfer_are_refused(self):
        bad = self.base / "my file.bin"
        bad.write_bytes(b"x")
        with self.assertRaises(ValueError):
            github_send.stage(bad, self.repo, self.packet, push=False)
        with self.assertRaises(ValueError):
            github_send.stage(self.source, self.repo, self.packet, xfer="a/b", push=False)
        github_send.stage(self.source, self.repo, self.packet, xfer="dup", plain=True, push=False)
        with self.assertRaises(ValueError):
            github_send.stage(self.source, self.repo, self.packet, xfer="dup", plain=True, push=False)


if __name__ == "__main__":
    unittest.main()
