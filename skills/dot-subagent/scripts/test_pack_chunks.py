import base64
import hashlib
import json
from pathlib import Path
import random
import re
import tempfile
import unittest
import zlib

import pack_chunks


def simulate_dot(out):
    """Rebuild the file the way the part and assemble packets tell Dot to, from the packet texts alone."""
    payload = b""
    for packet in sorted(Path(out).glob("part_*.md")):
        text = packet.read_text(encoding="utf-8")
        b64 = text.split("BEGIN_B64\n", 1)[1].rsplit("END_B64", 1)[0]
        payload += base64.b64decode(b64)
    return payload


class PackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        rng = random.Random(7)
        self.data = bytes(rng.randrange(256) for _ in range(5000)) + b"abc" * 40000
        self.source = self.base / "table.bin"
        self.source.write_bytes(self.data)

    def test_parts_rejoin_to_the_payload_and_unpack_to_the_original_for_every_codec(self):
        for codec in pack_chunks.CODECS:
            out = self.base / f"out_{codec}"
            manifest = pack_chunks.pack(self.source, out, chunk_bytes=3000, codec=codec)
            payload = simulate_dot(out)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), manifest["payload_sha256"], codec)
            self.assertEqual(len(payload), manifest["payload_bytes"], codec)
            if codec == "zlib":
                self.assertEqual(zlib.decompress(payload), self.data)
            if codec == "none":
                self.assertEqual(payload, self.data)
                self.assertGreater(len(manifest["parts"]), 1)

    def test_each_packet_is_self_contained_and_names_its_own_directory_and_part(self):
        out = self.base / "out"
        manifest = pack_chunks.pack(self.source, out, chunk_bytes=3000, codec="none", xfer="job1")
        first = (out / "part_001.md").read_text(encoding="utf-8")
        self.assertIn("$ROOT/rm-xfer-job1/part_001.b64", first)
        self.assertIn("DOT_WORK_ROOT", first)
        self.assertIn("TemporaryFile", (out / "assemble.md").read_text(encoding="utf-8"))
        self.assertIn(f"PART: 1/{len(manifest['parts'])}", first)
        assemble = (out / "assemble.md").read_text(encoding="utf-8")
        self.assertIn(manifest["original_sha256"], assemble)
        self.assertIn("rm -rf", assemble)
        kept = pack_chunks.pack(self.source, self.base / "kept", chunk_bytes=3000, codec="none", delete_after=False)
        self.assertNotIn("rm -rf", (self.base / "kept" / "assemble.md").read_text(encoding="utf-8"))
        self.assertEqual(kept["original_sha256"], manifest["original_sha256"])

    def test_unsafe_names_a_busy_directory_and_bad_options_are_refused(self):
        bad = self.base / "my file.bin"
        bad.write_bytes(b"x")
        with self.assertRaises(ValueError):
            pack_chunks.pack(bad, self.base / "o1")
        with self.assertRaises(ValueError):
            pack_chunks.pack(self.source, self.base / "o2", xfer="a/b")
        with self.assertRaises(ValueError):
            pack_chunks.pack(self.source, self.base / "o3", codec="gzip")
        busy = self.base / "o4"
        busy.mkdir()
        (busy / "x").write_text("mine")
        with self.assertRaises(ValueError):
            pack_chunks.pack(self.source, busy)

    def test_check_accepts_matching_replies_and_flags_missing_or_wrong_ones(self):
        out = self.base / "out"
        manifest = pack_chunks.pack(self.source, out, chunk_bytes=20000, codec="zlib")
        replies = self.base / "replies"
        replies.mkdir()
        for part in manifest["parts"]:
            (replies / f"reply_part_{part['n']:03d}.md").write_text(
                f"EXECUTED: yes\r\nPART: {part['n']}/{len(manifest['parts'])}\r\nBIN_BYTES: {part['bytes']}\r\nBIN_SHA256: {part['sha256']}\r\n", encoding="utf-8")
        self.assertFalse(all(ok for _, ok, _ in pack_chunks.check(out, replies)))   # no assemble reply yet
        (replies / "reply_assemble.md").write_text(
            f"EXECUTED: yes\nPAYLOAD_SHA256: {manifest['payload_sha256']}\nFILE_BYTES: {manifest['original_bytes']}\n"
            f"FILE_SHA256: {manifest['original_sha256']}\n", encoding="utf-8")
        self.assertTrue(all(ok for _, ok, _ in pack_chunks.check(out, replies)))
        (replies / "reply_part_001.md").write_text("EXECUTED: yes\nBIN_BYTES: 1\nBIN_SHA256: " + "0" * 64 + "\n", encoding="utf-8")
        results = dict((what, ok) for what, ok, _ in pack_chunks.check(out, replies))
        self.assertFalse(results["part 1"])
        self.assertEqual(pack_chunks.main(["check", str(out), str(replies)]), 1)

    def test_the_packets_survive_the_unpack_files_style_of_text_handling(self):
        out = self.base / "out"
        pack_chunks.pack(self.source, out, chunk_bytes=36000, codec="zlib")
        for packet in out.glob("part_*.md"):
            text = packet.read_text(encoding="utf-8")
            body = text.split("BEGIN_B64\n", 1)[1].rsplit("END_B64", 1)[0]
            self.assertTrue(all(len(line) <= 76 for line in body.splitlines()))
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9+/=\n]*", body))


if __name__ == "__main__":
    unittest.main()
