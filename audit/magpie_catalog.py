"""Build the saved Magpie catalog for the coverage check from a Magpie source checkout (static read: no build, no network).

    git -c core.longpaths=true clone -q --depth 1 --branch v<version> https://github.com/yetone/magpie <dir>
    python -X utf8 audit/magpie_catalog.py <dir>

writes `audit/gateway-catalogs/magpie-<version>.json` (schema `agent-compass.gateway-catalog/2`, `provider_kind:
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

`gate.providers` (the coverage check gates only these rows; the rest of the catalog is a report) is Magpie's own set of
makers, `makerCatalogs` in `internal/provider/models.go`: the presets of `Kind: KindVendor` ("the model's own maker") that
are not `Hosts` ("a vendor serving other makers' models as well as its own ... whose list is no maker's word on theirs"),
and the accounts whose list the source fixes (`account:claude`, `account:gemini-cli`, `account:antigravity`).
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

SCHEMA = 'agent-compass.gateway-catalog/2'
ACCOUNTS = ('account:antigravity', 'account:claude', 'account:gemini-cli')
OUT_DIR = Path(__file__).resolve().parent / 'gateway-catalogs'


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

    @staticmethod
    def bare(mid):
        return mid.lower().rsplit('/', 1)[-1]

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
        return sorted((m['id'], self.levels(pid, m)) for m in p.get('models', {}).values() if self.text_model(m))

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


def build(src, version, commit):
    rules = Rules(src)
    snap = Snapshot(src, rules)
    ps = presets(src)
    makers = list(dict.fromkeys(c for p in ps if p['kind'] == 'vendor' and not p['hosts'] for c in p['catalogs']))
    rows = {}

    def add(provider, model, efforts):
        rows.setdefault((provider, model), efforts or None)

    for p in ps:
        for mid, _ in p['models']:
            add(p['id'], mid, snap.borrowed(makers, mid))
        for c in p['catalogs']:
            for mid, e in snap.provider(c):
                add(p['id'], mid, e)
    for mid, e in snap.provider('anthropic'):
        add('account:claude', mid, e)
    for var, account in (('geminiModels', 'account:gemini-cli'), ('antigravityModels', 'account:antigravity')):
        for mid in go_list(src, 'internal/provider/google.go', var):
            add(account, mid, snap.borrowed(makers, mid))
    present = {p for p, _ in rows}
    gate = sorted({p['id'] for p in ps if p['kind'] == 'vendor' and not p['hosts']} & present) + \
        [a for a in ACCOUNTS if a in present]
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
        'gate': {'providers': gate,
                 'statement': ("Magpie's own makers (internal/provider/models.go makerCatalogs: presets of Kind vendor, "
                               "\"the model's own maker\", without Hosts) and the accounts whose list the source fixes; "
                               "owner 2026-11-18: only this first-party tier is gated, the rest is a report")},
        'rows': [{'provider': p, 'model': m, 'efforts': e} for (p, m), e in sorted(rows.items())],
    }


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
    args = ap.parse_args(argv)
    src = Path(args.checkout)
    version = args.version or git(src, 'describe', '--tags', '--exact-match').removeprefix('v')
    commit = args.commit or git(src, 'rev-parse', 'HEAD')
    doc = build(src, version, commit)
    out = Path(args.out) if args.out else OUT_DIR / f'magpie-{version}.json'
    out.write_text(dumps(doc), encoding='utf-8', newline='\n')
    print(f"{out}: {len(doc['rows'])} rows, {sum(1 for r in doc['rows'] if r['efforts'])} with efforts, "
          f"{len(doc['gate']['providers'])} gated providers")
    return 0


if __name__ == '__main__':
    sys.exit(main())
