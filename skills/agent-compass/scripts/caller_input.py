"""The caller's input to one `agentcompass` stage run, read exactly once.

route_judge.main reads it before the part of the run that may run again (public_data.with_fallback): standard input
(`roll --task -`, `scorer set --api-key -`) and the files named by `--input` and `--brief`. Every stage reads them
through this module, so a re-run receives exactly what the first run read, and nothing is read from the caller twice.
A file that could not be read raises its error where the stage reads it, as before. The files named by `--file` are
not read here: they go to the Dot runner, which reads them after the launch (an effect, after which nothing re-runs).
"""
import sys
from pathlib import Path


class CallerInput:
    """What the caller handed this run: standard input (bytes, or one line of text for `--api-key -`) and the bytes of
    each named file (or the OSError reading it raised)."""

    def __init__(self, stdin=None, files=None):
        self.stdin = stdin
        self.files = dict(files or {})


def capture(args, stdin=None):
    """Read the caller's input of ``args`` (parsed stage arguments) now, once."""
    stdin = stdin if stdin is not None else sys.stdin
    files = {}
    named = ([args.input] if getattr(args, 'input', None) is not None else []) + list(getattr(args, 'brief', None) or [])
    for path in named:
        try:
            files[str(path)] = Path(path).read_bytes()
        except OSError as exc:
            files[str(path)] = exc
    data = None
    if args.stage == 'roll' and getattr(args, 'task', None) == '-':
        data = stdin.buffer.read()
    elif args.stage == 'scorer' and getattr(args, 'action', None) == 'set' and getattr(args, 'api_key', None) == '-':
        data = stdin.readline()
    return CallerInput(data, files)


def attach(args, caller):
    """``args`` carrying ``caller`` (the input one run read), for a stage run or its re-run."""
    args.caller_input = caller
    return args


def read_bytes(args, path):
    """The bytes of caller file ``path``: what this run read once, or, for arguments built without capture (a batch
    brief's own namespace carries its parent's capture; a direct call from code), the file now."""
    caller = getattr(args, 'caller_input', None)
    if caller is not None and str(path) in caller.files:
        found = caller.files[str(path)]
        if isinstance(found, OSError):
            raise found
        return found
    return Path(path).read_bytes()


def read_text(args, path):
    return read_bytes(args, path).decode('utf-8-sig')


def stdin_bytes(args):
    """Standard input as this run read it once (`--task -`)."""
    caller = getattr(args, 'caller_input', None)
    if caller is not None and caller.stdin is not None:
        return caller.stdin
    return sys.stdin.buffer.read()


def stdin_line(args):
    """One line of standard input as this run read it once (`--api-key -`)."""
    caller = getattr(args, 'caller_input', None)
    if caller is not None and caller.stdin is not None:
        return caller.stdin
    return sys.stdin.readline()
