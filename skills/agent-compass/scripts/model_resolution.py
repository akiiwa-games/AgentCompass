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


def family_patterns(accounts):
    """The catalog's ordered family patterns ([{'pattern', 'models'}]) from its `families` maintainer declaration."""
    table = accounts.get('families') if isinstance(accounts, dict) else None
    if not isinstance(table, dict) or not isinstance(table.get('patterns'), list):
        raise ValueError('accounts has no families table: the catalog needs `families` {kind, as_of, statement, '
                         'patterns: [{pattern, models}]} (references/accounts.json)')
    return table['patterns']


def split_model_effort(raw):
    """('claude-opus-5-5', 'medium') for 'claude-opus-5-5:medium'; (raw, None) without an effort suffix."""
    name = str(raw or '').strip()
    match = _COLON_EFFORT.match(name)
    return (match['name'], match['effort'].lower()) if match else (name, None)


def resolve_model(raw_model_name, known_models, aliases, families):
    """The one model-name resolver: {'key', 'source', 'effort', 'bare'}.

    source: `exact` (the directory has this name), `alias` (found after dropping a provider prefix, case, date,
    -latest/-preview, a vendor tag or an effort suffix, or through `aliases`), `family` (only a fuzzy family match),
    `unknown` (key None). `effort` is what the name itself asked for. `bare` is the undecorated lowercase name.

    `families` is the catalog's ordered family patterns (family_patterns): the first pattern that matches the bare
    name decides, and the name resolves to the first of its models the directory knows (none known: unknown).
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
    family_candidates = next((f['models'] for f in families if re.search(f['pattern'], name)), None)
    if family_candidates is not None:
        target = next((t for t in family_candidates if t in known), None)
        return found(target, 'family' if target else 'unknown')
    for prefix in sorted(known, key=lambda x: (-len(x), x)):
        # A delimiter prevents e.g. gpt-6.10 from accidentally matching gpt-6.1.
        if name.startswith(prefix + '-') or name.startswith(prefix + ':'):
            if match := exact(prefix):
                return found(match, 'family')
    return found(None, 'unknown')


def resolve_canonical_model(raw_model_name, known_models, aliases, families):
    """Return a known canonical ID, or None for the generic baseline.

    Unknown launch IDs remain untouched by callers: matching affects scores only.
    """
    return resolve_model(raw_model_name, known_models, aliases, families)['key']
