"""Unpack the file blocks of a Dot reply into an empty directory and check every sha256.

    python -I -X utf8 unpack_files.py <reply.md> <empty-output-dir> [--keep-unverified]

The reply must carry blocks written exactly as

    === FILE: <relative/path> sha256=<64 lowercase hex> ===
    <file content>
    === END FILE ===

Exit 0 only when at least one block was found and every hash matched. A path is relative and made of plain names
(letters, digits, `_`, `.`, `-`); nothing outside the output directory is ever written and an existing file is never
overwritten. A file whose hash does not match is not written, unless --keep-unverified, which writes it with an
`.unverified` suffix so it can be read. The dot-subagent runner writes CRLF; the reply is normalized to LF first.
The code that comes back is untrusted: read it and run its tests before relying on it.
"""

import argparse
import hashlib
from pathlib import Path
import re
import sys

HEADER = re.compile(r'^=== FILE: (?P<name>\S+)(?: sha256=(?P<sha>[0-9a-f]{64}))? ===$')
END = '=== END FILE ==='
PART = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.\-]*$')


def safe_parts(name):
    parts = name.replace('\\', '/').lstrip('./').split('/') if False else name.split('/')
    if not parts or any(part in ('.', '..') for part in parts) or not all(PART.match(part) for part in parts):
        raise ValueError(f'unsafe file name {name!r}')
    return parts


def parse(reply, require_sha=True):
    """[(name, claimed sha256, content candidates)] from a reply; a candidate is the text with and without a final newline."""
    lines = reply.replace('\r\n', '\n').split('\n')
    blocks, i = [], 0
    while i < len(lines):
        match = HEADER.match(lines[i])
        if not match or (require_sha and not match['sha']):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j] != END:
            j += 1
        if j == len(lines):
            raise ValueError(f'block {match["name"]!r} has no {END} line')
        body = '\n'.join(lines[i + 1:j])
        blocks.append((match['name'], match['sha'], (body + '\n', body)))
        i = j + 1
    return blocks


def unpack(reply, out, keep_unverified=False, allow_existing=False):
    """Write the verified files under `out`; returns [(name, status)] with status 'ok', 'mismatch' or 'exists'."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if not allow_existing and any(out.iterdir()):
        raise ValueError('output directory must be empty')
    root = out.resolve()
    results = []
    seen_targets = set()
    for name, claimed, candidates in parse(reply, require_sha=not allow_existing):
        parts = safe_parts(name)
        target = root.joinpath(*parts)
        if root not in target.parents:
            raise ValueError(f'unsafe file name {name!r}')
        if claimed:
            content = next((c for c in candidates if hashlib.sha256(c.encode('utf-8')).hexdigest() == claimed), None)
        else:
            content = candidates[0]
        if content is None and allow_existing and keep_unverified:
            content = candidates[0]
        if content is None and not keep_unverified:
            results.append((name, 'mismatch'))
            continue
        if (target in seen_targets) or (not allow_existing and target.exists()):
            results.append((name, 'exists'))
            continue
        seen_targets.add(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            target = target.with_name(target.name + '.unverified')
            content = candidates[0]
        target.write_bytes(content.encode('utf-8'))
        results.append((name, 'ok' if not target.name.endswith('.unverified') else 'mismatch'))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('reply')
    parser.add_argument('out')
    parser.add_argument('--keep-unverified', action='store_true')
    parser.add_argument('--allow-existing', action='store_true',
                        help='Allow unpacking into a non-empty workspace directory and updating existing files.')
    args = parser.parse_args(argv)
    try:
        results = unpack(Path(args.reply).read_bytes().decode('utf-8'), args.out,
                         keep_unverified=args.keep_unverified, allow_existing=args.allow_existing)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    for name, status in results:
        print(f'{name}: {status}')
    if not results:
        print('ERROR: no file blocks found', file=sys.stderr)
        return 1
    return 0 if all(status == 'ok' for _, status in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
