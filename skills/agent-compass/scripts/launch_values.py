"""Launch values: the declared patterns a value from reference data or discovery must match before it can reach a command
line, an environment variable, a config fragment or a path, and the quoting used where such a value is put into text.

Reference data (shipped or published) and discovery are checked where they enter (account_fleet.load_accounts): a row
whose model, effort, mode or name does not match is a named error, never a launch. Every place that builds a command
still passes a value as its own argument, or quotes it for the syntax it lands in (toml_string, shell_word), so a value
that slipped past the check cannot change the command's structure either.
"""
import json
import re

# A model id: `claude-opus-5-5`, `gpt-6.2-sol`, `openai-codex/gpt-6-sol`, `model:high` (a fixed effort), `name@version`.
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}')
# An account, provider, harness, family, effort or mode id: one word, no path separator (a provider names a run directory).
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
# A bundle id, which never names a path: an automatic one is `auto:<account>:<harness>:<model>@<effort>`.
BUNDLE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,239}')
BUNDLE_FIELDS = {'model': MODEL, 'launch_model': MODEL, 'effort': NAME, 'modeId': NAME, 'provider': NAME,
                 'harness': NAME, 'family': NAME}
# A word a POSIX shell and PowerShell both read as itself.
_PLAIN_WORD = re.compile(r'[A-Za-z0-9._:/@+=-]+')


def problem(label, value, pattern):
    """None when ``value`` is a string matching ``pattern``, else what is wrong, naming ``label``."""
    if isinstance(value, str) and pattern.fullmatch(value):
        return None
    return f'{label} {value!r} does not match {pattern.pattern}'


def bundle_problems(account, bundle_id, bundle):
    """What in one bundle row cannot reach a launch: its id and every launch field that is present."""
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


def toml_string(value):
    """``value`` as a TOML basic string (`-c key=<this>`): JSON string escapes are valid TOML basic-string escapes."""
    if not isinstance(value, str):
        raise ValueError(f'a TOML string value must be text, not {type(value).__name__}')
    return json.dumps(value, ensure_ascii=True)


def shell_word(value):
    """``value`` as one word of a command a parent session runs in a POSIX shell or PowerShell: as it is when plain,
    else single-quoted; a value holding a single quote cannot be quoted the same way for both and is refused."""
    value = str(value)
    if _PLAIN_WORD.fullmatch(value):
        return value
    if "'" in value or not value:
        raise ValueError(f'{value!r} cannot be quoted as one shell word')
    return f"'{value}'"
