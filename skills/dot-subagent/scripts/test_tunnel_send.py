import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import tunnel_send


class TunnelSendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.source = self.base / "table.bin"
        self.source.write_bytes(bytes(range(256)) * 400)
        self.packet = self.base / "packet.md"

    def run_serve(self, **kwargs):
        result = {}
        thread = threading.Thread(target=lambda: result.update(fetched=tunnel_send.serve(self.source, self.packet, tunnel=False, **kwargs)))
        thread.start()
        deadline = time.time() + 30
        while not self.packet.exists() and time.time() < deadline:
            time.sleep(0.05)
        return thread, result

    def test_the_file_is_served_once_encrypted_and_the_packet_lets_it_be_decrypted(self):
        if not tunnel_send.find_openssl():
            self.skipTest("no openssl")
        thread, result = self.run_serve(ttl=30)
        text = self.packet.read_text(encoding="utf-8")
        url = re.search(r"http://127\.0\.0\.1:\d+/[^\s`]+", text).group(0)
        passphrase = re.search(r"printf '%s' '([^']+)'", text).group(1)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url + "x")
        self.assertEqual(caught.exception.code, 404)
        blob = urllib.request.urlopen(url).read()
        thread.join(10)
        self.assertTrue(result["fetched"])
        self.assertNotIn(b"\x00\x01\x02\x03", blob)
        cipher_sha = re.search(r"ciphertext \d+ bytes with sha256 ([0-9a-f]{64})", text).group(1)
        self.assertEqual(hashlib.sha256(blob).hexdigest(), cipher_sha)
        encrypted = self.base / "t.enc"
        encrypted.write_bytes(blob)
        key = self.base / "key.txt"
        key.write_text(passphrase, encoding="ascii")
        back = subprocess.run([tunnel_send.find_openssl(), "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", f"file:{key}",
                               "-in", str(encrypted)], check=True, capture_output=True).stdout
        self.assertEqual(back, self.source.read_bytes())

    def test_the_url_stops_answering_after_one_download(self):
        if not tunnel_send.find_openssl():
            self.skipTest("no openssl")
        thread, result = self.run_serve(ttl=30)
        url = re.search(r"http://127\.0\.0\.1:\d+/[^\s`]+", self.packet.read_text(encoding="utf-8")).group(0)
        urllib.request.urlopen(url).read()
        thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_an_unused_transfer_expires_and_unsafe_names_are_refused(self):
        if not tunnel_send.find_openssl():
            self.skipTest("no openssl")
        thread, result = self.run_serve(ttl=1)
        thread.join(15)
        self.assertFalse(result["fetched"])
        bad = self.base / "my file.bin"
        bad.write_bytes(b"x")
        with self.assertRaises(ValueError):
            tunnel_send.serve(bad, self.packet, tunnel=False)
        with self.assertRaises(ValueError):
            tunnel_send.serve(self.source, self.packet, xfer="a/b", tunnel=False)


if __name__ == "__main__":
    unittest.main()
