"""The public entry (`agentcompass`) as this install spells it, as text for messages and as argv for hand-back commands.

A source checkout (or an installed package) is run by its package directory: `python -X utf8 <dir>/agentcompass <command>`
works from any working directory. When this skill is a bare copy with no package next to it, the console script name is
all that can be given. Inside the Windows installer's frozen `agentcompass.exe` the argv is that exe itself.
"""
import shlex
import sys
from pathlib import Path


def package_dir():
    here = Path(__file__).resolve()
    checkout = here.parents[3]
    if (checkout / 'agentcompass' / '__main__.py').is_file() and (checkout / 'pyproject.toml').is_file():
        return checkout / 'agentcompass'
    installed = here.parents[4] if len(here.parents) > 4 else None
    if installed is not None and installed.name == 'agentcompass' and (installed / '__main__.py').is_file():
        return installed
    return None


def argv(*args):
    """Argument vector that runs `agentcompass <args>`; a list, so paths with spaces need no quoting."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, *map(str, args)]
    package = package_dir()
    if package is None:
        return ['agentcompass', *map(str, args)]
    return [sys.executable, '-X', 'utf8', str(package), *map(str, args)]


def command(*args):
    """The same as one line of text for messages (quoted where a path needs it)."""
    package = None if getattr(sys, 'frozen', False) else package_dir()
    if package is None:
        return ' '.join(['agentcompass', *map(str, args)])
    return ' '.join(['python', '-X', 'utf8',
                     shlex.quote(package.as_posix()), *map(str, args)])
