"""Split a local file into Dot-sized packets so it can be sent over the text-only channel and rebuilt on Dot's disk.

    python -I -X utf8 pack_chunks.py pack  <input-file> <out-dir> [--chunk-bytes 36000] [--codec zlib|lzma|none] [--id NAME] [--keep] [--root auto|/path]
    python -I -X utf8 pack_chunks.py check <out-dir> <reply-dir>

`pack` writes part_001.md ... part_NNN.md (each self-contained: it tells Dot to write one base64 part under
<ROOT>/rm-xfer-<id>/ (ROOT = a verified writable directory on the remote host, or --root), decode it and report its size and sha256), assemble.md (join the parts, check the
whole, decompress, report the original's sha256) and manifest.json (the expected hashes, kept locally). Send the
part packets with dot-subagent in any order or in parallel; send assemble.md after all of them returned. `check`
compares the replies (reply_part_001.md ..., reply_assemble.md in <reply-dir>) with the manifest and exits 0 only
if every size and hash matched.

Verified 2026-10-09: a 36000-byte part (48.6k characters) travels exactly. Keep parts at or below about 45000 bytes.
The files stay on Dot's disk until a later packet deletes them (they can also vanish: see dot-subagent Long jobs), so
use only data the user allows there, and have the final job delete the directory.
"""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also support the documented python -I invocation
from remote_root import root_step, check_root
ROOT = "$ROOT"        # the shell variable set by root_step in every packet


CODECS = ("zlib", "lzma", "none")


def compress(data, codec):
    if codec == "zlib":
        return zlib.compress(data, 9)
    if codec == "lzma":
        import lzma
        return lzma.compress(data, preset=9 | lzma.PRESET_EXTREME)
    return data


def sha(data):
    return hashlib.sha256(data).hexdigest()


def part_packet(xfer, number, total, b64, expected_bytes, expected_sha, root="auto"):
    return f"""# Task: receive part {number} of {total} of a file sent as base64 text and report its exact hash

Nothing sensitive beyond what the user chose to send. Use the shell on your own computer (virtual machine). Do not estimate: run the commands and copy their output. Do not modify, repair or interpret the data. If you cannot run the commands, reply `NOT EXECUTED` and say why.

1. Set the work root and create the directory: {root_step(xfer, root)}.
2. Write exactly the lines between `BEGIN_B64` and `END_B64` (every line, nothing added) to `{ROOT}/rm-xfer-{xfer}/part_{number:03d}.b64`.
3. Decode: `base64 -d {ROOT}/rm-xfer-{xfer}/part_{number:03d}.b64 > {ROOT}/rm-xfer-{xfer}/part_{number:03d}.bin`, then run `wc -c` and `sha256sum` on the .bin file. Leave both files in place.

Reply format (exactly this):
```
EXECUTED: yes|no
PART: {number}/{total}
BIN_BYTES: <number>
BIN_SHA256: <hex>
NOTES: <how you wrote the file, anything unusual>
```

BEGIN_B64
{b64}END_B64
"""


def assemble_packet(xfer, total, codec, name, payload_sha, payload_bytes, original_sha, original_bytes, delete_after, root="auto"):
    unpack = {
        "zlib": "python3 -c \"import zlib,sys; sys.stdout.buffer.write(zlib.decompress(open(sys.argv[1],'rb').read()))\" payload.bin > " + name,
        "lzma": "python3 -c \"import lzma,sys; sys.stdout.buffer.write(lzma.decompress(open(sys.argv[1],'rb').read()))\" payload.bin > " + name,
        "none": "cp payload.bin " + name,
    }[codec]
    cleanup = (f"\n6. Delete the directory `{ROOT}/rm-xfer-{xfer}` (`rm -rf`) and report `ls -d {ROOT}/rm-xfer-{xfer}`."
               if delete_after else f"\n6. Leave `{ROOT}/rm-xfer-{xfer}` in place; a later job will use `{name}`.")
    return f"""# Task: join {total} received parts, check the whole, and unpack it

Nothing sensitive beyond what the user chose to send. Use the shell on your own computer (virtual machine). Do not estimate: run the commands and copy their output. Do not modify or repair data. If you cannot run the commands, reply `NOT EXECUTED` and say why.

Set the work root first: {root_step(xfer, root)}.
Then work in `{ROOT}/rm-xfer-{xfer}`, where the parts part_001.bin to part_{total:03d}.bin were written by earlier jobs.
1. `ls -l` the directory and report the file names and sizes. If any part is missing, stop and say which.
2. `cat part_*.bin > payload.bin` (the names sort in order), then `wc -c payload.bin` and `sha256sum payload.bin`.
3. If the payload hash is not the expected one given below, stop and report; do not continue.
4. Unpack: `{unpack}`
5. Report `wc -c {name}` and `sha256sum {name}`.{cleanup}

Expected, for your comparison only: payload {payload_bytes} bytes with sha256 {payload_sha}; unpacked file {original_bytes} bytes with sha256 {original_sha}.

Reply format (exactly this):
```
EXECUTED: yes|no
PARTS_FOUND: <number>
PAYLOAD_BYTES: <number>
PAYLOAD_SHA256: <hex>
FILE_NAME: {name}
FILE_BYTES: <number>
FILE_SHA256: <hex>
CLEANUP: <output or KEPT>
NOTES: <anything unusual>
```
"""


def pack(source, out, chunk_bytes=36000, codec="zlib", xfer=None, delete_after=True, root="auto"):
    source, out = Path(source), Path(out)
    check_root(root)
    if codec not in CODECS or chunk_bytes < 1000:
        raise ValueError("unknown codec or chunk size too small")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", source.name):
        raise ValueError(f"file name {source.name!r} must be plain letters, digits, _ . -")
    original = source.read_bytes()
    payload = compress(original, codec)
    parts = [payload[i:i + chunk_bytes] for i in range(0, len(payload), chunk_bytes)] or [b""]
    xfer = xfer or sha(original)[:12]
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", xfer):
        raise ValueError("transfer id must be letters, digits, _ -")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("output directory must be empty")
    manifest = {"id": xfer, "name": source.name, "codec": codec, "original_bytes": len(original), "original_sha256": sha(original),
                "payload_bytes": len(payload), "payload_sha256": sha(payload),
                "parts": [{"n": i + 1, "bytes": len(p), "sha256": sha(p)} for i, p in enumerate(parts)]}
    for i, part in enumerate(parts, 1):
        b64 = base64.encodebytes(part).decode("ascii")
        (out / f"part_{i:03d}.md").write_text(part_packet(xfer, i, len(parts), b64, len(part), sha(part), root), encoding="utf-8", newline="\n")
    (out / "assemble.md").write_text(assemble_packet(xfer, len(parts), codec, source.name, sha(payload), len(payload), sha(original),
                                                     len(original), delete_after, root), encoding="utf-8", newline="\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def field(reply, name):
    match = re.search(rf"^{name}:[ \t]*(\S+)", reply.replace("\r\n", "\n"), re.M)
    return match.group(1) if match else None


def check(out, replies):
    """[(what, ok, detail)] comparing the reply files with the manifest."""
    manifest = json.loads((Path(out) / "manifest.json").read_text(encoding="utf-8"))
    results = []
    for part in manifest["parts"]:
        path = Path(replies) / f"reply_part_{part['n']:03d}.md"
        if not path.exists():
            results.append((f"part {part['n']}", False, "no reply"))
            continue
        text = path.read_text(encoding="utf-8")
        ok = (field(text, "EXECUTED") == "yes" and field(text, "BIN_BYTES") == str(part["bytes"])
              and field(text, "BIN_SHA256") == part["sha256"])
        results.append((f"part {part['n']}", ok, "" if ok else f"bytes={field(text, 'BIN_BYTES')} sha={field(text, 'BIN_SHA256')}"))
    path = Path(replies) / "reply_assemble.md"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        ok = (field(text, "EXECUTED") == "yes" and field(text, "PAYLOAD_SHA256") == manifest["payload_sha256"]
              and field(text, "FILE_BYTES") == str(manifest["original_bytes"]) and field(text, "FILE_SHA256") == manifest["original_sha256"])
        results.append(("assembled file", ok, "" if ok else f"payload={field(text, 'PAYLOAD_SHA256')} file={field(text, 'FILE_SHA256')}"))
    else:
        results.append(("assembled file", False, "no reply"))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("pack")
    pk.add_argument("source")
    pk.add_argument("out")
    pk.add_argument("--chunk-bytes", type=int, default=36000)
    pk.add_argument("--codec", choices=CODECS, default="zlib")
    pk.add_argument("--id")
    pk.add_argument("--keep", action="store_true", help="the assemble job leaves the directory on Dot's disk")
    pk.add_argument("--root", default="auto", help="'auto' (verify a writable root in the remote environment) or an absolute path")
    ck = sub.add_parser("check")
    ck.add_argument("out")
    ck.add_argument("replies")
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            manifest = pack(args.source, args.out, args.chunk_bytes, args.codec, args.id, not args.keep, args.root)
            print(f"{manifest['name']}: {manifest['original_bytes']} bytes -> {manifest['payload_bytes']} ({manifest['codec']}), "
                  f"{len(manifest['parts'])} parts, id {manifest['id']}")
            return 0
        results = check(args.out, args.replies)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for what, ok, detail in results:
        print(f"{what}: {'ok' if ok else 'FAILED'} {detail}".rstrip())
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
