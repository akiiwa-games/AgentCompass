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
