"""The installer's ``agentcompass.exe`` doubles as the Python interpreter for the router's own scripts.

The skill and setup scripts stay real files on disk and start each other as ``[sys.executable, "-X", "utf8", script, ...]``
(also ``-I``, ``-S``, ``-c <code>`` and ``-m <module>``). In the frozen CLI ``sys.executable`` is ``agentcompass.exe``, so it
accepts that command line too and runs the script with the bundled interpreter and standard library. No subcommand of
``agentcompass`` starts with ``-`` other than ``-h``/``--help``/``-V``/``--version``, and none names a ``.py`` file or a
directory, so the two never overlap.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

# Interpreter switches that take no value; they change nothing the router's scripts rely on and are accepted as such.
_PLAIN_FLAGS = {"-I", "-E", "-s", "-S", "-B", "-u", "-O", "-OO", "-q", "-b", "-bb", "-d"}


def looks_like_python(argv: list[str]) -> bool:
    if not argv:
        return False
    first = argv[0]
    if first in _PLAIN_FLAGS or first in ("-c", "-m", "-X", "-W") or first.startswith(("-X", "-W")):
        return True
    if first.lower().endswith((".py", ".pyw")):
        return True
    return Path(first).is_dir() and (Path(first) / "__main__.py").is_file()


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    sys.stderr.write(f"{code}\n")
    return 1


def run(argv: list[str]) -> int:
    """Run ``argv`` the way ``python <argv>`` would, in this process."""
    args = list(argv)
    while args:
        head = args[0]
        if head in _PLAIN_FLAGS:
            args.pop(0)
        elif head in ("-X", "-W"):
            args = args[2:]
        elif head.startswith(("-X", "-W")) and len(head) > 2:
            args.pop(0)
        else:
            break
    if not args:
        sys.stderr.write("agentcompass.exe has no interactive Python prompt; pass a script, -c or -m\n")
        return 2
    try:
        if args[0] == "-c":
            if len(args) < 2:
                sys.stderr.write("Argument expected for the -c option\n")
                return 2
            sys.argv = ["-c", *args[2:]]
            namespace = {"__name__": "__main__", "__builtins__": __builtins__}
            exec(compile(args[1], "<string>", "exec"), namespace)
            return 0
        if args[0] == "-m":
            if len(args) < 2:
                sys.stderr.write("Argument expected for the -m option\n")
                return 2
            sys.argv = [args[1], *args[2:]]
            if args[1] == "agentcompass":
                from agentcompass.cli import main
                return int(main(args[2:]) or 0)
            runpy.run_module(args[1], run_name="__main__", alter_sys=True)
            return 0
        target = Path(args[0])
        if target.is_dir() and target.name == "agentcompass":
            # `python <dir>/agentcompass <command>`: the package directory of a checkout or wheel; run the bundled CLI.
            from agentcompass.cli import main
            return int(main(args[1:]) or 0)
        sys.argv = [str(target), *args[1:]]
        folder = str(target.resolve().parent)
        if folder not in sys.path:
            sys.path.insert(0, folder)
        runpy.run_path(str(target), run_name="__main__")
        return 0
    except SystemExit as exc:
        return _exit_code(exc)
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (OSError, ValueError, AttributeError):
            pass


def environment_ready() -> None:
    """UTF-8 for child scripts that read PYTHONUTF8 or decode their own pipes."""
    os.environ.setdefault("PYTHONUTF8", "1")
