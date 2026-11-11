"""Local model-name normalization shared by discovery and allocation.

Always preserve an exact calibrated model/alias before trying a family match. A name that is not a calibrated row has
no scores (nothing here supplies a default row).
"""
import re


# A `:effort` suffix names an effort, not a vendor tag: `claude-opus-5-5:medium` asks for Medium.
EFFORT_SUFFIXES = ('none', 'minimal', 'default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_COLON_EFFORT = re.compile(r'^(?P<name>.+):(?P<effort>' + '|'.join(EFFORT_SUFFIXES) + r')$', re.I)
_DASH_EFFORT = re.compile(r'^(?P<name>.+)-(?P<effort>' + '|'.join(EFFORT_SUFFIXES) + r')$')
_VENDOR_TAG = re.compile(r'(?:@\d{8}|-\d{8}|-\d{4}-\d{2}-\d{2}|:\w+|\[\w+\]|-(?:latest|preview))$')
# How a name reached its directory row. `family` and `unknown` carry no measured speed of their own.
DIRECT_SOURCES = ('exact', 'alias')


def split_model_effort(raw):
    """('claude-opus-5-5', 'medium') for 'claude-opus-5-5:medium'; (raw, None) without an effort suffix."""
    name = str(raw or '').strip()
    match = _COLON_EFFORT.match(name)
    return (match['name'], match['effort'].lower()) if match else (name, None)


def resolve_model(raw_model_name, known_models, aliases=None):
    """The one model-name resolver: {'key', 'source', 'effort', 'bare'}.

    source: `exact` (the directory has this name), `alias` (found after dropping a provider prefix, case, date,
    -latest/-preview, a vendor tag or an effort suffix, or through `aliases`), `family` (only a fuzzy family match),
    `unknown` (key None). `effort` is what the name itself asked for. `bare` is the undecorated lowercase name.
    """
    aliases = aliases or {}
    known = {k for k, v in known_models.items() if isinstance(v, dict)}

    def exact(name):
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name if name in known else None

    raw, effort = split_model_effort(raw_model_name)
    name = raw.rsplit('/', 1)[-1].lower()

    def found(key, source, named_effort=None):
        return {'key': key, 'source': source, 'effort': effort or named_effort, 'bare': name}

    if match := exact(raw):
        return found(match, 'exact' if match == raw else 'alias')
    for candidate in (raw.rsplit('/', 1)[-1], raw.lower(), name):
        if match := exact(candidate):
            return found(match, 'alias')
    # Grok Bot is a remote service selector, not a Grok Build/model-family alias.
    if name == 'grok-bot':
        return found(None, 'unknown')
    while True:
        stripped = _VENDOR_TAG.sub('', name)
        if stripped == name:
            break
        name = stripped
        if match := exact(name):
            return found(match, 'alias')
    if dashed := _DASH_EFFORT.match(name):
        if match := exact(dashed['name']):
            return found(match, 'alias', dashed['effort'])
    families = (
        (r'^claude-opus', ('claude-opus-5-5',)),
        (r'^claude-sonnet', ('claude-sonnet-5-5',)),
        (r'^claude-fable', ('claude-fable-5-2',)),
        (r'^claude-haiku', ('claude-haiku-5-5',)),
        (r'^gpt-6.*luna', ('gpt-6-luna',)),
        (r'^gpt-6.*terra', ('gpt-6.1-terra', 'gpt-6-astra')),
        (r'^gpt-6.*sol', ('gpt-6.1-sol', 'gpt-6-astra')),
        (r'^gpt-6.*pro', ('gpt-6-pro', 'gpt-6-astra')),
        (r'^gpt-6', ('gpt-6-astra',)),
        (r'^gemini.*flash', ('gemini-3.8-flash-high',)),
        (r'^gemini', ('gemini-4-argon-high',)),
        (r'^grok.*(?:mini|fast)|^grok-code', ('grok-4.7-build-fast', 'grok-4.7')),
        (r'^grok', ('grok-4.7',)),
        (r'^deepseek.*flash', ('deepseek-v4.1-flash-free',)),
        (r'^deepseek', ('deepseek-v4-pro',)),
        (r'^qwen.*flash', ('qwen3-flash-2025-07-28',)),
        (r'^qwen.*coder', ('qwen3-coder-next', 'qwen3.8-max')),
        (r'^qwen', ('qwen3.8-max',)),
        (r'^kimi|^moonshot', ('kimi-k3.1-pro',)),
        (r'^glm.*flash', ('glm-5.3-flash-free',)),
        (r'^glm', ('glm-5.3',)),
        (r'^minimax.*flash', ('minimax-m3.1-flash',)),
        (r'^minimax|^abab', ('minimax-m3',)),
        (r'^doubao', ('doubao-seed-2-1-pro-260628',)),
        (r'^mimo.*flash', ('mimo-v2.6-flash-free',)),
        (r'^mimo', ('mimo-v2.6-pro',)),
        (r'^nemotron.*lightning', ('nemotron-3.5-lightning-free', 'nemotron-3-ultra-free')),
        (r'^nemotron.*(?:super|vl)', ('nemotron-3.5-super-vl-free', 'nemotron-3-ultra-free')),
        (r'^nemotron', ('nemotron-3-ultra-free',)),
        (r'^muse', ('muse-wonder-1.0',)),
        # No new Llama ID in the October catalog. Use the generic baseline,
        # rather than resurrecting Llama 4 or pretending Muse is a Llama alias.
        (r'^llama', ()),
        (r'^mistral|^codestral|^devstral', ('mistral-large-4',)),
    )
    family_candidates = next((targets for pattern, targets in families if re.search(pattern, name)), None)
    if family_candidates is not None:
        target = next((t for t in family_candidates if t in known), None)
        return found(target, 'family' if target else 'unknown')
    for prefix in sorted(known, key=lambda x: (-len(x), x)):
        # A delimiter prevents e.g. gpt-6.10 from accidentally matching gpt-6.1.
        if name.startswith(prefix + '-') or name.startswith(prefix + ':'):
            if match := exact(prefix):
                return found(match, 'family')
    return found(None, 'unknown')


def resolve_canonical_model(raw_model_name, known_models, aliases=None):
    """Return a known canonical ID, or None for the generic baseline.

    Unknown launch IDs remain untouched by callers: matching affects scores only.
    """
    return resolve_model(raw_model_name, known_models, aliases)['key']
