"""Local model-name normalization shared by discovery and allocation.

Always preserve an exact calibrated model/alias before trying a family match. A name that is not a calibrated row has
no scores (nothing here supplies a default row).

Which row a served name is comes first from the catalog's `provider_names` table (owner, 2026-11-19): a serving
provider's own declaration (`rows`: the provider's listing names a different dated snapshot than the maker's default,
or spells the row another way, or the maker shut the name down there), else the maker's declaration of what the name serves now (`defaults`: a name whose
maker moved it to a newer dated snapshot reads as that snapshot's row on any provider and with no provider known), else
the name resolves as below. A decorated spelling (case, a vendor tag such as a date, `-latest` or `-preview`, a dash
effort) reads the declarations of each undecorated spelling at the step where it would reach a row of that spelling, so
`deepseek-v4-pro-high` reads the declarations of `deepseek-v4-pro` (resolve_model). Provider ids, like names, are
matched lowercase. The serving provider of a name is the endpoint a caller knows serves it (an OpenAI-compatible
gateway account, a Magpie preset or `account:<kind>`), else the provider part of a `provider/name` id
(serving_providers); the release check (audit/gateway_coverage.py) reads the same functions.
"""
import re

import public_data


# A `:effort` suffix names an effort, not a vendor tag: `claude-opus-5-5:medium` asks for Medium.
EFFORT_SUFFIXES = ('none', 'minimal', 'default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_COLON_EFFORT = re.compile(r'^(?P<name>.+):(?P<effort>' + '|'.join(EFFORT_SUFFIXES) + r')$', re.I)
_DASH_EFFORT = re.compile(r'^(?P<name>.+)-(?P<effort>' + '|'.join(EFFORT_SUFFIXES) + r')$')
_VENDOR_TAG = re.compile(r'(?:@\d{8}|-\d{8}|-\d{4}-\d{2}-\d{2}|:\w+|\[\w+\]|-(?:latest|preview))$')
# How a name reached its directory row: `provider` is the row a serving provider's declaration names, `default` the row
# the maker's declaration names (provider_names). `family`, the PROVIDER_NAME_STATUSES, `conflict` and `unknown` name no
# row of their own.
DIRECT_SOURCES = ('exact', 'alias', 'provider', 'default')
# Why a provider's declaration names no row: the maker shut the name down there (`retired`), or the provider's listing
# identifies the name as weights the catalog has no row for (`other_weights`).
PROVIDER_NAME_STATUSES = ('retired', 'other_weights')


FAMILY_SIZES = ('small', 'full')     # a families row's declared size class (series.py: small never joins a full series)


def family_patterns(accounts):
    """The catalog's ordered family patterns ([{'pattern', 'models', 'maker', 'line', 'size', 'tier_up'?}]) from its `families` maintainer
    declaration."""
    table = accounts.get('families') if isinstance(accounts, dict) else None
    if not isinstance(table, dict) or not isinstance(table.get('rows'), list):
        raise public_data.ReferenceDataError('accounts has no families table: the catalog needs `families` {kind, as_of, statement, '
                         'rows: [{pattern, models}]} (references/accounts.json)')
    return table['rows']


def provider_name_table(accounts):
    """The catalog's `provider_names` table (accounts.json) {statement, defaults: {served name: declaration}, rows:
    {provider: {served name: declaration}}}; a declaration is {model: row | null, status?, canonical_id?, kind, url,
    as_of, statement}. Every resolution reads it: a document without it is refused, never read as declaring none."""
    table = accounts.get('provider_names') if isinstance(accounts, dict) else None
    if not isinstance(table, dict) or not isinstance(table.get('rows'), dict) or not isinstance(table.get('defaults'), dict):
        raise public_data.ReferenceDataError('accounts has no provider_names table: the catalog needs `provider_names` '
                                             '{statement, defaults: {name: declaration}, rows: {provider: {name: '
                                             'declaration}}} (references/accounts.json)')
    return table


def serving_providers(raw_model_name, endpoints=()):
    """[(provider, name as that provider serves it)]: the endpoints the caller knows serve the name (`endpoints`: an
    OpenAI-compatible gateway account and its catalog provider, a Magpie preset and the provider of a Magpie
    `account:<kind>`), each with the name as listed, since an id an endpoint lists is in that endpoint's own namespace
    (OpenRouter's `deepseek/deepseek-v4-pro` is the weights OpenRouter serves under that id, owner 2026-11-19). With no
    endpoint known (a launcher's catalog, whose `provider/name` ids name the provider it launches: a Paseo provider's model
    list, Cindy's harness rows, a name with no account), the provider part of a `provider/name` id with the name after
    it. No `:effort` suffix; duplicates dropped."""
    name = split_model_effort(raw_model_name)[0]
    out = [(str(p), name) for p in endpoints if p]
    if not out and '/' in name:
        head, tail = name.split('/', 1)
        out.append((head, tail))
    return list(dict.fromkeys(out))


def expand_host_providers(providers, host, accounts=None):
    """`providers` with each preset/provider id of `host` replaced by the models.dev catalog ids
    `provider_names.host_providers[host]` maps it to (ids not in that host's table stay themselves; order kept,
    duplicates dropped). Only callers holding a `host` provider id call this, never `_declared_at`."""
    if isinstance(accounts, dict) and 'rows' in accounts and 'defaults' in accounts:
        hosts = accounts.get('host_providers') or {}
    elif isinstance(accounts, dict) and isinstance(accounts.get('provider_names'), dict):
        hosts = accounts['provider_names'].get('host_providers') or {}
    else:
        hosts = {}
    mapping = hosts.get(str(host or '').lower()) if isinstance(hosts, dict) else None
    out = []
    for p in providers or ():
        if not p:
            continue
        entry = mapping.get(str(p).lower()) if isinstance(mapping, dict) else None
        targets = (entry.get('providers') if isinstance(entry, dict) else None) or ()
        if targets:
            out.extend(str(t) for t in targets if t)
        else:
            out.append(str(p))
    return tuple(dict.fromkeys(out))


def slash_suffixes(spelling):
    """The spelling, then what follows each `/` in turn, ending at the last segment (`a/b/c` -> `('a/b/c', 'b/c', 'c')`)."""
    parts = spelling.split('/')
    return tuple('/'.join(parts[i:]) for i in range(len(parts)))


def provider_declarations(raw_model_name, endpoints, table):
    """[(provider, declaration)] of `table` (provider_name_table) `rows` for the serving providers of the name; providers
    and their names are looked up lowercase across slash_suffixes."""
    return [(p, d) for p, _, d in _declared_at(serving_providers(raw_model_name, endpoints), table)]


def _declared_at(pairs, table):
    rows = table['rows']
    out = []
    for p, n in pairs:
        pid = p.lower()
        prows = rows.get(pid)
        if not isinstance(prows, dict):
            continue
        for cand in slash_suffixes(n.lower()):
            if isinstance(prows.get(cand), dict):
                out.append((pid, cand, prows[cand]))
                break
    return out


def maker_default(raw_model_name, table):
    """The `defaults` declaration (`table`) of the name (lowercase, no `:effort`), or None: default_for its spelling."""
    return default_for(split_model_effort(raw_model_name)[0].lower(), table)


def default_for(spelling, table):
    """The `defaults` declaration of a lowercase spelling, read in slash_suffixes order: the full id, then what follows
    each `/` in turn, then the last segment (`siliconflow/deepseek-ai/deepseek-v4-pro`, `deepseek-ai/deepseek-v4-pro`,
    `deepseek-v4-pro`); the first declared wins, or None."""
    for candidate in slash_suffixes(spelling):
        entry = table['defaults'].get(candidate)
        if isinstance(entry, dict):
            return entry
    return None


def undecorated(name):
    """[(spelling, dash effort)]: `name` (lowercase), then each vendor tag stripped in turn (_VENDOR_TAG), then the fully
    stripped name without a dash effort when it has one: the spellings resolve_model tries for a row, in its order."""
    out, name = [(name.lower(), None)], name.lower()
    while (stripped := _VENDOR_TAG.sub('', name)) != name:
        name = stripped
        out.append((name, None))
    if dashed := _DASH_EFFORT.match(name):
        out.append((dashed['name'], dashed['effort']))
    return out


def split_model_effort(raw):
    """('claude-opus-5-5', 'medium') for 'claude-opus-5-5:medium'; (raw, None) without an effort suffix."""
    name = str(raw or '').strip()
    match = _COLON_EFFORT.match(name)
    return (match['name'], match['effort'].lower()) if match else (name, None)


def bare_name(raw_model_name):
    """The undecorated lowercase name a family pattern is matched against: no provider prefix, no `:effort`, no vendor
    tag (date, -latest, -preview, ...)."""
    name = split_model_effort(raw_model_name)[0].rsplit('/', 1)[-1].lower()
    while (stripped := _VENDOR_TAG.sub('', name)) != name:
        name = stripped
    return name


def family_row(raw_model_name, families):
    """The first `families` row whose pattern matches the bare name (the row that decides the name's family), or None."""
    name = bare_name(raw_model_name)
    return next((f for f in families if re.search(f['pattern'], name)), None)


def resolve_model(raw_model_name, known_models, aliases, families, provider_names, providers=()):
    """The one model-name resolver: {'key', 'source', 'effort', 'bare', 'provider'}.

    `provider_names` is the catalog's provider_name_table. First the serving providers' declarations (serving_providers):
    one declared row is the key (source `provider`, the declaring provider in 'provider'); a declaration with no row
    gives its status (`retired`, `other_weights`: key None, no row on that provider, so out of automatic routing, while a
    route the user names still launches); serving providers that declare different rows give `conflict` (key None).
    Else the `defaults` declaration of the name, its full id before its last segment (default_for: source `default`).
    Else the name's row
    (`exact`, `alias`). The three are read for each spelling of undecorated (the name as given, each vendor tag stripped,
    a dash effort dropped) before the next spelling: a decorated spelling reads the declarations of the undecorated name
    exactly where it would reach that name's row. The result does not depend on the order of the declarations. A name
    nothing declares and no spelling reaches as a row resolves as below.

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

    def found(key, source, named_effort=None, provider=None, *, bare=None):
        return {'key': key, 'source': source, 'effort': effort or named_effort,
                'bare': name if bare is None else bare, 'provider': provider}

    def declared_row(full, spelling, served, dash_effort):
        """The `found` of the declarations of one spelling (served: [(provider, that provider's spelling)]), or None."""
        declared = _declared_at(served, provider_names)
        if len({(d.get('model'), d.get('status')) for _, _, d in declared}) > 1:
            return found(None, 'conflict', dash_effort, bare=spelling)
        if declared:
            by, served_name = min((p, c) for p, c, _ in declared)
            d = next(d for p, c, d in declared if (p, c) == (by, served_name))
            if d.get('model') is None:
                return found(None, d.get('status'), dash_effort, provider=by, bare=served_name)
            if d['model'] not in known:
                raise public_data.ReferenceDataError(f"provider_names rows[{by!r}] names row {d['model']!r}, which the "
                                                     'catalog does not have')
            return found(d['model'], 'provider', dash_effort, provider=by, bare=spelling)
        default = default_for(full, provider_names)
        if isinstance(default, dict):
            if default.get('model') not in known:
                raise public_data.ReferenceDataError(f"provider_names defaults[{full!r}] names row "
                                                     f"{default.get('model')!r}, which the catalog does not have")
            return found(default['model'], 'default', dash_effort, bare=spelling)
        return None

    pairs = serving_providers(raw, providers)
    served_steps = [undecorated(n) for _, n in pairs]
    full_steps = undecorated(raw)            # the full id (owner part included), stripped as the last segment is
    for step, (spelling, dash_effort) in enumerate(undecorated(name)):
        if dash_effort is None:
            name = spelling       # `bare`: the spelling reached so far (a dash effort is not stripped from it)
        served = [(p, steps[step][0]) for (p, _), steps in zip(pairs, served_steps) if step < len(steps)]
        full = full_steps[step][0] if step < len(full_steps) else spelling
        if (hit := declared_row(full, spelling, served, dash_effort)) is not None:
            return hit
        if step == 0:
            for candidate in slash_suffixes(raw):
                if match := exact(candidate):
                    return found(match, 'exact' if candidate == raw and match == raw else 'alias')
                if candidate.lower() != candidate and (match := exact(candidate.lower())):
                    return found(match, 'alias')
        else:
            for candidate in slash_suffixes(full):
                if match := exact(candidate):
                    return found(match, 'alias', dash_effort, bare=spelling)
    row = next((f for f in families if re.search(f['pattern'], name)), None)
    if row is not None:
        target = next((t for t in row['models'] if t in known), None)
        return found(target, 'family' if target else 'unknown')
    for prefix in sorted(known, key=lambda x: (-len(x), x)):
        # A delimiter prevents e.g. gpt-6.10 from accidentally matching gpt-6.1.
        if name.startswith(prefix + '-') or name.startswith(prefix + ':'):
            if match := exact(prefix):
                return found(match, 'family')
    return found(None, 'unknown')


def resolve_canonical_model(raw_model_name, known_models, aliases, families, provider_names, providers=()):
    """Return a known canonical ID, or None for the generic baseline.

    Unknown launch IDs remain untouched by callers: matching affects scores only.
    """
    return resolve_model(raw_model_name, known_models, aliases, families, provider_names, providers)['key']


def resolve_in(raw_model_name, accounts, providers=()):
    """resolve_model against a catalog document (accounts.json shape): its models, aliases, families and provider_names
    table. `providers`: the endpoints the caller knows serve the name (serving_providers)."""
    accounts = accounts if isinstance(accounts, dict) else {}
    return resolve_model(raw_model_name, accounts.get('models') or {}, accounts.get('aliases') or {},
                         family_patterns(accounts), provider_name_table(accounts), providers)
