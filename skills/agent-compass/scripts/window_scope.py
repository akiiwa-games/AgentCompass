"""Which models a quota window binds: the one rule routing, the Router Gate and the displays share.

A window's scope is data, never a window id or model name in code:

- ``models``: tags; the window binds a model whose id contains one of them as whole tokens
  (``fable`` binds ``claude-fable-5-1``; ``gemini`` binds ``gemini-4-argon-high``).
- ``models_except``: tags; the window binds every model except those (Antigravity's third-party pool
  is ``models_except: ["gemini"]``).

Scope sources: the account or plan spec's ``windows.<id>`` (accounts.json, plan-templates.json, the user's
accounts-override.json) and the provider reading's own window (usage readers pass a provider-declared
``models`` through). A window binds a model when any source that declares a scope binds it. A window no
source scopes binds every model: an allowance whose scope is unknown is assumed shared, so it can stop a
launch but is never silently skipped. An unknown model (empty id) is drawn by every window for the same reason.
"""
import re

SCOPE_KEYS = ('models', 'models_except')


def _token_form(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')


def _tags(value):
    return [tag for tag in (_token_form(v) for v in value if isinstance(v, str)) if tag] if isinstance(value, (list, tuple)) else []


def tag_matches(tag, model):
    """True when ``tag`` occurs in ``model`` as whole tokens; case and separators do not matter."""
    tag, model = _token_form(tag), _token_form(model)
    return bool(tag and model) and f'-{tag}-' in f'-{model}-'


def declares_scope(source):
    return isinstance(source, dict) and any(_tags(source.get(key)) for key in SCOPE_KEYS)


def scope_of(source):
    """The scope keys a window spec or reading declares, normalized; {} when it declares none."""
    if not declares_scope(source):
        return {}
    return {key: list(source[key]) for key in SCOPE_KEYS if _tags(source.get(key))}


def _source_binds(source, model):
    include, exclude = _tags(source.get('models')), _tags(source.get('models_except'))
    if include and not any(tag_matches(tag, model) for tag in include):
        return False
    return not any(tag_matches(tag, model) for tag in exclude)


def binds(model, *sources):
    """Does work on ``model`` draw a window scoped by ``sources`` (window specs and/or readings)?"""
    if not _token_form(model):
        return True
    declared = [source for source in sources if declares_scope(source)]
    return not declared or any(_source_binds(source, model) for source in declared)
