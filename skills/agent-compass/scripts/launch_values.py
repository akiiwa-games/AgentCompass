"""Launch values: the declared patterns reference data must match, and how any launch value is made safe where it lands.

Reference data (shipped or published) is checked where it enters (account_fleet.launch_problems): a reference row whose
model, effort, mode or name does not match a pattern refuses the set, by name. The patterns accept every value in the
shipped data and every id this code generates itself (an automatic bundle id, its `#<sha8>` collision suffix).

Values from the user's machine (account names the user chose, model ids a provider lists, ids derived from them) are never
dropped by a pattern. They are made safe where they are used: passed as their own argument (argument), quoted for the
syntax they land in (toml_string, shell_word), or reduced to a safe file-name part (path_part). Where a value cannot be
expressed safely in one syntax, that one launch is refused there with a named error; nothing else about the account or
the model disappears.
"""
import json
import re

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
