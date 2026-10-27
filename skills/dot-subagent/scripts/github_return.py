"""Get files back from Dot through the user's private GitHub repository instead of through its typed reply.

    python -I -X utf8 github_return.py recipe <job> [--branch dot-returns] [--gzip|--no-gzip]
    python -I -X utf8 github_return.py fetch  <reply.md> <out-dir> [--repo <owner/dot-transfer>] [--branch dot-returns]
    python -I -X utf8 github_return.py purge  <repo-clone-dir> <job> [--branch dot-returns]

Why: Dot's reply is typed by the model, about 18 s per 1,000 characters, so a 60,000-character log costs 18 minutes. The
connector write is made by Dot's own code: on 2026-10-10 a 405 KB text file (300,000 random bytes) reached the branch in
8.3 s and decoded to exactly the sha256 Dot reported. A write of 1,000,000 bytes did not return within 120 s (an approval
was pending), so files go in parts of at most 5,200 lines of 76 characters (about 400 KB of text, 300 KB of data).

`recipe` prints the instructions to paste into the packet. Dot gzips each file, base64-encodes it, writes the parts to
`returns/<job>/<name>.partNNN.b64.txt` on the branch and replies with one short line per file:

    FILE <name> <bytes> <sha256> parts=<n> gzip=<yes|no>

`fetch` reads those lines from the reply, downloads the parts with the gh CLI (the user's own login), joins them, decodes,
gunzips, checks size and sha256 against the line, and writes the files into <out-dir> (a new directory; never overwrites,
refuses unsafe names). `purge` deletes `returns/<job>` from the branch with git in a local clone and pushes. Git history
keeps what was pushed, so send only results the user is content to leave in that private repository.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


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
BRANCH = "dot-returns"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
JOB = re.compile(r"[A-Za-z0-9_\-]+")
LINES_PER_PART = 5200
FILE_LINE = re.compile(r"^FILE[ \t]+(\S+)[ \t]+(\d+)[ \t]+([0-9a-fA-F]{64})[ \t]+parts=(\d+)[ \t]+gzip=(yes|no)[ \t]*$", re.M)


def recipe(job, branch=BRANCH, use_gzip=True):
    if not JOB.fullmatch(job):
        raise ValueError("job id must be letters, digits, _ -")
    compress = ("Compress it with `gzip -9` (keep the original), then work with the compressed file."
                if use_gzip else "Do not compress it.")
    return f"""Return the result files through GitHub, not through your reply. The user connected GitHub to this account on purpose: use only the connector you already have, write only to the existing branch `{branch}` of `{REPO}` under `returns/{job}/`, never touch `main`, and do not ask for or write any token or key.

For each file to return (call it F, plain letters, digits, _ . - in its name):
1. Compute the size and `sha256sum` of F.
2. {compress}
3. Base64-encode the file with 76-character lines (`base64 -w 76`) and split the text into parts of at most {LINES_PER_PART} lines (`split -l {LINES_PER_PART}`), in order.
4. Write each part with the connector to the path `returns/{job}/F.partNNN.b64.txt` on branch `{branch}` (NNN = 001, 002, ...). The content must come from your own code on the virtual machine, never typed or pasted by you. Do the writes one after the other. If one write has not returned after 60 seconds, stop and report which parts were written.
5. In your reply, give one line per file and nothing else about the contents:
   `FILE <F> <size of the ORIGINAL F in bytes> <sha256 of the ORIGINAL F> parts=<number of parts> gzip={"yes" if use_gzip else "no"}`
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gh_get(repo, branch, path):
    """Raw bytes of a file on the branch through the gh CLI."""
    result = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={branch}"],
                            capture_output=True)
    if result.returncode:
        raise OSError(f"gh could not read {path}: {result.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return result.stdout


def fetch(reply_path, out_dir, repo=REPO, branch=BRANCH, job=None, get=gh_get):
    """Download, join, decode and verify every FILE line of the reply; returns [(name, bytes, ok)]."""
    text = Path(reply_path).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = FILE_LINE.findall(text)
    if not lines:
        raise ValueError("no FILE lines in the reply")
    job = job or (re.search(r"^JOB:[ \t]*(\S+)", text, re.M) or [None, None])[1]
    if not job or not JOB.fullmatch(job):
        raise ValueError("the job id is needed: pass --job (it is the directory under returns/)")
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} must be empty or new")
    results, staged = [], []
    for name, size, digest, parts, gz in lines:
        if not NAME.fullmatch(name):
            raise ValueError(f"unsafe file name {name!r}")
        count = int(parts)
        joined = b"".join(get(repo, branch, f"returns/{job}/{name}.part{n:03d}.b64.txt") for n in range(1, count + 1))
        try:
            data = base64.b64decode(joined, validate=False)
            if gz == "yes":
                data = gzip.decompress(data)
        except (ValueError, OSError, EOFError):
            results.append((name, 0, False))
            continue
        ok = len(data) == int(size) and sha(data) == digest.lower()
        results.append((name, len(data), ok))
        if ok:
            staged.append((name, data))
    out.mkdir(parents=True, exist_ok=True)
    for name, data in staged:
        (out / name).write_bytes(data)
    return results


def purge(clone, job, branch=BRANCH):
    if not JOB.fullmatch(job):
        raise ValueError("job id must be letters, digits, _ -")
    git = ["git", "-C", str(clone)]
    subprocess.run(git + ["fetch", "origin", branch], check=True, capture_output=True)
    subprocess.run(git + ["checkout", "-B", branch, f"origin/{branch}"], check=True, capture_output=True)
    subprocess.run(git + ["rm", "-r", "-q", f"returns/{job}"], check=True, capture_output=True)
    subprocess.run(git + ["commit", "-q", "-m", f"purge returns/{job}"], check=True, capture_output=True)
    subprocess.run(git + ["push", "origin", f"HEAD:{branch}"], check=True, capture_output=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rc = sub.add_parser("recipe")
    rc.add_argument("job")
    rc.add_argument("--branch", default=BRANCH)
    rc.add_argument("--no-gzip", action="store_true")
    fc = sub.add_parser("fetch")
    fc.add_argument("reply")
    fc.add_argument("out_dir")
    fc.add_argument("--repo", default=REPO)
    fc.add_argument("--branch", default=BRANCH)
    fc.add_argument("--job")
    pg = sub.add_parser("purge")
    pg.add_argument("clone")
    pg.add_argument("job")
    pg.add_argument("--branch", default=BRANCH)
    args = parser.parse_args(argv)
    try:
        if args.command == "recipe":
            print(recipe(args.job, args.branch, not args.no_gzip), end="")
            return 0
        if args.command == "purge":
            purge(args.clone, args.job, args.branch)
            return 0
        results = fetch(args.reply, args.out_dir, args.repo, args.branch, args.job)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for name, size, ok in results:
        print(f"{name}: {size} bytes {'ok' if ok else 'FAILED'}")
    return 0 if all(ok for _, _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
