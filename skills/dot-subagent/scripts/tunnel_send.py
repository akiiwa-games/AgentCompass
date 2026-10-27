"""Hand a larger local file to Dot through a short-lived, single-use, encrypted download from this machine.

    python -I -X utf8 tunnel_send.py serve <file> <packet-out.md> [--ttl 1200] [--id NAME] [--keep] [--no-tunnel]

What it does: encrypts <file> with a random passphrase (openssl AES-256-CBC, PBKDF2), serves the ciphertext from a
local HTTP server on 127.0.0.1 under a random 256-bit path, opens its own Cloudflare quick tunnel to that server
(cloudflared, a different URL from Dot2API's tunnel, which is never touched), writes <packet-out.md> (the URL, the
passphrase, the expected hashes and the commands for Dot), prints READY, and waits. When the ciphertext has been
downloaded once, or after --ttl seconds, it stops the tunnel and the server and deletes the staged ciphertext.
Send the packet with dot-subagent while this runs; check the reply for the hashes it prints.

Privacy: the data leaves this machine only as ciphertext, once, to a client that knows the path. Cloudflare relays
ciphertext on this tunnel, but the passphrase travels in the packet over the Dot2API tunnel, so the protection is the
unguessable path plus the single use and the short lifetime, not the provider. The URL and passphrase are in the
packet file: treat it like the data. Only send data the user allows to reach Dot. The VM keeps the unpacked file
under /workspace/shared until a job deletes it (Dot has made that directory read-only; it is no longer a safe target, so this relay is kept only for another host); --keep leaves it for a later job.
"""

import argparse
import base64
import hashlib
import http.server
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
GIT_OPENSSL = r"C:\Program Files\Git\usr\bin\openssl.exe"
ROOT = "/workspace/shared"
URL_PATTERN = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def find_openssl():
    return shutil.which("openssl") or (GIT_OPENSSL if Path(GIT_OPENSSL).exists() else None)


def encrypt(source, target, passphrase, openssl=None):
    openssl = openssl or find_openssl()
    if not openssl:
        raise OSError("openssl not found")
    descriptor, name = tempfile.mkstemp(prefix="rm-key-")
    os.close(descriptor)
    keyfile = Path(name)
    try:
        keyfile.write_text(passphrase, encoding="ascii")
        subprocess.run([openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", f"file:{keyfile}",
                        "-in", str(source), "-out", str(target)], check=True, capture_output=True)
    finally:
        keyfile.unlink(missing_ok=True)


class OneShot(http.server.ThreadingHTTPServer):
    """Serves one file at one secret path; remembers when it was downloaded completely."""
    daemon_threads = True

    def __init__(self, path_token, name, blob_path):
        super().__init__(("127.0.0.1", 0), self.handler_class())
        self.secret = f"/{path_token}/{name}"
        self.blob_path = Path(blob_path)
        self.done = threading.Event()
        self.served = 0

    @staticmethod
    def handler_class():
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != self.server.secret or self.server.done.is_set():
                    self.send_error(404)
                    return
                size = self.server.blob_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    with open(self.server.blob_path, "rb") as stream:
                        while chunk := stream.read(1 << 20):
                            self.wfile.write(chunk)
                    self.server.served += 1
                    self.server.done.set()
                except OSError:
                    pass          # the client went away mid-download; it may try again until the ttl

        return Handler


def start_tunnel(port, timeout=60):
    """(process, public URL) of a new quick tunnel to 127.0.0.1:port."""
    descriptor, name = tempfile.mkstemp(prefix="rm-tunnel-", suffix=".log")
    os.close(descriptor)
    log = Path(name)
    handle = open(log, "wb")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                               stdout=handle, stderr=handle, creationflags=flags)
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = URL_PATTERN.search(log.read_text(encoding="utf-8", errors="replace"))
        if match:
            time.sleep(3)         # the hostname answers a moment after it is printed
            return process, match.group(0), handle, log
        if process.poll() is not None:
            break
        time.sleep(0.5)
    process.kill()
    raise OSError("the tunnel did not start")


def packet_text(url, passphrase, xfer, name, cipher_bytes, cipher_sha, plain_bytes, plain_sha, keep):
    cleanup = "Leave the directory in place; a later job will use the file." if keep else \
        f"Then delete `{ROOT}/rm-xfer-{xfer}` (`rm -rf`) and report `ls -d {ROOT}/rm-xfer-{xfer}`."
    return f"""# Task: download one file from a URL the user controls, decrypt it, and report its exact hash

The user sent this on purpose; it is their own data. Use the shell on your own computer (virtual machine). Do not estimate: run the commands and copy their output. Do not modify or interpret the data. The download works once and the URL stops answering after that, so do not repeat the download; if it fails, report the exact error. Do not use any other URL.

1. `mkdir -p {ROOT}/rm-xfer-{xfer} && cd {ROOT}/rm-xfer-{xfer}`.
2. `curl -sS -m 900 -o {name}.enc -w '%{{http_code}} %{{size_download}} %{{time_total}}s\\n' {url}` and report that line, then `wc -c {name}.enc` and `sha256sum {name}.enc`.
3. If the size and hash differ from the expected ones below, stop and report; do not decrypt.
4. Decrypt: `printf '%s' '{passphrase}' > key.txt && openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:key.txt -in {name}.enc -out {name}; rm -f key.txt`. Report `wc -c {name}` and `sha256sum {name}`. {cleanup}

Expected, for your comparison only: ciphertext {cipher_bytes} bytes with sha256 {cipher_sha}; file {plain_bytes} bytes with sha256 {plain_sha}.

Reply format (exactly this):
```
EXECUTED: yes|no
DOWNLOAD: <the curl line>
CIPHER_BYTES: <number>
CIPHER_SHA256: <hex>
FILE_NAME: {name}
FILE_BYTES: <number>
FILE_SHA256: <hex>
CLEANUP: <output or KEPT>
NOTES: <anything unusual>
```
"""


def serve(source, packet_out, ttl=1200, xfer=None, keep=False, tunnel=True):
    source = Path(source)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", source.name):
        raise ValueError(f"file name {source.name!r} must be plain letters, digits, _ . -")
    plain = source.read_bytes()
    xfer = xfer or sha(plain)[:12]
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", xfer):
        raise ValueError("transfer id must be letters, digits, _ -")
    passphrase = base64.b64encode(secrets.token_bytes(24)).decode("ascii").replace("+", "A").replace("/", "B")
    stage = Path(tempfile.mkdtemp(prefix="rm-tunnel-send-"))
    process = handle = log = server = None
    try:
        blob = stage / (source.name + ".enc")
        encrypt(source, blob, passphrase)
        cipher = blob.read_bytes()
        server = OneShot(secrets.token_urlsafe(32), source.name + ".enc", blob)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        if tunnel:
            process, base, handle, log = start_tunnel(port)
        else:
            base = f"http://127.0.0.1:{port}"
        url = base + server.secret
        Path(packet_out).write_text(packet_text(url, passphrase, xfer, source.name, len(cipher), sha(cipher), len(plain), sha(plain), keep),
                                    encoding="utf-8", newline="\n")
        print(f"READY ciphertext {len(cipher)} bytes sha256 {sha(cipher)}; file {len(plain)} bytes sha256 {sha(plain)}; packet {packet_out}", flush=True)
        fetched = server.done.wait(ttl)
        print("DOWNLOADED" if fetched else "EXPIRED (nothing was downloaded)", flush=True)
        return fetched
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if process is not None:
            process.kill()
            process.wait()
        if handle is not None:
            handle.close()
        if log is not None:
            log.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("source")
    sv.add_argument("packet_out")
    sv.add_argument("--ttl", type=int, default=1200)
    sv.add_argument("--id")
    sv.add_argument("--keep", action="store_true")
    sv.add_argument("--no-tunnel", action="store_true", help="serve on loopback only (tests)")
    args = parser.parse_args(argv)
    try:
        fetched = serve(args.source, args.packet_out, args.ttl, args.id, args.keep, not args.no_tunnel)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0 if fetched else 1


if __name__ == "__main__":
    raise SystemExit(main())
