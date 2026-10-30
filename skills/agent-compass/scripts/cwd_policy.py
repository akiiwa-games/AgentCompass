"""Account working-directory policy, shared by the allocator and model settings."""
from pathlib import Path
import re


def normalized(path=None):
    return str(path or Path.cwd()).replace('\\', '/').rstrip('/')


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


def from_prefixes(prefixes):
    values = [prefixes] if isinstance(prefixes, str) else prefixes or []
    patterns = []
    for value in values:
        path = normalized(value)
        start = '^' if ':' in path or path.startswith('/') else r'(^|/)'
        patterns.append(start + re.escape(path) + r'(/|$)')
    return patterns[0] if len(patterns) == 1 else '(?:' + '|'.join(patterns) + ')' if patterns else ''
