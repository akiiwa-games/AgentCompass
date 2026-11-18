"""Launch values: the declared patterns reference data must match, and how any launch value is made safe where it lands.

Reference data (shipped or published) is checked where it enters (account_fleet.launch_problems): a reference row whose
model, effort, mode or name does not match a pattern refuses the set, by name. The patterns accept every value in the
shipped data and every id this code generates itself (an automatic bundle id, its `#<sha8>` collision suffix).

Values from the user's machine (account names the user chose, model ids a provider lists, ids derived from them) are never
dropped by a pattern. They are made safe where they are used: passed as their own argument (argument), quoted for the
syntax they land in (toml_string, shell_word), or reduced to a safe file-name part (path_part). Where a value cannot be
expressed safely in one syntax, that one launch is refused there with a named error; nothing else about the account or
the model disappears.

No value is passed through cmd.exe: a Windows batch launcher (an npm `.cmd` shim, a desktop app's `.cmd`) is started as
the program its launch line runs (program), never as itself, since cmd.exe reads every argument of a batch file as
command text and no quoting of the argument list makes that safe.
"""
import json
import os
import re
import shutil
from pathlib import Path

# A model id: `claude-opus-5-5`, `claude-opus-5-5[1m]`, `gpt-6.2-sol`, `openai-codex/gpt-6-sol`, `model:high`,
# `name@version`.
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,159}')
# An account, provider, harness, family, effort or mode id in reference data: one word, no path separator.
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
# A bundle id: an automatic one is `auto:<account>:<harness>:<model>@<effort>`, with `#<sha8>` after a name collision.
BUNDLE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+#\[\]-]{0,239}')
BUNDLE_FIELDS = {'model': MODEL, 'launch_model': MODEL, 'effort': NAME, 'modeId': NAME, 'provider': NAME,
                 'harness': NAME, 'family': NAME}
# A word a POSIX shell and PowerShell both read as itself.
_PLAIN_WORD = re.compile(r'[A-Za-z0-9._:/@+=-]+')
_UNSAFE_PATH = re.compile(r'[^A-Za-z0-9._-]+')


class LaunchValueError(ValueError):
    """A launch value that cannot be expressed safely in the syntax it would land in: this one launch is refused."""


def problem(label, value, pattern):
    """None when ``value`` is a string matching ``pattern``, else what is wrong, naming ``label``."""
    if isinstance(value, str) and pattern.fullmatch(value):
        return None
    return f'{label} {value!r} does not match {pattern.pattern}'


def bundle_problems(account, bundle_id, bundle):
    """What in one reference bundle row does not match the declared patterns: its id and every launch field present."""
    where = f'{account}.bundles.{bundle_id}'
    found = [p for p in (problem(f'{account}: bundle id', bundle_id, BUNDLE_ID),) if p]
    if not isinstance(bundle, dict):
        return found + [f'{where} is not an object']
    for field, pattern in BUNDLE_FIELDS.items():
        if bundle.get(field) not in (None, ''):
            found += [p for p in (problem(f'{where}.{field}', bundle[field], pattern),) if p]
    efforts = bundle.get('efforts')
    if efforts is not None:
        if not isinstance(efforts, list):
            found.append(f'{where}.efforts is not a list')
        else:
            found += [p for p in (problem(f'{where}.efforts[]', e, NAME) for e in efforts) if p]
    return found


def argument(label, value):
    """``value`` as one argument after its option (`--model <value>`): text, not empty, not read as an option, no line
    break or NUL. Anything else is refused for this launch, by name."""
    if not isinstance(value, str) or not value or value.startswith('-') or any(c in value for c in '\0\r\n'):
        raise LaunchValueError(f'{label} {value!r} cannot be passed as a command-line argument; this launch is refused')
    return value


def path_part(value):
    """``value`` as one file-name part: unsafe characters become `_` (a run directory label, never an identity)."""
    return _UNSAFE_PATH.sub('_', str(value)).strip('._') or '_'


def toml_string(value):
    """``value`` as a TOML basic string (`-c key=<this>`): JSON string escapes are valid TOML basic-string escapes."""
    if not isinstance(value, str):
        raise LaunchValueError(f'a TOML string value must be text, not {type(value).__name__}')
    return json.dumps(value, ensure_ascii=True)


def shell_word(value):
    """``value`` as one word of a command a parent session runs in a POSIX shell or PowerShell: as it is when plain,
    else single-quoted; a value holding a single quote, or one read as an option, cannot be written the same way for
    both and is refused for this launch."""
    value = str(value)
    if value.startswith('-') or not value:
        raise LaunchValueError(f'{value!r} would be read as an option; this launch is refused')
    if _PLAIN_WORD.fullmatch(value):
        return value
    if "'" in value or any(c in value for c in '\0\r\n'):
        raise LaunchValueError(f'{value!r} cannot be quoted as one shell word; this launch is refused')
    return f"'{value}'"


BATCH_SUFFIXES = ('.cmd', '.bat')
_SET = re.compile(r'^@?set\s+(?!/)(?:"(?P<quoted>[^"=]+)=(?P<qvalue>[^"]*)"|(?P<name>[^\s="]+)=(?P<value>.*?))\s*$',
                  re.IGNORECASE)
_REFERENCE = re.compile(r'%~f0|%~dp0|%([^%\s]*)%', re.IGNORECASE)


class UnreadableLauncher(LaunchValueError):
    """A batch launcher whose launch line cannot be read as a program and its arguments: the launch is refused."""


def program(path, environ=None):
    """``(argv, env)`` that start the program at ``path`` with no command interpreter in between.

    For anything but a Windows batch launcher this is ``[path]`` and no variables. A batch launcher (`.cmd`, `.bat`: an
    npm shim, a desktop app's command-line launcher) is read, not run: its launch line is the last line that passes the
    arguments on (`%*`), after the last `&` or `|` outside quotes. Its words are expanded with what the file itself
    sets (`set "NAME=value"`, `%~dp0`, `%~f0`) and then the process environment; the first word, the program, must name
    an existing file that is not itself a batch file (a bare name is looked up on PATH), and a word with several
    possible values (an `if exist` choice) takes the first one that exists. ``env`` holds the variables the file sets
    for the program (those its launch line does not consume, with one value). Anything else (no such line, a word that
    cannot be expanded, more after `%*`, a program that cannot be found) is a stated refusal (UnreadableLauncher),
    never a start through cmd.exe."""
    path = Path(path)
    if path.suffix.lower() not in BATCH_SUFFIXES:
        return [str(path)], {}
    environ = os.environ if environ is None else environ

    def refuse(why):
        raise UnreadableLauncher(f'{path} is a batch launcher that cannot be started without cmd.exe ({why}); cmd.exe '
                                 'would read its arguments as commands, so this launch is refused: point the command '
                                 'at the executable it runs')
    try:
        text = path.read_text(encoding='utf-8', errors='replace')[:65536]
    except OSError as exc:
        refuse(f'it cannot be read: {exc.strerror or exc}')
    lines = [line.strip() for line in text.splitlines()]
    assigned = {}
    for line in lines:
        found = _SET.match(line)
        if found:
            name = found.group('quoted') or found.group('name')
            value = found.group('qvalue') if found.group('quoted') else found.group('value')
            assigned.setdefault(name.lower(), (name, []))[1].append(value)
    launch = [line for line in lines if '%*' in line and not line.lower().startswith(('rem ', '::', 'set '))]
    if not launch:
        refuse('no line passes its arguments on with %*')
    words = _words(_last_command(launch[-1]))
    if '%*' not in words:
        refuse('its launch line passes %* inside another word')
    at = words.index('%*')
    if words[at + 1:]:
        refuse(f'its launch line goes on after %*: {" ".join(words[at + 1:])}')
    words = words[:at]
    if words and words[0].lower() == 'call':
        words = words[1:]
    if not words:
        refuse('its launch line names no program')
    consumed, argv = set(), []
    for index, word in enumerate(words):
        values = _expand(word, path, assigned, environ, consumed, 0)
        if not values:
            refuse(f'{word} cannot be expanded without running cmd.exe')
        if index == 0:
            chosen = next((found for found in map(lambda v: _program_file(v, environ), values) if found), None)
            if chosen is None:
                refuse(f'its program {word} ({values[0]}) is not an existing executable')
        else:
            existing = [v for v in values if _looks_like_path(v) and Path(v).exists()]
            if not existing and len(values) > 1:
                refuse(f'{word} has more than one possible value and none exists')
            chosen = os.path.normpath(existing[0]) if existing else (
                os.path.normpath(values[0]) if _looks_like_path(values[0]) else values[0])
        argv.append(chosen)
    env = {}
    for key, (name, values) in assigned.items():
        if key in consumed or len(set(values)) != 1:
            continue
        expanded = _expand(values[0], path, assigned, environ, set(), 0)
        if len(expanded) == 1:
            env[name] = expanded[0]
    return argv, env


def _last_command(line):
    """What runs last on ``line``: the part after the last `&` or `|` outside double quotes, without a leading `@`."""
    quoted, start = False, 0
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char in '&|' and not quoted:
            start = index + 1
    return line[start:].strip().lstrip('@').strip().rstrip(')').strip()


def _words(text):
    """The words of ``text`` split at spaces outside double quotes, quotes removed."""
    words, current, quoted, started = [], [], False, False
    for char in text:
        if char == '"':
            quoted, started = not quoted, True
        elif char.isspace() and not quoted:
            if started:
                words.append(''.join(current))
            current, started = [], False
        else:
            current.append(char)
            started = True
    if started:
        words.append(''.join(current))
    return words


def _expand(text, path, assigned, environ, consumed, depth):
    """Every value ``text`` can take, its `%NAME%` references expanded from what the file sets, then the environment; []
    when a reference cannot be expanded (substring syntax, `%%F`, an unset name) or the nesting is too deep."""
    if depth > 8:
        return []
    results = ['']
    position = 0
    for found in _REFERENCE.finditer(text):
        literal = text[position:found.start()]
        token = found.group(0)
        if token.lower() == '%~dp0':
            options = [str(path.parent) + os.sep]
        elif token.lower() == '%~f0':
            options = [str(path)]
        else:
            name = found.group(1)
            if not name or ':' in name or name.startswith('~'):
                return []
            if name.lower() in assigned:
                consumed.add(name.lower())
                options = [v for raw in assigned[name.lower()][1]
                           for v in _expand(raw, path, assigned, environ, consumed, depth + 1)]
            else:
                value = next((v for k, v in environ.items() if k.lower() == name.lower()), None)
                options = [value] if value is not None else []
            if not options:
                return []
        results = [r + literal + o for r in results for o in options][:32]
        position = found.end()
    if '%' in text[position:]:
        return []
    return [r + text[position:] for r in results]


def _looks_like_path(value):
    return os.path.isabs(value) or '\\' in value or '/' in value


def _program_file(value, environ):
    """The existing executable file ``value`` names (a bare name looked up on PATH), else None; never a batch file."""
    if _looks_like_path(value):
        candidate = Path(os.path.normpath(value))
        found = str(candidate) if candidate.is_file() else None
    else:
        found = shutil.which(value, path=environ.get('PATH'))
    if not found or Path(found).suffix.lower() in BATCH_SUFFIXES:
        return None
    return found
