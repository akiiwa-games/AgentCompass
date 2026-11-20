"""Account working-directory policy, shared by the allocator and model settings."""
import ntpath
from pathlib import Path
import re


def normalized(path=None):
    """The one form a directory is matched in: forward slashes, `.`, `..` and doubled separators resolved (so
    `D:/work/app/../app2` is `D:/work/app2`, never inside `D:/work/app`), no trailing separator."""
    return ntpath.normpath(str(path or Path.cwd())).replace('\\', '/').rstrip('/')


def validate(pattern):
    value = str(pattern or '').strip()
    try:
        re.compile(value, re.IGNORECASE)
    except re.error as error:
        raise ValueError(f'Invalid directory regex: {error}') from None
    return value


def matches(pattern, path=None):
    try:
        return not pattern or re.search(pattern, normalized(path), re.IGNORECASE) is not None
    except (re.error, TypeError):
        return False


def refusal(account, pattern, path=None):
    """The one sentence of an account whose `cwd_regex` does not match the directory it was matched against: the account,
    the pattern, that directory and whose it is, and that the restriction is the user's own setting on this machine.
    Callers add whether anything still launches."""
    origin = "the worker's directory" if path else "this process's working directory"
    return (f"account {account} is restricted to directories matching its cwd_regex {pattern!r} (the user's own "
            f'account setting on this machine), and {origin} {normalized(path)} does not match')
