"""Hand a local file to Dot through the user's private GitHub repository, read by Dot's own GitHub connector.

    python -I -X utf8 github_send.py stage <file> <repo-clone-dir> <packet-out.md> [--id NAME] [--part-mb 3] [--per-packet 15]
                                      [--root auto|/path] [--plain] [--cleanup] [--no-push]
    python -I -X utf8 github_send.py repacket <packet-out.md.json> <packet-out.md> [--root auto|/path] [--per-packet 15] [--cleanup]
    python -I -X utf8 github_send.py check <packet-out.md.json> <reply.md> [<reply.md> ...]

Verified 2026-10-09: Dot (ChatGPT) reads the configured transfer repository through its connected GitHub connector, cannot clone it
with git (no credentials, and it refuses keys or tokens in a task), and the connector returns UTF-8 text only. Parts of
3 MB came through `fetch_blob` complete and were written by Dot's own code (48,000-character chunks, no retyping), hash
exact; a part of about 10 MB failed. `fetch_file` can silently return an empty string for a large file; `fetch_blob` with
the returned blob sha worked. The file must therefore be stored as base64 text.

`stage` encrypts <file> (openssl AES-256-CBC, PBKDF2, random passphrase; --plain skips this), base64-encodes it, splits it
into text parts of --part-mb MB under <repo-clone-dir>/<id>/, pushes them (unless --no-push; git credentials are the local
ones) and writes the packets for Dot. Up to --per-packet parts go in one packet that fetches and assembles. More parts are
split into `<stem>.fetch01.md`, `<stem>.fetch02.md` ... (independent, may run in parallel, each skips parts already present
with the right hash, so a failed one is simply sent again) and a final `<stem>.assemble.md` that verifies every part and then
joins and decrypts. A long transfer therefore never sits in one fragile task. Send them with dot-subagent, then run `check`
with every reply (the last reply that mentions a part or a hash wins).

Where the files go: --root auto (default) probes the current remote environment and verifies a writable root with a file.
Set remote DOT_WORK_ROOT or give --root /path to pin it, and reuse the reported ROOT in subsequent packets.
`repacket` rebuilds the packets from the manifest for a new root without touching GitHub. The decrypted file stays
in place unless --cleanup is given (use it for test transfers only).

The manifest (<packet-out.md>.json) holds the passphrase: treat it and the packets like the data. Git history keeps pushed
blobs (ciphertext only): when the job is over, delete the directory and, to drop the history, delete and recreate the
repository.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also support the documented python -I invocation
from remote_root import root_step as _root_step, check_root
def _default_repo() -> str:
    if os.environ.get("DOT_TRANSFER_REPO"):
        return os.environ["DOT_TRANSFER_REPO"]
    try:
        cfg = json.loads((Path.home() / ".route-manager" / "dot-plugin.json").read_text(encoding="utf-8"))
        if isinstance(cfg.get("transfer_repo"), str) and cfg["transfer_repo"].strip():
            return cfg["transfer_repo"].strip()
    except (OSError, ValueError):
        pass
    return "owner/dot-transfer"


REPO = _default_repo()
BRANCH = "main"
LINE = 76


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob_sha(data):
    """The git blob id of this content: what the connector's fetch_blob asks for."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def find_executable(name, override, candidates):
    configured = os.environ.get(override)
    if configured:
        executable = Path(configured).expanduser()
        if not executable.is_file():
            raise FileNotFoundError(f"{override} does not name an installed executable")
        return str(executable)
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(f"{name} is not installed; put it on PATH or set {override}")


def find_openssl():
    return find_executable("openssl", "ROUTE_MANAGER_OPENSSL", (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Git/usr/bin/openssl.exe",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Programs/Git/usr/bin/openssl.exe",
    ))


def encrypt(data, passphrase):
    openssl = find_openssl()
    if not openssl:
        raise OSError("openssl not found")
    descriptor, name = tempfile.mkstemp(prefix="rm-key-")
    os.close(descriptor)
    key = Path(name)
    try:
        key.write_text(passphrase, encoding="ascii")
        return subprocess.run([openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", f"file:{key}"],
                              input=data, check=True, capture_output=True).stdout
    finally:
        key.unlink(missing_ok=True)


def parts_of(payload, part_bytes):
    """Base64 text parts; every part is whole lines of LINE characters (the last may be shorter), ending in a newline."""
    text = base64.encodebytes(payload)          # 76-character lines
    lines = text.splitlines(keepends=True)
    per = max(1, part_bytes // (LINE + 1))
    return [b"".join(lines[i:i + per]) for i in range(0, len(lines), per)] or [b""]


def root_step(xfer, root):
    return "0. Discover and verify the writable work root: " + _root_step(xfer, root) + "."


def part_listing(xfer, numbered):
    return "\n".join(f"   {n}. `part_{n:03d}.b64.txt` ({p['bytes']} bytes, sha256 {p['sha256']}" + (f", blob {p['blob']})" if p.get("blob") else ")")
                     for n, p in numbered)


def fetch_steps(xfer, numbered):
    return f"""1. For each file below (in the repository under `{xfer}/`): if `$ROOT/rm-xfer-{xfer}/parts/<name>` already exists with exactly the listed size and sha256, do not fetch it again (report SKIPPED). Otherwise fetch it with the GitHub connector's `fetch_blob` using the blob sha listed (`fetch_file` has returned an empty string for every one of these files, so do not call it; if a file has no blob sha listed, list the directory to get it). If a fetch fails, try that file once more. Write the returned content to `$ROOT/rm-xfer-{xfer}/parts/<name>` with your own code, by passing the connector result to the shell in chunks of at most 48,000 characters (never retype or paste the content yourself), then report `wc -c` and `sha256sum` of each. A file that fails: report FAILED and carry on with the next one.
{part_listing(xfer, numbered)}"""


HEAD = """The user owns the private repository `{repo}` (branch `{branch}`) and connected GitHub to this account on purpose. Use only that connector; do not ask for or write any token or key. Do not estimate: run things and report exact output. Do not change the repository. Use only the writable work root verified in step 0."""

PART_LINES = "PART_<n>: <name> <bytes> <sha256> FETCHED|SKIPPED|FAILED   (one line for each file above, n as numbered)"


def packet_fetch(xfer, numbered, root):
    return f"""# Task: copy {len(numbered)} text file(s) from the user's private GitHub repository onto your computer and check them

{HEAD.format(repo=REPO, branch=BRANCH)}

{root_step(xfer, root)}
{fetch_steps(xfer, numbered)}

Reply format (exactly this):
```
EXECUTED: yes|no
ROOT: <the work root>
{PART_LINES}
NOTES: <anything unusual>
```
"""


def assemble_steps(xfer, name, passphrase, payload_sha, payload_bytes, file_sha, file_bytes, parts, cleanup, verify):
    base = f"$ROOT/rm-xfer-{xfer}"
    decrypt = (f"`printf '%s' '{passphrase}' > key.txt && openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:key.txt "
               f"-in payload.enc -out {name}; rm -f key.txt`" if passphrase else f"`mv payload.enc {name}`")
    tail = (f"Then delete `{base}` (`rm -rf`) and report `ls -d {base}`." if cleanup
            else f"Leave `{base}` in place: the file is `{base}/{name}`.")
    check_parts = f"""Check every part now: for each of the {len(parts)} parts `{base}/parts/part_NNN.b64.txt` (NNN = 001..{len(parts):03d}) report `wc -c` and `sha256sum` as a PART_<n> line, with MISSING if the file does not exist. The expected values are:
{part_listing(xfer, [(i, p) for i, p in enumerate(parts, 1)])}
If any part is missing or differs, stop and reply `EXECUTED: yes` with those PART lines and `ASSEMBLED: no`; do not continue.
Otherwise: """ if verify else ""
    return f"""{check_parts}`cd {base} && cat parts/part_*.b64.txt | base64 -d > payload.enc` (names sort in order); report `wc -c payload.enc` and `sha256sum payload.enc`. Decrypt: {decrypt}. Report `wc -c {name}` and `sha256sum {name}`. {tail}

Expected, for your comparison only: payload.enc {payload_bytes} bytes with sha256 {payload_sha}; {name} {file_bytes} bytes with sha256 {file_sha}."""


ASSEMBLE_REPLY = """Reply format (exactly this):
```
EXECUTED: yes|no
ROOT: <the work root>
PART_<n>: <name> <bytes> <sha256> OK|MISSING|DIFFERENT   (one line per part)
ASSEMBLED: yes|no
PAYLOAD_BYTES: <number>
PAYLOAD_SHA256: <hex>
FILE_NAME: {name}
FILE_BYTES: <number>
FILE_SHA256: <hex>
CLEANUP: <output or KEPT>
NOTES: <anything unusual>
```
"""


def packet_assemble(m, root, cleanup):
    parts = m["parts"]
    return f"""# Task: check {len(parts)} text file(s) already on your computer, join them and unpack one file

{HEAD.format(repo=REPO, branch=BRANCH)}

{root_step(m['id'], root)}
{assemble_steps(m['id'], m['name'], m.get('passphrase'), m['payload_sha256'], m['payload_bytes'], m['file_sha256'], m['file_bytes'], parts, cleanup, True)}

{ASSEMBLE_REPLY.format(name=m['name'])}"""


def packet_all(m, root, cleanup):
    parts = m["parts"]
    numbered = [(i, p) for i, p in enumerate(parts, 1)]
    return f"""# Task: copy {len(parts)} text file(s) from the user's private GitHub repository onto your computer, check them, and unpack one file

{HEAD.format(repo=REPO, branch=BRANCH)}

{root_step(m['id'], root)}
{fetch_steps(m['id'], numbered)}
2. If any file differs from the listed size or hash, stop and report which (reply `ASSEMBLED: no`); do not continue.
3. {assemble_steps(m['id'], m['name'], m.get('passphrase'), m['payload_sha256'], m['payload_bytes'], m['file_sha256'], m['file_bytes'], parts, cleanup, False)}

{ASSEMBLE_REPLY.format(name=m['name'])}"""


def write_packets(manifest, packet_out, root, per_packet, cleanup):
    """Write the packet files for this manifest; return their names in the order to send them."""
    packet_out = Path(packet_out)
    parts, xfer = manifest["parts"], manifest["id"]
    kw = dict(newline="\n", encoding="utf-8")
    if len(parts) <= per_packet:
        packet_out.write_text(packet_all(manifest, root, cleanup), **kw)
        return [packet_out.name]
    names = []
    stem = packet_out.with_suffix("")
    for k, start in enumerate(range(0, len(parts), per_packet), 1):
        numbered = [(i + 1, p) for i, p in enumerate(parts[start:start + per_packet], start)]
        target = Path(f"{stem}.fetch{k:02d}.md")
        target.write_text(packet_fetch(xfer, numbered, root), **kw)
        names.append(target.name)
    target = Path(f"{stem}.assemble.md")
    target.write_text(packet_assemble(manifest, root, cleanup), **kw)
    names.append(target.name)
    return names


def stage(source, repo_dir, packet_out, xfer=None, part_mb=3, plain=False, cleanup=False, push=True, root="auto", per_packet=15):
    source, repo_dir = Path(source), Path(repo_dir)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", source.name):
        raise ValueError(f"file name {source.name!r} must be plain letters, digits, _ . -")
    check_root(root)
    data = source.read_bytes()
    xfer = xfer or sha(data)[:12]
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", xfer):
        raise ValueError("transfer id must be letters, digits, _ -")
    target = repo_dir / xfer
    if target.exists():
        raise ValueError(f"{target} already exists")
    passphrase = None if plain else base64.b64encode(secrets.token_bytes(24)).decode("ascii").replace("+", "A").replace("/", "B")
    payload = data if plain else encrypt(data, passphrase)
    parts = parts_of(payload, int(part_mb * 1_000_000))
    target.mkdir(parents=True)
    for i, part in enumerate(parts, 1):
        (target / f"part_{i:03d}.b64.txt").write_bytes(part)
    manifest = {"id": xfer, "name": source.name, "encrypted": not plain, "passphrase": passphrase, "file_bytes": len(data),
                "file_sha256": sha(data), "payload_bytes": len(payload), "payload_sha256": sha(payload),
                "parts": [{"n": i, "bytes": len(p), "sha256": sha(p), "blob": blob_sha(p)} for i, p in enumerate(parts, 1)]}
    manifest["packets"] = write_packets(manifest, packet_out, root, per_packet, cleanup)
    Path(str(packet_out) + ".json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    if push:
        git = ["git", "-C", str(repo_dir)]
        subprocess.run(git + ["add", xfer], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-m", f"transfer {xfer}"], check=True, capture_output=True)
        subprocess.run(git + ["push", "origin", "HEAD"], check=True, capture_output=True)
        listed = subprocess.run(git + ["ls-tree", "HEAD", f"{xfer}/"], check=True, capture_output=True, text=True).stdout
        stored = {name: blob for blob, name in re.findall(r"blob (\w{40})\t(?:.*/)?(part_\d+\.b64\.txt)", listed)}
        if stored != {f"part_{p['n']:03d}.b64.txt": p["blob"] for p in manifest["parts"]}:
            raise ValueError("git stored different content than was staged (line-ending conversion?)")
    return manifest



def repacket(manifest_path, packet_out, root="auto", per_packet=15, cleanup=False):
    """Rewrite the packets of an already staged transfer (new root, new packet size) without touching GitHub."""
    check_root(root)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    manifest["packets"] = write_packets(manifest, packet_out, root, per_packet, cleanup)
    Path(str(packet_out) + ".json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def field(reply, name):
    match = re.search(rf"^{name}:[ \t]*(\S+)", reply.replace("\r\n", "\n"), re.M)
    return match.group(1) if match else None


def check(manifest_path, *reply_paths):
    """[(what, ok)]: every reply executed, every part reported with its size and hash, payload and file hashes match."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    texts = [Path(p).read_text(encoding="utf-8").replace("\r\n", "\n") for p in reply_paths]
    seen = {}
    for text in texts:
        for n, _name, size, digest, state in re.findall(r"^PART_(\d+):[ \t]*(\S+)[ \t]+(\d+)[ \t]+([0-9a-fA-F]{64})(?:[ \t]+(\w+))?", text, re.M):
            if state.upper() not in ("MISSING", "FAILED", "DIFFERENT"):
                seen[int(n)] = (int(size), digest.lower())
    wrong = [p["n"] for p in manifest["parts"] if seen.get(p["n"]) != (p["bytes"], p["sha256"])]

    def last(name):
        values = [v for v in (field(t, name) for t in texts) if v]
        return values[-1] if values else None

    return [("executed", bool(texts) and all(field(t, "EXECUTED") == "yes" for t in texts)),
            ("parts" + (f" (not confirmed: {wrong[:8]})" if wrong else ""), not wrong),
            ("payload", last("PAYLOAD_SHA256") == manifest["payload_sha256"] and last("PAYLOAD_BYTES") == str(manifest["payload_bytes"])),
            ("file", last("FILE_SHA256") == manifest["file_sha256"] and last("FILE_BYTES") == str(manifest["file_bytes"]))]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("stage")
    st.add_argument("source")
    st.add_argument("repo_dir")
    st.add_argument("packet_out")
    st.add_argument("--id")
    st.add_argument("--part-mb", type=float, default=3, help="size of one text part; 3 works, about 10 failed")
    st.add_argument("--per-packet", type=int, default=15, help="parts fetched by one task")
    st.add_argument("--root", default="auto")
    st.add_argument("--plain", action="store_true", help="do not encrypt (GitHub then holds the data readable)")
    st.add_argument("--cleanup", action="store_true", help="delete the files on Dot after the check (test transfers)")
    st.add_argument("--no-push", action="store_true")
    rp = sub.add_parser("repacket")
    rp.add_argument("manifest")
    rp.add_argument("packet_out")
    rp.add_argument("--root", default="auto")
    rp.add_argument("--per-packet", type=int, default=15)
    rp.add_argument("--cleanup", action="store_true")
    ck = sub.add_parser("check")
    ck.add_argument("manifest")
    ck.add_argument("replies", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command in ("stage", "repacket"):
            if args.command == "stage":
                manifest = stage(args.source, args.repo_dir, args.packet_out, args.id, args.part_mb, args.plain, args.cleanup,
                                 not args.no_push, args.root, args.per_packet)
            else:
                manifest = repacket(args.manifest, args.packet_out, args.root, args.per_packet, args.cleanup)
            print(f"{manifest['name']}: {manifest['file_bytes']} bytes, payload {manifest['payload_bytes']}, {len(manifest['parts'])} part(s), "
                  f"id {manifest['id']}, {'encrypted' if manifest['encrypted'] else 'plain'}")
            print("send in this order (the fetch packets may run in parallel; the last one only after they all succeeded):")
            for name in manifest["packets"]:
                print(f"  {Path(args.packet_out).with_name(name)}")
            return 0
        results = check(args.manifest, *args.replies)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for what, ok in results:
        print(f"{what}: {'ok' if ok else 'FAILED'}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
