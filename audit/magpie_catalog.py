"""Build the saved Magpie catalog for the coverage check from a Magpie source checkout (static read: no build, no network).

    git -c core.longpaths=true clone -q --depth 1 --branch v<version> https://github.com/yetone/magpie <dir>
    python -X utf8 audit/magpie_catalog.py <dir>

writes `audit/gateway-catalogs/magpie-<version>.json` (schema `agent-compass.gateway-catalog/3`, `provider_kind:
magpie_preset`), the same bytes for the same checkout. `--version` and `--commit` name the source when the checkout is not a
git clone of a tag; `--out` writes elsewhere.

What it reads, all fixed in Magpie's source (what an install fetches at run time, models.dev syncs and providers' live lists,
is never in the catalog):

- `internal/provider/presets.go`: every preset (provider) with its models.dev catalog ids, any model list it gives itself
  (Vertex AI, the plans), its `Kind` and `Hosts`;
- `internal/catalog/bundled/api.json.gz` and `bundled.go`: the models.dev snapshot the binary ships and its date;
- `internal/provider/google.go`: the Gemini CLI and Antigravity fallback lists; the Claude account's base list is models.dev
  `anthropic` (`internal/provider/claude_code_models.go`);
- `internal/catalog/catalog.go`, `internal/provider/ultra.go`: the rules below, read from the source rather than copied.

Rows are `{provider, model, efforts}`, sorted; `efforts` is null where the source states none (the model's one
configuration). The efforts follow Magpie's own rules: a models.dev row takes its `effort` values, with `none` first for a
model with a thinking switch at a provider of `reasoningOffProviders`, else the newest thinking model of its family at that
provider (`levels`); a model the provider has no word on (a preset's own list, the Google fallbacks) borrows them
(`borrowedEfforts`): its maker's listing, else what most providers give it, else the newest model of its versionless shape
at a maker (`KinEfforts`), else the same without a proxy's level or speed suffix (`Unsuffixed`) or a date (`datedSuffix`).
Magpie also matches other spellings (`SameModel`, renames, user statements); those are not mirrored, so such a row may read
null here where Magpie lists efforts.

The gate is decided per pair (review 2026-11-19): a row is gated when its model's maker is its provider's maker, and is a
report otherwise. A model's maker (`maker` on the row) is read from the bundled models.dev snapshot: the namespace of the
listing's `canonical_model_id` (`anthropic/claude-...` -> `anthropic`); a listing without one, on a models.dev provider
whose id is itself such a namespace, is that provider's own model; a row no listing gives a maker for takes the one maker
the snapshot's listings of the same id (then of the same bare id) agree on, else none (`null`, a report). A provider's
maker (`gate.makers`) is declared in `audit/magpie-makers.json`, each entry with the Magpie source line it reads (a vendor
preset's `Name`, the accounts' lists); a relay, local or remote preset has none.

It also writes `audit/models-dev-names.json` (schema `agent-compass.models-dev-names/2`): for every alias name and alias
target and every row of the shipped `accounts.json`, and every model name a saved gateway catalog or the shipped launch
table offers, each listing of that exact id in the bundled snapshot as `[provider, canonical id, input price, output
price]` (the canonical id is the listing's `canonical_model_id`, else `<provider>/<id>` on a provider whose id is a maker
namespace, else null). audit/gateway_coverage.py checks the aliases against it, and whether an offered name is, on its
provider, the weights of the row it resolves to.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCHEMA = 'agent-compass.gateway-catalog/3'
MAKERS_SCHEMA = 'agent-compass.magpie-makers/1'
NAMES_SCHEMA = 'agent-compass.models-dev-names/2'
OUT_DIR = Path(__file__).resolve().parent / 'gateway-catalogs'
MAKERS = Path(__file__).resolve().parent / 'magpie-makers.json'
NAMES = Path(__file__).resolve().parent / 'models-dev-names.json'
ACCOUNTS = Path(__file__).resolve().parents[1] / 'skills' / 'agent-compass' / 'references' / 'accounts.json'


def line_of(text, idx):
    return text.count('\n', 0, idx) + 1


def strip_go(text):
    """`text` with Go line comments blanked (offsets kept), so brace matching ignores them."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            i = j + 1
        elif c == '`':
            i = text.index('`', i + 1) + 1
        elif text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            out[i:j] = ' ' * (j - i)
            i = j
        else:
            i += 1
    return ''.join(out)


def match_brace(s, i):
    depth = 0
    while i < len(s):
        c = s[i]
        if c == '"':
            j = i + 1
            while s[j] != '"':
                j += 2 if s[j] == '\\' else 1
            i = j
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError('unbalanced braces')


def top_entries(s, start, end):
    i, out = start, []
    while i < end:
        j = s.find('{', i, end)
        if j < 0:
            break
        k = match_brace(s, j)
        out.append((j, k))
        i = k + 1
    return out


def field_str(body, name):
    m = re.search(r'\b' + name + r':\s*"([^"]*)"', body)
    return m.group(1) if m else None


def need(pattern, text, where, flags=0):
    m = re.search(pattern, text, flags)
    if not m:
        raise SystemExit(f'{where}: the source no longer has {pattern!r}; the reader must follow it')
    return m


def go_strings(block):
    return re.findall(r'"([^"]*)"', block)


class Rules:
    """The name rules Magpie's catalog applies, read from its source."""

    def __init__(self, src):
        cat = (src / 'internal/catalog/catalog.go').read_text(encoding='utf-8')
        m = need(r'var reasoningOffProviders = map\[string\]struct\{\}\{', cat, 'catalog.go')
        self.reasoning_off = set(go_strings(cat[m.end():match_brace(cat, m.end() - 1)]))
        m = need(r'func textModel\(m mdModel\) bool \{', cat, 'catalog.go')
        body = cat[m.end():match_brace(cat, m.end() - 1)]
        self.text_bad = go_strings(need(r'range \[\]string\{([^}]*)\}', body, 'catalog.go textModel').group(1))
        self.variant = re.compile(need(r'variantSuffix = lazyre\.New\(`([^`]+)`\)', cat, 'catalog.go').group(1))
        self.geos = go_strings(need(r'var bedrockGeos = \[\]string\{([^}]*)\}', cat, 'catalog.go').group(1))
        ultra = (src / 'internal/provider/ultra.go').read_text(encoding='utf-8')
        self.dated = re.compile(need(r'datedSuffix = lazyre\.New\(`([^`]+)`\)', ultra, 'ultra.go').group(1))


def presets(src):
    """[{id, kind, hosts, catalogs, models: [(id, line)], line}] from presets.go, in source order."""
    s = strip_go((src / 'internal/provider/presets.go').read_text(encoding='utf-8'))
    lists = {}
    for m in re.finditer(r'(\w+)\s*=\s*\[\]string\{', s):
        b = s.index('{', m.start())
        lists[m.group(1)] = (go_strings(s[b:match_brace(s, b)]), line_of(s, m.start()))
    m = need(r'var presets = \[\]PresetDef\{', s, 'presets.go')
    b = s.index('{', m.start())
    e = match_brace(s, b)
    consts = {}
    for f in sorted((src / 'internal/provider').glob('*.go')):
        if not f.name.endswith('_test.go'):
            for cm in re.finditer(r'(?m)^\s*(?:const\s+)?(\w+Preset)\s*=\s*"([^"]+)"', f.read_text(encoding='utf-8')):
                consts[cm.group(1)] = cm.group(2)
    out, unread = [], []
    for pb, pe in top_entries(s, b + 1, e):
        body = s[pb:pe + 1]
        pid = field_str(body, 'ID')
        if not pid:
            ident = re.match(r'\{\s*ID:\s*(\w+)', body)
            pid = consts.get(ident.group(1)) if ident else None
        if not pid:
            unread.append(line_of(s, pb))
            continue
        regions = []
        rm = re.search(r'Regions:\s*\[\]Region\{', body)
        own = body
        if rm:
            rb = body.index('{', rm.start())
            re_ = match_brace(body, rb)
            regions = [body[xb:xe + 1] for xb, xe in top_entries(body, rb + 1, re_)]
            own = body[:rb] + body[re_:]
        cats, models = [], []
        for part in [own] + regions:
            c = field_str(part, 'Catalog')
            if c:
                cats += [x.strip().lower() for x in re.split(r'[ ,]', c) if x.strip()]
            mm = re.search(r'\bModels:\s*(\[\]string\{[^}]*\}|\w+)', part)
            if mm:
                if mm.group(1).startswith('[]string'):
                    models += [(x, line_of(s, s.find(mm.group(0), pb))) for x in go_strings(mm.group(1))]
                elif mm.group(1) in lists:
                    ids, ln = lists[mm.group(1)]
                    models += [(x, ln) for x in ids]
        kind = re.search(r'Kind:\s*Kind(\w+)', own)
        out.append({'id': pid, 'kind': kind.group(1).lower() if kind else None, 'hosts': 'Hosts: true' in own,
                    'catalogs': list(dict.fromkeys(cats)), 'models': models, 'line': line_of(s, pb)})
    if unread:
        raise SystemExit(f'presets.go: presets without a readable ID at lines {unread}')
    return out


def go_list(src, rel, var):
    text = (src / rel).read_text(encoding='utf-8')
    m = need(r'var ' + var + r' = \[\]catalog\.Model\{', text, rel)
    b = text.index('{', m.start())
    return [mm.group(1) for mm in re.finditer(r'\{ID:\s*"([^"]+)"', text[b:match_brace(text, b)])]


class Snapshot:
    """The bundled models.dev snapshot and Magpie's effort rules over it."""

    def __init__(self, src, rules):
        self.all = json.load(gzip.open(src / 'internal/catalog/bundled/api.json.gz'))
        self.rules = rules
        # catalog.go's `efforts` table: one vote per provider and list for a model's bare id, a provider's own off level
        # left out (efforts(""))
        votes, voted = {}, set()
        for pid in sorted(self.all):
            for mid, m in sorted(self.all[pid].get('models', {}).items()):
                e = ','.join(self.efforts(m))
                if e and (pid, self.bare(mid), e) not in voted:
                    voted.add((pid, self.bare(mid), e))
                    votes.setdefault(self.bare(mid), Counter())[e] += 1
        # catalog.mostListed: the list most providers give; a tie goes to the shorter, then the smaller
        self.most = {k: min(v.items(), key=lambda kv: (-kv[1], kv[0].count(','), kv[0]))[0].split(',')
                     for k, v in votes.items()}

        # the maker namespaces: the part of every canonical id before its slash
        self.namespaces = {m['canonical_model_id'].split('/')[0] for p in self.all.values()
                           for m in p.get('models', {}).values() if m.get('canonical_model_id')}
        self.makers_by = {}     # {('id' | 'bare', name): {maker}} over the listings whose maker is known
        for pid in sorted(self.all):
            for mid, m in self.all[pid].get('models', {}).items():
                k = self.listing_maker(pid, m)
                if k:
                    self.makers_by.setdefault(('id', mid), set()).add(k)
                    self.makers_by.setdefault(('bare', self.bare(mid)), set()).add(k)

    @staticmethod
    def bare(mid):
        return mid.lower().rsplit('/', 1)[-1]

    def listing_maker(self, pid, m):
        """The maker of one models.dev listing: its canonical id's namespace, else the provider when the provider's id is
        itself a namespace (the maker's own listing), else None."""
        if m.get('canonical_model_id'):
            return m['canonical_model_id'].split('/')[0]
        return pid if pid in self.namespaces else None

    def maker_of(self, mid):
        """The one maker the snapshot's listings of `mid` (then of its bare id) agree on, else None."""
        for k in (('id', mid), ('bare', self.bare(mid))):
            found = self.makers_by.get(k) or set()
            if found:
                return next(iter(found)) if len(found) == 1 else None
        return None

    def efforts(self, m, pid=''):
        out, toggle = [], False
        for r in m.get('reasoning_options') or []:
            if r.get('type') == 'toggle':
                toggle = True
            elif r.get('type') == 'effort':
                out = list(r.get('values') or [])
        if pid in self.rules.reasoning_off and toggle and out and 'none' not in out:
            out = ['none'] + out
        return out

    @staticmethod
    def budgeted(m):
        return any(r.get('type') == 'budget_tokens' for r in m.get('reasoning_options') or [])

    @staticmethod
    def thinks(m):
        return bool(m.get('reasoning'))

    def levels(self, pid, m, override=True):
        own = pid if override else ''
        e = self.efforts(m, own)
        if e or not self.thinks(m) or not m.get('family') or self.budgeted(m):
            return e
        kin = None
        for x in self.all[pid]['models'].values():
            if x.get('family') != m['family'] or not self.thinks(x) or x['id'] == m['id']:
                continue
            if m.get('release_date') and (x.get('release_date') or '') > m['release_date']:
                continue
            xd = x.get('release_date') or ''
            if kin and (xd < kin[1] or xd == kin[1] and x['id'] <= kin[0]):
                continue
            xe = self.efforts(x, own)
            if xe:
                kin = (x['id'], xd, xe)
        return kin[2] if kin else []

    def text_model(self, m):
        outs = (m.get('modalities') or {}).get('output') or []
        if outs and 'text' not in outs:
            return False
        return not any(b in m['id'] for b in self.rules.text_bad)

    def provider(self, pid):
        p = self.all.get(pid) or {}
        return sorted((m['id'], self.levels(pid, m), self.listing_maker(pid, m) or self.maker_of(m['id']))
                      for m in p.get('models', {}).values() if self.text_model(m))

    def listed_by(self, makers, mid):
        b = self.bare(mid)
        for g in self.rules.geos:
            if b.startswith(g):
                b = b[len(g):]
                break
        for want in dict.fromkeys((b, b.split('(')[0], b.split(':')[0])):
            for c in makers:
                for key in sorted(self.all.get(c, {}).get('models', {})):
                    m = self.all[c]['models'][key]
                    if self.bare(key) == want:
                        e = self.levels(c, m, override=False)
                        if e or not self.budgeted(m):
                            return e, True
        return [], False

    def versionless(self, b):
        parts = b.split('-')
        versioned = named = False
        for i, s in enumerate(parts):
            v = s[1:] if s.startswith('v') else s
            if v and v.strip('0123456789.') == '' and v[0] != '.':
                parts[i], versioned = '#', True
            else:
                named = True
        return '-'.join(parts), versioned and named

    def kin(self, makers, mid):
        shape, ok = self.versionless(self.bare(mid))
        if not ok:
            return [], False
        best = None
        for c in makers:
            for key in sorted(self.all.get(c, {}).get('models', {})):
                m = self.all[c]['models'][key]
                if not (self.thinks(m) or self.efforts(m)) or self.budgeted(m):
                    continue
                if self.versionless(self.bare(key)) != (shape, True):
                    continue
                d = m.get('release_date') or ''
                if best is None or d > best[0] or d == best[0] and c == best[1] and key > best[2]:
                    best = (d, c, key, self.levels(c, m, override=False))
        return (best[3], True) if best else ([], False)

    def borrowed(self, makers, mid):
        """Magpie's borrowedEfforts (internal/provider/models.go)."""
        e, ok = self.listed_by(makers, mid)
        if ok:
            return e
        if self.most.get(self.bare(mid)):
            return self.most[self.bare(mid)]
        e, ok = self.kin(makers, mid)
        if ok:
            return e
        m = self.rules.variant.search(mid)
        if m and m.start() > 0:
            return self.borrowed(makers, mid[:m.start()])
        base = self.rules.dated.sub('', mid)
        if base != mid and base:
            return self.borrowed(makers, base)
        return []


def provider_makers(path, offered, vendors):
    """{provider: maker} from the declared makers file: every vendor preset and account that offers rows has an entry
    {maker, source, reads} naming the Magpie source line it reads; a maker is a namespace or null."""
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    if doc.get('schema') != MAKERS_SCHEMA or not isinstance(doc.get('makers'), dict):
        raise SystemExit(f'{path}: not a {MAKERS_SCHEMA} document with makers')
    need_ = sorted(p for p in offered if p in vendors or p.startswith('account:'))
    lacking = [p for p in need_ if p not in doc['makers']]
    bad = [p for p, e in doc['makers'].items() if not isinstance(e, dict) or set(e) != {'maker', 'source', 'reads'}
           or not re.match(r'^internal/[\w/]+\.go:\d+$', str(e.get('source')))]
    if lacking or bad:
        raise SystemExit(f'{path}: no entry for {lacking}; entries without {{maker, source, reads}}: {bad}')
    return {p: doc['makers'][p]['maker'] for p in need_}


def build(src, version, commit, makers_path=MAKERS):
    rules = Rules(src)
    snap = Snapshot(src, rules)
    ps = presets(src)
    makers = list(dict.fromkeys(c for p in ps if p['kind'] == 'vendor' and not p['hosts'] for c in p['catalogs']))
    rows, maker = {}, {}

    def add(provider, model, efforts, by):
        rows.setdefault((provider, model), efforts or None)
        if maker.get((provider, model)) is None:
            maker[(provider, model)] = by

    for p in ps:
        for mid, _ in p['models']:
            add(p['id'], mid, snap.borrowed(makers, mid), snap.maker_of(mid))
        for c in p['catalogs']:
            for mid, e, by in snap.provider(c):
                add(p['id'], mid, e, by)
    for mid, e, by in snap.provider('anthropic'):
        add('account:claude', mid, e, by)
    for var, account in (('geminiModels', 'account:gemini-cli'), ('antigravityModels', 'account:antigravity')):
        for mid in go_list(src, 'internal/provider/google.go', var):
            add(account, mid, snap.borrowed(makers, mid), snap.maker_of(mid))
    gate = provider_makers(makers_path, {p for p, _ in rows}, {p['id'] for p in ps if p['kind'] == 'vendor'})
    bundled = need(r'bundledAt = time\.Date\(([^)]*)\)', (src / 'internal/catalog/bundled.go').read_text(encoding='utf-8'),
                   'bundled.go').group(1)
    return {
        'schema': SCHEMA,
        'host': 'magpie',
        'version': version,
        'source': {'repository': 'https://github.com/yetone/magpie', 'commit': commit,
                   'models_dev_bundled_at': re.sub(r'\s+', ' ', bundled)},
        'statement': ("Magpie's catalog as fixed in its source (presets, their models.dev catalogs and own lists, the "
                      "Claude and Google accounts), reduced to provider, model and the efforts Magpie offers; built by "
                      "audit/magpie_catalog.py, nothing of an install is kept"),
        'provider_kind': 'magpie_preset',
        'gate': {'makers': gate,
                 'statement': ("a (provider, model) pair is gated when the model's maker (`maker`: the models.dev "
                               "canonical_model_id namespace) is the provider's maker (`makers`, declared with its Magpie "
                               "source line in audit/magpie-makers.json); every other pair is a report (owner 2026-11-18: "
                               "only first-party is gated; per pair, review 2026-11-19)")},
        'rows': [{'provider': p, 'model': m, 'efforts': e, 'maker': maker[(p, m)]} for (p, m), e in sorted(rows.items())],
    }


def names(src, version, commit, accounts, offered=()):
    """The models.dev listings of every alias name and target and every row of `accounts`, and of every `offered` model
    name (see the module text)."""
    snap = Snapshot(src, Rules(src))
    aliases = accounts.get('aliases') or {}
    out = {n: [] for n in sorted(set(aliases) | set(aliases.values()) | set(accounts.get('models') or {}) | set(offered))}
    for pid in sorted(snap.all):
        for mid, m in sorted(snap.all[pid].get('models', {}).items()):
            if mid in out:
                cost = m.get('cost') or {}
                canon = m.get('canonical_model_id') or (f'{pid}/{mid}' if pid in snap.namespaces else None)
                out[mid].append([pid, canon, cost.get('input'), cost.get('output')])
    bundled = need(r'bundledAt = time\.Date\(([^)]*)\)', (src / 'internal/catalog/bundled.go').read_text(encoding='utf-8'),
                   'bundled.go').group(1)
    return {'schema': NAMES_SCHEMA,
            'source': {'repository': 'https://github.com/yetone/magpie', 'version': version, 'commit': commit,
                       'models_dev_bundled_at': re.sub(r'\s+', ' ', bundled)},
            'statement': ("every listing, in the models.dev snapshot Magpie bundles, of each alias name and alias target "
                          "and each row of accounts.json and each model name a saved gateway catalog or the shipped launch "
                          "table offers, as [provider, canonical id, input price, output price]; built by "
                          "audit/magpie_catalog.py, read by audit/gateway_coverage.py (the alias check and the identity of "
                          "an offered name on its provider)"),
            'names': out}


def dumps_names(doc):
    """The names document with one name per line: stable bytes for a stable input."""
    head = {k: v for k, v in doc.items() if k != 'names'}
    text = json.dumps(head, indent=1, ensure_ascii=False)[:-2]
    rows = ',\n'.join(f'  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}' for k, v in doc['names'].items())
    return f'{text},\n "names": {{\n{rows}\n }}\n}}\n'


def dumps(doc):
    """The document as JSON with one row per line: stable bytes for a stable input."""
    head = {k: v for k, v in doc.items() if k != 'rows'}
    text = json.dumps(head, indent=1, ensure_ascii=False)[:-2]
    rows = ',\n'.join('  ' + json.dumps(r, ensure_ascii=False) for r in doc['rows'])
    return f'{text},\n "rows": [\n{rows}\n ]\n}}\n'


def git(src, *args):
    return subprocess.run(['git', '-C', str(src), *args], capture_output=True, text=True, check=True).stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('checkout')
    ap.add_argument('--version', help='default: the checkout\'s tag without its leading v')
    ap.add_argument('--commit', help='default: the checkout\'s HEAD')
    ap.add_argument('--out', help='default: audit/gateway-catalogs/magpie-<version>.json')
    ap.add_argument('--names-out', default=str(NAMES), help='default: audit/models-dev-names.json')
    ap.add_argument('--makers', default=str(MAKERS), help='default: audit/magpie-makers.json')
    args = ap.parse_args(argv)
    src = Path(args.checkout)
    version = args.version or git(src, 'describe', '--tags', '--exact-match').removeprefix('v')
    commit = args.commit or git(src, 'rev-parse', 'HEAD')
    doc = build(src, version, commit, args.makers)
    out = Path(args.out) if args.out else OUT_DIR / f'magpie-{version}.json'
    out.write_text(dumps(doc), encoding='utf-8', newline='\n')
    import gateway_coverage     # the shipped launch table's rows, read as the coverage check reads them
    accounts = json.loads(ACCOUNTS.read_text(encoding='utf-8'))
    offered = {r['model'] for r in doc['rows']} | {r['model'] for r in gateway_coverage.launch_rows(accounts)}
    for path in sorted(OUT_DIR.glob('*.json')):
        if path.resolve() != out.resolve():
            offered |= {r['model'] for r in json.loads(path.read_text(encoding='utf-8')).get('rows') or ()}
    listed = names(src, version, commit, accounts, offered)
    Path(args.names_out).write_text(dumps_names(listed), encoding='utf-8', newline='\n')
    print(f"{args.names_out}: {len(listed['names'])} names (aliases, targets, rows and offered names), "
          f"{sum(1 for v in listed['names'].values() if v)} listed by models.dev")
    print(f"{out}: {len(doc['rows'])} rows, {sum(1 for r in doc['rows'] if r['efforts'])} with efforts, "
          f"{sum(1 for r in doc['rows'] if r['maker'] and doc['gate']['makers'].get(r['provider']) == r['maker'])} gated")
    return 0


if __name__ == '__main__':
    sys.exit(main())
