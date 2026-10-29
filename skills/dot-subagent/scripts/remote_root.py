"""Shared writable-root preflight for text and GitHub transfer packets."""
import re
import shlex


def check_root(root):
    if root != "auto" and not re.fullmatch(r"/[A-Za-z0-9_./\-]+", root):
        raise ValueError(f"root must be 'auto' or an absolute path of plain characters, got {root!r}")


def root_command(root="auto"):
    check_root(root)
    if root != "auto":
        return f"ROOT={root}"
    # Discover the live environment, then prove writability with a real file.
    # An explicit remote DOT_WORK_ROOT must succeed instead of silently relocating data.
    probe = '''import os, pathlib, tempfile
configured = os.environ.get("DOT_WORK_ROOT")
candidates = [configured] if configured else [os.environ.get("TMPDIR"), tempfile.gettempdir(), os.getcwd(), str(pathlib.Path.home())]
for candidate in candidates:
    if not candidate:
        continue
    try:
        root = pathlib.Path(candidate).resolve()
        with tempfile.TemporaryFile(dir=root):
            pass
    except OSError:
        continue
    print(root)
    break
else:
    raise SystemExit("No writable work root; configure DOT_WORK_ROOT or pass --root")
'''
    return f"ROOT=$(python3 -c {shlex.quote(probe)}) && [ -n \"$ROOT\" ]"


def root_step(xfer, root="auto"):
    command = root_command(root)
    return (f'`{command} && mkdir -p "$ROOT/rm-xfer-{xfer}/parts" '
            f'&& touch "$ROOT/rm-xfer-{xfer}/.w" && rm "$ROOT/rm-xfer-{xfer}/.w" '
            '&& printf "ROOT=%s\\n" "$ROOT"`. If no ROOT line is printed, stop and reply '
            '`EXECUTED: no` with the error. Use the printed ROOT as a literal path in every later '
            'command (variables may not survive between commands) and report it as `ROOT:` in the reply')
