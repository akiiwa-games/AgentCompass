"""Discover local launchable accounts; product configurations remain read-only.

Only allowlisted metadata leaves the adapters. Secrets are used for identity binding
only; product files and cached usage are read without any network requests.
"""
from __future__ import annotations

import base64
import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import tomllib
from model_resolution import resolve_canonical_model

MAX_BYTES = 5 * 1024 * 1024
GROK_BOT_CLI = Path(__file__).resolve().parents[1] / 'grok-bot/runtime/bin/gbot.mjs'
# Paseo built-in providers recognized from their Paseo entry without a model catalog.
PASEO_RECOGNIZED_ONLY = ('copilot', 'muse', 'dsh')


PLAN_TEMPLATES = Path(__file__).resolve().parents[1] / 'references/plan-templates.json'
TEMPLATE_KEYS = ('windows', 'capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour')
POOL_KEYS = ('windows', 'burn_pct_per_worker_hour', 'capacity_usd', 'capacity_change', 'observed', 'expires_at')


def quota_sources(accounts):
    """Accounts that draw on another account's quota, as declared in the account document.

    Either a top-level `quota_sources` map ({account: source}) or a per-account `quota_source`.
    Nothing is pooled by default: two products only share a pool when the user's config says so.
    """
    accounts = obj(accounts)
    declared = {k: v for k, v in obj(accounts.get('quota_sources')).items()}
    for name, spec in obj(accounts.get('accounts')).items():
        if isinstance(spec, dict) and 'quota_source' in spec:
            declared[name] = spec['quota_source']
    return {name: source for name, source in declared.items()
            if isinstance(name, str) and isinstance(source, str) and source and source != name}


def quota_source(account, accounts=None):
    return quota_sources(accounts).get(account, account) if accounts else account


def apply_shared_quota(accounts):
    """A pooled account mirrors its source's quota state; execution settings stay its own."""
    specs = accounts['accounts']
    for name, source_name in quota_sources(accounts).items():
        if name not in specs or source_name not in specs:
            continue
        bot, source = specs[name], specs[source_name]
        bot['quota_source'] = source_name
        for key in POOL_KEYS:
            if key in source:
                bot[key] = copy.deepcopy(source[key])
            else:
                bot.pop(key, None)
        # An unread source meter is unknown, not free, for the account drawing on it.
        bot.pop('unmetered', None)
        bot.pop('unlimited', None)


def plan_defaults(accounts, family, plan):
    """Quota template for a subscription plan, independent of any configured account instance.

    The account document's `plan_templates` overrides the shipped references/plan-templates.json
    per family and plan. A template of `{"alias": "<plan>"}` resolves to that plan's template. Only
    `windows`, `capacity_usd`, `capacity_change` and `burn_pct_per_worker_hour` are copied onto an
    account; other keys (provenance, basis, price) are documentation.
    """
    shipped, _ = read_json(PLAN_TEMPLATES)
    overrides = obj(obj(obj(accounts).get('plan_templates')).get(family))
    base = obj(shipped.get(family))
    template = None
    for _ in range(3):
        configured = overrides.get(plan)
        template = configured if isinstance(configured, dict) else obj(base.get(plan))
        plan = template.get('alias')
        if not isinstance(plan, str) or not template:
            break
    return copy.deepcopy({k: v for k, v in template.items() if k != 'alias'}) or None


def fill_template_gaps(spec, template):
    """Add template values the account does not set; never overwrite what the account already declares."""
    for key in TEMPLATE_KEYS:
        if key not in template:
            continue
        have = spec.get(key)
        if not have:
            spec[key] = copy.deepcopy(template[key])
        elif isinstance(have, dict) and isinstance(template[key], dict) and key != 'windows':
            for wid, value in template[key].items():
                have.setdefault(wid, copy.deepcopy(value))


def digest(*values):
    return hashlib.sha256('\0'.join(str(v or '') for v in values).encode()).hexdigest()


def obj(value):
    return value if isinstance(value, dict) else {}


def array(value):
    return value if isinstance(value, list) else []


def read_json(path, expected=dict):
    try:
        if path.stat().st_size > MAX_BYTES:
            return expected(), 'oversized'
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return (value, None) if isinstance(value, expected) else (expected(), 'invalid_shape')
    except FileNotFoundError:
        return expected(), 'missing'
    except (OSError, ValueError, UnicodeError):
        return expected(), 'unreadable'


def read_toml(value):
    try:
        return tomllib.loads(value) if isinstance(value, str) else {}
    except ValueError:
        return {}


def endpoint(value):
    value = str(value or '').rstrip('/')
    for suffix in ('/v1/chat/completions', '/v1/messages', '/v1/responses', '/v1'):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def plan_name(family, value, tier=''):
    value = value.lower() if isinstance(value, str) else ''
    tier = tier if isinstance(tier, str) else ''
    if family == 'claude' and value == 'max':
        return 'max20' if '20x' in tier else 'max5' if '5x' in tier else 'max'
    return value or None


def auth_facts(family, auth, home, profile=None):
    """Return metadata plus in-memory binding material; never return raw auth."""
    auth = auth if isinstance(auth, dict) else {}
    if family == 'claude':
        oauth = auth.get('claudeAiOauth') or {}
        oauth = oauth if isinstance(oauth, dict) else {}
        token = oauth.get('accessToken') if isinstance(oauth.get('accessToken'), str) else None
        profile = obj(profile)
        account = profile.get('accountUuid') or profile.get('emailAddress')
        identity = digest('claude-account', account, profile.get('organizationUuid')) if token and account else digest('claude', token) if token else None
        return {'present': bool(token), 'identity': identity,
                'credential_identity': digest('claude', token) if token else None,
                'credential_version': digest(token, oauth.get('refreshToken')) if token else None,
                'credential_key': digest(home.resolve(), token) if token else None,
                'plan': plan_name(family, oauth.get('subscriptionType'), oauth.get('rateLimitTier', ''))}
    tokens = auth.get('tokens') or {}
    tokens = tokens if isinstance(tokens, dict) else {}
    token = tokens.get('access_token') if isinstance(tokens.get('access_token'), str) else None
    account = tokens.get('account_id') if isinstance(tokens.get('account_id'), str) else None
    claims = {}
    try:
        encoded = str(tokens.get('id_token') or '').split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        claims = claims.get('https://api.openai.com/auth') or {}
        claims = claims if isinstance(claims, dict) else {}
    except (ValueError, IndexError, UnicodeError, AttributeError):
        claims = {}
    return {'present': bool(token), 'identity': digest('codex', account or token) if token else None,
            'credential_key': digest(home.resolve(), str(token) + ':' + str(account or '')) if token else None,
            'plan': plan_name(family, claims.get('chatgpt_plan_type'))}


def resolve_definition(name, definitions):
    env, seen = {}, set()
    current = name
    enabled = obj(definitions.get(name)).get('enabled') is not False
    while current not in seen:
        seen.add(current)
        spec = definitions.get(current) or {}
        if not isinstance(spec, dict):
            break
        env = {**{k: v for k, v in obj(spec.get('env')).items() if isinstance(v, str)}, **env}
        parent = spec.get('extends')
        if not parent:
            return current, env, enabled
        if not isinstance(parent, str):
            return current, env, False
        current = parent
    return name, env, False


def product_config(family, config):
    env = config.get('env') if isinstance(config.get('env'), dict) else {}
    if family == 'claude':
        token = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or ''
        return (endpoint(env.get('ANTHROPIC_BASE_URL') or ('https://api.anthropic.com' if token else '')),
                token,
                [v for k, v in env.items() if 'MODEL' in k and isinstance(v, str)])
    parsed = read_toml(config.get('config'))
    selected = parsed.get('model_provider') if isinstance(parsed.get('model_provider'), str) else ''
    provider = obj(obj(parsed.get('model_providers')).get(selected))
    auth = config.get('auth') or {}
    auth = auth if isinstance(auth, dict) else {}
    token = provider.get('experimental_bearer_token') or auth.get('OPENAI_API_KEY') or ''
    return (endpoint(provider.get('base_url') or ('https://api.openai.com' if token else '')),
            token,
            [parsed['model']] if isinstance(parsed.get('model'), str) else [])


def cc_switch(path):
    """Do not inspect logs, execute usage_script, or open the DB writable."""
    if not path.exists():
        return [], 'missing'
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute('SELECT id, app_type, name, settings_config, category, is_current '
                              'FROM providers LIMIT 513').fetchall()
            health = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_health'").fetchone():
                health = {(app, ident): bool(ok) for ident, app, ok in db.execute(
                    'SELECT provider_id, app_type, is_healthy FROM provider_health LIMIT 512')}
        result = []
        partial = len(rows) > 512
        for ident, family, name, raw, category, current in rows[:512]:
            try:
                config = json.loads(raw)
                if not isinstance(config, dict):
                    partial = True
                    continue
            except (ValueError, TypeError):
                partial = True
                continue
            result.append(({'id': f'cc-switch:{family}:{ident}', 'source': 'cc-switch',
                            'family': family, 'name': name, 'current': bool(current),
                            'category': category, 'healthy': health.get((family, ident))}, config))
        return result, 'partial' if partial else None
    except (sqlite3.Error, OSError):
        return [], 'unreadable'


def installed_executables(product, home, environ):
    """Bounded OS installation hints; never execute the application or scan a drive."""
    paths = []
    found = shutil.which(product, path=environ.get('PATH'))
    if found:
        paths.append(Path(found).resolve())
    explicit = environ.get('MAGPIE_EXE' if product == 'magpie' else 'CC_SWITCH_EXE')
    if explicit:
        paths.insert(0, Path(explicit).expanduser())
    if home.resolve() == Path.home().resolve():
        repository = Path(__file__).resolve().parents[3]
        for directory in (repository, repository / product, repository / 'tools', repository / 'tools' / product):
            candidate = directory / (product + ('.exe' if os.name == 'nt' else ''))
            if candidate.is_file():
                paths.append(candidate)
    if os.name != 'nt' or home.resolve() != Path.home().resolve():
        return paths
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + product + '.exe') as key:
                value = winreg.QueryValue(key, None)
                paths.append(Path(value.strip('"')))
        except OSError:
            pass
        for subkey in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                       r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                with winreg.OpenKey(hive, subkey) as root:
                    for index in range(min(winreg.QueryInfoKey(root)[0], 1024)):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                name = winreg.QueryValueEx(key, 'DisplayName')[0]
                                if str(name).lower().replace(' ', '-') != product:
                                    continue
                                folder = winreg.QueryValueEx(key, 'InstallLocation')[0]
                                paths.append(Path(folder) / (product + '.exe'))
                        except OSError:
                            continue
            except OSError:
                pass
    return list(dict.fromkeys(paths))


def product_root(product, home, environ):
    explicit = environ.get('MAGPIE_CONFIG_DIR' if product == 'magpie' else 'CC_SWITCH_HOME')
    if explicit:
        return Path(explicit).expanduser()
    for exe in installed_executables(product, home, environ):
        if product == 'magpie':
            data = exe.parent / 'data'
            if (exe.parent / '.portable').is_file() or any((data / f).is_file() for f in ('providers.json', 'logins.json')):
                return data
        elif (exe.parent / 'portable.ini').is_file():
            return exe.parent / 'data/app'
    if product == 'magpie':
        return Path(environ.get('XDG_CONFIG_HOME') or home / '.config') / 'magpie'
    # CC Switch's own Tauri store records its relocated database; device settings
    # remain in the default folder. Do not fall back to an old DB after relocation.
    stores = [Path(environ.get('APPDATA') or home / 'AppData/Roaming'),
              Path(environ.get('XDG_CONFIG_HOME') or home / '.config'),
              Path(environ.get('XDG_DATA_HOME') or home / '.local/share'),
              home / 'Library/Application Support']
    for store in stores:
        data, _ = read_json(store / 'com.ccswitch.desktop/app_paths.json')
        value = data.get('app_config_dir_override')
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    return home / '.cc-switch'


def read_magpie(home, environ, result, bindings):
    root = product_root('magpie', home, environ)
    stored, error = read_json(root / 'providers.json')
    logins, login_error = read_json(root / 'logins.json', list)
    result['sources']['magpie_config'] = error or 'read'
    result['sources']['magpie_logins'] = login_error or 'read'
    result['source_paths']['magpie'] = str(root)
    if 'providers' in stored and not isinstance(stored['providers'], list):
        result['sources']['magpie_config'] = 'invalid_shape'
    for item in array(stored.get('providers')):
        if not isinstance(item, dict):
            result['sources']['magpie_config'] = 'partial'
            continue
        bases = {endpoint(item.get(k)) for k in ('anthropic', 'responses', 'chat') if item.get(k)}
        keys = {digest(k['key']): bool(k.get('off')) for k in array(item.get('keys')) if isinstance(k, dict) and k.get('key')}
        if item.get('key'):
            keys[digest(item['key'])] = False  # the active key wins over a duplicate backup
        matched = [name for name, binding in bindings.items() if binding['base'] in bases and binding['key'] in keys]
        builtin = item.get('id') in ('claude', 'codex', 'antigravity') and not bases and not keys
        if builtin:
            matched = [name for name, row in result['accounts'].items()
                       if row['family'] == item['id'] and row.get('billing') == 'subscription']
        disabled = item.get('off') or (builtin and item.get('hidden'))
        paused = [name for name in matched if disabled or (not builtin and keys[bindings[name]['key']])]
        models = [m for m in array(item.get('models')) if isinstance(m, str)]
        for name in matched:
            result['accounts'][name]['sources'].append('magpie-config')
            if name in paused:
                result['accounts'][name].update(status='paused', reason='disabled_in_magpie')
            # Magpie's empty picks mean defaults; noModels is its explicit Select none.
            if models or item.get('noModels') is True:
                result['accounts'][name]['models'] = models
        result['saved'].append({'id': 'magpie:' + str(item.get('id')), 'source': 'magpie_config',
            'name': item.get('name'), 'status': 'paused' if disabled or paused else 'bound' if matched else 'saved',
            'bound_to': matched, 'models': models, 'removal_pauses': not builtin})
    for login in logins:
        if not isinstance(login, dict):
            result['sources']['magpie_logins'] = 'partial'
            continue
        family = login.get('agent')
        facts = auth_facts(family, obj(login.get('auth')), root, login.get('profile')) if family in ('claude', 'codex') else {}
        matched = [name for name, binding in bindings.items() if facts.get('identity')
                   and (binding['identity'] == facts['identity'] or (facts.get('credential_identity')
                        and facts['credential_identity'] == binding.get('credential_identity')))
                   and result['accounts'][name]['family'] == family]
        active = family in matched or login.get('held') is True
        enabled = (active or login.get('on') is True) and not any(login.get(k) for k in ('paused', 'hidden'))
        plan = facts.get('plan') or plan_name(family, login.get('plan'))
        for name in matched:
            result['accounts'][name]['sources'].append('magpie-logins')
            refused = login.get('lapsed') and (family != 'claude' or
                      (login.get('refused') or facts.get('credential_version')) == bindings[name].get('credential_version'))
            if not enabled or refused:
                result['accounts'][name].update(status='paused', reason='account_disabled_in_magpie')
            if plan:
                result['accounts'][name]['plan'] = plan
        result['saved'].append({'id': 'magpie-login:' + digest(family, login.get('id') or login.get('user'))[:20],
            'source': 'magpie_logins', 'family': family, 'plan': plan, 'bound_to': matched,
            'status': 'paused' if not enabled or login.get('lapsed') else 'bound' if matched else 'saved',
            'credentials_present': bool(facts.get('present'))})


def apply_removals(result, previous):
    """A confirmed product removal pauses the same credential until restored.

    Missing/unreadable databases do not mean their accounts were deleted. Keep a
    tombstone only while that exact credential is still installed in the launcher.
    """
    if previous.get('schema') != result['schema']:
        return
    current = {r['id']: r for r in result['saved']}
    pending = list(previous.get('removed') or [])
    for item in previous.get('saved') or []:
        if result['sources'].get(item.get('source')) != 'read':
            if item.get('status') == 'paused':
                for name in item.get('bound_to') or []:
                    old = obj(previous.get('accounts')).get(name) or {}
                    row = result['accounts'].get(name) or {}
                    if old.get('status') == 'paused' and old.get('identity') and old['identity'] == row.get('identity'):
                        row.update(status='paused', reason='last_known_disabled_in_' + item['source'])
                        if item['id'] not in current:
                            result['saved'].append(item)
                            current[item['id']] = item
            continue
        new = current.get(item['id']) or {}
        if item.get('removal_pauses') is False:
            continue  # removing built-in subscription picks restores Magpie defaults
        for name in item.get('bound_to') or []:
            old = obj(previous.get('accounts')).get(name) or {}
            if name not in (new.get('bound_to') or []) and old.get('identity'):
                pending.append({'entry': item['id'], 'source': item['source'], 'account': name, 'identity': old['identity']})
    kept = {}
    for removed in pending:
        row = result['accounts'].get(removed['account']) or {}
        if not row.get('identity') or row['identity'] != removed.get('identity'):
            continue
        restored = removed['account'] in (current.get(removed['entry'], {}).get('bound_to') or [])
        if restored:
            continue
        row.update(status='paused', reason='removed_from_' + removed['source'])
        kept[(removed['entry'], removed['account'])] = removed
    result['removed'] = list(kept.values())


def consult_home(home, environ):
    """The ChatGPT Web bridge profile the consultation client uses: PRO_CONSULT_HOME, else the default folder."""
    value = str(environ.get('PRO_CONSULT_HOME') or '').strip()
    return Path(os.path.expandvars(value)).expanduser() if value else home / '.codex-chatgpt-web-consult'


def dot_configuration(home, environ):
    """Dot is configured by DOT2API_BASE_URL plus DOT2API_API_KEY, or a client.json with both values."""
    if str(environ.get('DOT2API_BASE_URL') or '').strip() and str(environ.get('DOT2API_API_KEY') or '').strip():
        return 'ready'
    config, error = read_json(home / '.dot2api/client.json')
    if error:
        return 'missing_configuration' if error == 'missing' else 'invalid_configuration'
    base, key = config.get('base_url'), config.get('api_key')
    valid = (isinstance(base, str) and base.strip().startswith(('http://', 'https://'))
             and isinstance(key, str) and bool(key.strip()))
    return 'ready' if valid else 'invalid_configuration'


def consult_configuration(web_home):
    """The same local checks the consultation client applies before it contacts the bridge."""
    config, error = read_json(web_home / 'config.json')
    if error:
        return 'missing_configuration' if error == 'missing' else 'invalid_configuration'
    valid = (config.get('mode') == 'browser-only' and config.get('browserInteractionMode') == 'automatic'
             and config.get('host') == '127.0.0.1' and isinstance(config.get('port'), int)
             and not isinstance(config.get('port'), bool) and config.get('proAvailable') is True)
    return 'ready' if valid else 'invalid_configuration'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


_SCOPE = []


class snapshot:
    """Within this block, argument-free discover() runs once and later calls get a copy: one routing decision sees one
    inventory instead of rescanning local product files five times. Only discovery is shared; quota, policy, disablement
    and reservations are read live. Nested blocks join the outer one."""

    def __enter__(self):
        self.owner = not _SCOPE
        if self.owner:
            _SCOPE.append({})
        return self

    def __exit__(self, *exc):
        if self.owner:
            _SCOPE.clear()


def discover(home=None, environ=None, now=None):
    if _SCOPE and home is None and environ is None and now is None:
        if 'inventory' not in _SCOPE[0]:
            _SCOPE[0]['inventory'] = _discover()
        return copy.deepcopy(_SCOPE[0]['inventory'])
    return _discover(home, environ, now)


def _discover(home=None, environ=None, now=None):
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path(environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    now = now or datetime.now(timezone.utc)
    state_dir = Path(environ.get('ROUTER_STATE_DIR') or home / '.agent-compass')
    paseo_path = Path(environ.get('PASEO_HOME') or home / '.paseo') / 'config.json'
    paseo, paseo_error = read_json(paseo_path)
    agents = paseo.get('agents') if isinstance(paseo.get('agents'), dict) else {}
    definitions = agents.get('providers') or {}
    if not isinstance(definitions, dict):
        definitions = {}
    cache, _ = read_json(Path(environ.get('ROUTER_USAGE_CACHE') or home / '.usage-float/usage-cache.json'))
    cache_rows = cache.get('providers') if isinstance(cache.get('providers'), dict) else {}
    result = {'schema': 'route-manager.providers.v2.local', 'generated_at': now.isoformat(),
              'accounts': {}, 'launchers': {}, 'saved': [], 'source_paths': {},
              'sources': {'paseo': paseo_error or 'read'}}
    # Conventional alternate homes (.claude-*, .codex-*) are also shown when they have no launcher.
    slots = {'claude': ('claude', home / '.claude'), 'codex': ('codex', home / '.codex')}
    for family in ('claude', 'codex'):
        for other in sorted(home.glob(f'.{family}-*')):
            if not other.is_dir():
                continue
            slot_name = other.name.lstrip('.')
            if slot_name.startswith(('codex-chatgpt-web', 'codex-pro-consult')):
                continue
            slots[slot_name] = (family, other)
    for name in definitions:
        family, env, _ = resolve_definition(name, definitions)
        if family in ('claude', 'codex'):
            root_key = 'CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'
            candidate = home / f'.{name}'
            fallback_home = candidate if candidate.is_dir() else home / ('.' + family)
            slots[name] = (family, Path(env.get(root_key) or environ.get(root_key) or fallback_home))
    # Sources retain private binding material only until this function returns.
    bindings = {}
    for name, (family, default_home) in slots.items():
        _, env, enabled = resolve_definition(name, definitions)
        root_key = 'CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'
        account_home = Path(env.get(root_key) or (environ.get(root_key) if name == family else '') or default_home)
        credential_home = Path(env.get('CLAUDE_SECURESTORAGE_CONFIG_DIR') or account_home) if family == 'claude' else account_home
        auth, error = read_json(credential_home / ('.credentials.json' if family == 'claude' else 'auth.json'))
        profile_path = home / '.claude.json' if credential_home == home / '.claude' else credential_home / '.claude.json'
        profile, _ = read_json(profile_path) if family == 'claude' else ({}, None)
        facts = auth_facts(family, auth, credential_home, profile.get('oauthAccount'))
        settings, _ = read_json(account_home / 'settings.json') if family == 'claude' else ({}, None)
        config = settings
        if family == 'claude':
            config = {**settings, 'env': {**obj(settings.get('env')), **env}}
            oauth_token = config['env'].get('CLAUDE_CODE_OAUTH_TOKEN')
            if isinstance(oauth_token, str) and oauth_token:
                facts.update(present=True, identity=digest('claude', oauth_token),
                             credential_identity=digest('claude', oauth_token), credential_version=digest(oauth_token, ''),
                             credential_key=digest(credential_home.resolve(), oauth_token), plan=None)
        else:
            try:
                config = {'auth': auth, 'config': (account_home / 'config.toml').read_text(encoding='utf-8-sig')}
            except OSError:
                config = {'auth': auth}
        base, token, models = product_config(family, config)
        token = token if isinstance(token, str) else ''
        is_api = bool(base and token)
        bound = name == family or name in definitions
        row = {'family': family, 'provider': name, 'home': str(account_home),
               'sources': ['native'] + (['paseo'] if name in definitions else []),
               'status': 'ready' if bound and enabled and (facts['present'] or is_api) else
                         'disabled' if not enabled else 'saved' if not bound else 'missing_credentials',
               'plan': facts['plan'], 'credential_key': facts['credential_key'],
               'identity': facts['identity'], 'billing': ('metered' if base in ('https://api.anthropic.com', 'https://api.openai.com')
                                                         else 'unknown') if is_api else 'subscription'}
        bindings[name] = {'identity': facts['identity'], 'base': base, 'key': digest(token) if token else None}
        bindings[name]['credential_identity'] = facts.get('credential_identity')
        bindings[name]['credential_version'] = facts.get('credential_version')
        if is_api:
            row.update(plan=None, credential_key=digest('api', account_home.resolve(), base, token),
                       identity=digest(base, token))
            bindings[name].update(identity=row['identity'], credential_identity=None, credential_version=None)
            if models:
                row['models'] = models
        source, _ = read_json(account_home / 'usage-float.json') if family == 'claude' else ({}, None)
        # This is the collector's local cache binding, not a gateway request.
        collector_base = str(config.get('env', {}).get('ANTHROPIC_BASE_URL') or '').rstrip('/') if family == 'claude' else ''
        magpie_candidates = set()
        if is_api and family == 'claude':
            if source.get('source') == 'magpie':
                acct = source.get('account') if isinstance(source.get('account'), str) else ''
                row['credential_key'] = digest('magpie', collector_base, token, source.get('provider') or family, acct)
                magpie_candidates.add(row['credential_key'])
            elif not source.get('source') and collector_base.startswith('https://') and collector_base != 'https://api.anthropic.com':
                magpie_candidates.add(digest('magpie', collector_base, token, family, ''))
        cached = obj(cache_rows.get(name))
        if cached.get('credential_key') in magpie_candidates:
            row['credential_key'] = cached['credential_key']
        is_magpie = (source.get('source') == 'magpie') or (cached.get('credential_key') in magpie_candidates) or (cached.get('backend_kind') == 'gateway')
        if (cached.get('credential_key') and cached.get('credential_key') == row.get('credential_key')):
            row['usage_binding'] = 'matches'
            rejected = {'claude': '登录凭据需由 Claude Code 更新', 'codex': '登录凭据需由 Codex 更新'}
            if cached.get('error') == rejected[family] and row['status'] == 'ready':
                row.update(status='paused', reason='credentials_rejected_by_collector')
            if not row.get('plan') and isinstance(cached.get('plan_label'), str):
                row['plan'] = configured_plan(family, cached['plan_label'])
                if is_api and is_magpie:
                    row['billing'] = 'subscription'
        elif cached:
            row['usage_binding'] = 'changed'
        result['accounts'][name] = row
        result['launchers'][name] = {'family': family, 'status': row['status']}
    # Existing independent harnesses keep their established account association.
    for name in set(definitions) | {'antigravity-resilient', 'opencode', 'pi'}:
        if name in result['launchers']:
            continue
        family, _, enabled = resolve_definition(name, definitions)
        result['launchers'][name] = {'family': family, 'status': 'ready' if enabled else 'disabled'}
    agy = obj(cache_rows.get('antigravity'))
    agy_present = bool(shutil.which('agy', path=environ.get('PATH')))
    result['accounts']['antigravity'] = {'family': 'antigravity', 'provider': 'antigravity-resilient',
        'sources': ['native'], 'status': 'ready' if agy_present else 'missing_client',
        'plan': agy.get('plan_label'), 'billing': 'subscription'}
    opencode = result['launchers']['opencode']
    installed = bool(shutil.which('opencode', path=environ.get('PATH')))
    if opencode['status'] == 'ready' and not installed:
        opencode['status'] = 'missing_client'
    result['accounts']['opencode'] = {'family': 'opencode', 'provider': 'opencode',
        'sources': ['native'], 'status': opencode['status'], 'plan': 'free', 'billing': 'subscription'}
    # Grok's ACP launcher is not an OAuth Claude/Codex slot. A cached reading is
    # metadata only; it cannot by itself prove that a client is installed.
    grok_cached = obj(cache_rows.get('grok'))
    grok_native = bool(shutil.which('grok', path=environ.get('PATH'))) or (home / '.grok').is_dir()
    grok_defined = 'grok' in definitions
    _, _, grok_enabled = resolve_definition('grok', definitions)
    grok_sources = (['native'] if grok_native else []) + (['paseo'] if grok_defined else [])
    if grok_cached:
        grok_sources.append('usage-cache')
    grok_status = 'disabled' if not grok_enabled else 'ready' if grok_defined or grok_native else 'missing_client'
    result['accounts']['grok'] = {'family': 'grok', 'provider': 'grok',
        'sources': grok_sources, 'status': grok_status,
        'plan': grok_cached.get('plan_label') or 'subscription', 'billing': 'subscription'}
    # Preserve window IDs to share the collector's quota accounting when present.
    grok_windows = [w for w in array(grok_cached.get('windows')) if isinstance(w, dict) and w.get('id')]
    if grok_windows:
        result['accounts']['grok']['usage_windows'] = [w['id'] for w in grok_windows]
    result['launchers']['grok'] = {'family': 'grok', 'status': grok_status}
    # Oh My Pi is Paseo's built-in `omp` provider. Its models come from the user's own
    # API keys (~/.omp/agent/models.yml), so it is metered: explicit selection only.
    omp_native = (bool(shutil.which('omp', path=environ.get('PATH')))
                  or (home / '.bun' / 'bin' / ('omp.exe' if os.name == 'nt' else 'omp')).is_file())
    omp_defined = 'omp' in definitions
    _, _, omp_enabled = resolve_definition('omp', definitions)
    omp_status = ('disabled' if not omp_enabled or not omp_defined
                  else 'ready' if omp_native or obj(definitions.get('omp')).get('command') else 'missing_client')
    result['accounts']['omp'] = {'family': 'omp', 'provider': 'omp',
        'sources': (['native'] if omp_native else []) + (['paseo'] if omp_defined else []),
        'status': omp_status, 'plan': 'API (Oh My Pi)', 'billing': 'metered'}
    result['launchers']['omp'] = {'family': 'omp', 'status': omp_status}
    # Other Paseo built-in launchers are recognized from their Paseo entry alone. Their
    # billing is unknown and no model is invented, so they produce no bundle until the
    # user configures models; unknown billing then stays explicit-selection only.
    for name in PASEO_RECOGNIZED_ONLY:
        if name in definitions and name not in result['accounts']:
            _, _, enabled = resolve_definition(name, definitions)
            models = [m if isinstance(m, str) else m['id'] for m in array(obj(definitions[name]).get('models'))
                      if isinstance(m, str) or isinstance(m, dict) and isinstance(m.get('id'), str)]
            result['accounts'][name] = {'family': name, 'provider': name, 'sources': ['paseo'],
                'status': 'ready' if enabled else 'disabled', 'billing': 'metered' if name == 'dsh' else 'unknown',
                **({'plan': 'API (DeepSeek Harness)'} if name == 'dsh' else {}),
                **({'models': models} if models else {})}
            result['launchers'][name] = {'family': name, 'status': 'ready' if enabled else 'disabled'}
    # Other explicitly configured launchers can supply their own advertised IDs.
    # Saved product profiles alone do not imply a launchable client.
    for name, definition in definitions.items():
        if name in result['accounts']:
            continue
        models = [m for m in array(obj(definition).get('models')) if isinstance(m, str)]
        if models:
            launcher = result['launchers'][name]
            result['accounts'][name] = {'family': launcher['family'], 'provider': name,
                'sources': ['paseo'], 'status': launcher['status'], 'models': models,
                'billing': 'unknown'}
    pi_home = Path(environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent')
    pi_auth, _ = read_json(pi_home / 'auth.json')
    pi_codex = obj(pi_auth.get('openai-codex'))
    pi_installed = bool(shutil.which('pi', path=environ.get('PATH')))
    pi_launcher = result['launchers']['pi']
    if pi_launcher['status'] == 'ready' and not pi_installed and not pi_codex.get('access'):
        pi_launcher['status'] = 'missing_client'
    if pi_codex.get('access'):
        pi_facts = auth_facts('codex', {'tokens': {'access_token': pi_codex.get('access'),
                                                   'account_id': pi_codex.get('accountId')}}, pi_home)
        matched_codex = next(
            (cname for cname, crow in result['accounts'].items()
             if crow.get('family') == 'codex' and crow.get('identity') and crow.get('identity') == pi_facts.get('identity')),
            None,
        )
        pi_cached = obj(cache_rows.get('pi'))
        pi_plan = pi_facts['plan'] or configured_plan('codex', pi_cached.get('plan_label')) or 'pro'
        result['accounts']['pi'] = {
            'family': 'pi',
            'provider': 'pi',
            'home': str(pi_home),
            'sources': ['native'] + (['paseo'] if 'pi' in definitions else []),
            'status': 'ready' if pi_launcher['status'] == 'ready' else pi_launcher['status'],
            'plan': pi_plan,
            'identity': pi_facts['identity'],
            'billing': 'subscription',
            **({'quota_account': matched_codex} if matched_codex else {}),
        }
    override_env = environ.get('ROUTE_MANAGER_ACCOUNTS_OVERRIDE') or environ.get('ROUTER_ACCOUNTS_OVERRIDE')
    override_path = Path(override_env).expanduser() if override_env else state_dir / 'accounts-override.json'
    override_doc, _ = read_json(override_path)
    for section in ('litellm_proxies', 'accounts'):
        for pname, pspec in obj(override_doc.get(section)).items():
            if pname in result['accounts'] or not isinstance(pspec, dict) or pspec.get('enabled') is False:
                continue
            sub = obj(pspec.get('litellm')) or pspec
            bkind = str(pspec.get('backend_kind') or sub.get('backend_kind') or pspec.get('probe') or '').lower()
            ep = str(sub.get('endpoint') or sub.get('base_url') or pspec.get('endpoint') or pspec.get('base_url') or '').strip()
            key_env = str(sub.get('api_key_env') or pspec.get('api_key_env') or '').strip()
            key = str(sub.get('api_key') or pspec.get('api_key') or (environ.get(key_env) if key_env else '') or '').strip()
            if bkind in ('litellm', 'proxy', 'api-proxy') or (ep and key):
                pcached = obj(cache_rows.get(pname))
                budget_usd = None
                plabel = str(pcached.get('plan_label') or pspec.get('plan') or '').strip()
                if plabel.startswith('$'):
                    try:
                        budget_usd = float(plabel[1:].split()[0])
                    except ValueError:
                        pass
                if budget_usd is None:
                    raw_cap = obj(pspec.get('capacity_usd')).get('spend') or pspec.get('budget_usd') or sub.get('budget_usd')
                    if isinstance(raw_cap, (int, float)) and raw_cap > 0:
                        budget_usd = float(raw_cap)
                pfamily = str(pspec.get('family') or pspec.get('provider') or 'codex')
                pmodels = [m for m in array(pspec.get('models') or sub.get('models')) if isinstance(m, str)]
                pstatus = 'ready' if (ep and key) else 'missing_credentials'
                result['accounts'][pname] = {
                    'family': pfamily,
                    'provider': pname,
                    'sources': ['router-override'],
                    'status': pstatus,
                    'plan': plabel or pspec.get('plan') or 'LiteLLM Proxy',
                    'billing': 'metered',
                    'backend_kind': 'litellm',
                    **({'budget_usd': budget_usd} if budget_usd is not None else {}),
                    **({'models': pmodels} if pmodels else {}),
                    **({'allowed_cwd': pspec['allowed_cwd']} if pspec.get('allowed_cwd') else {}),
                    **({'cwd_regex': pspec['cwd_regex']} if pspec.get('cwd_regex') else {}),
                }
                result['launchers'][pname] = {'family': pfamily, 'status': pstatus}
    # Configuration validity only; whether the local service answers is checked at dispatch.
    web_home = consult_home(home, environ)
    result['source_paths']['chatgpt-web'] = str(web_home)
    for name, status in (('dot', dot_configuration(home, environ)),
                         ('chatgpt-web', consult_configuration(web_home))):
        result['accounts'][name] = {'family': name, 'provider': name, 'sources': ['native'], 'status': status}
    # Remote external runner. No model benchmarks, inferred quota or credential reads.
    bot_config, bot_error = read_json(state_dir / 'grok-bot/config.json')
    bot_configured = bot_error is None and isinstance(bot_config.get('bot_id'), str) and bool(bot_config['bot_id'].strip())
    bot_status = 'missing_configuration' if not bot_configured else \
        'ready' if GROK_BOT_CLI.is_file() and shutil.which('node', path=environ.get('PATH')) else 'missing_client'
    result['accounts']['grok-bot'] = {
        'family': 'grok-bot', 'provider': 'grok-bot', 'sources': ['native'], 'status': bot_status,
        'billing': 'unknown', 'execution': 'remote', 'dispatch': 'automatic',
        'capabilities': ['text_prompt', 'thread_read'], 'auth_check': 'upstream_at_dispatch',
    }
    result['launchers']['grok-bot'] = {'family': 'grok-bot', 'status': bot_status}
    cc_path = product_root('cc-switch', home, environ) / 'cc-switch.db'
    result['source_paths']['cc-switch'] = str(cc_path)
    rows, cc_error = cc_switch(cc_path)
    result['sources']['cc-switch'] = cc_error or 'read'
    for entry, config in rows:
        family = 'grok' if entry['family'] == 'grokbuild' else entry['family']
        if family == 'grok' and entry['current']:
            if 'cc-switch' not in result['accounts']['grok']['sources']:
                result['accounts']['grok']['sources'].append('cc-switch')
            if grok_enabled:
                result['accounts']['grok']['status'] = 'ready'
                if entry.get('healthy') is False:
                    result['accounts']['grok'].update(status='paused', reason='unhealthy_in_cc_switch')
        base, token, models = product_config(family, config)
        facts = auth_facts(family, config.get('auth') or {}, home) if family in ('claude', 'codex') else {}
        matched = [name for name, binding in bindings.items() if result['accounts'][name]['family'] == family and
                   ((facts.get('identity') and facts['identity'] == binding['identity']) or
                    (base and token and base == binding['base'] and digest(token) == binding['key']))]
        # Claude Official normally has no saved OAuth: it uses the active native sign-in.
        if not matched and entry['current'] and entry['category'] == 'official' and family in ('claude', 'grok') and (family == 'grok' or not base):
            if result['accounts'].get(family, {}).get('billing') == 'subscription':
                matched = [family]
        for name in matched:
            if 'cc-switch' not in result['accounts'][name]['sources']:
                result['accounts'][name]['sources'].append('cc-switch')
            result['accounts'][name]['display_name'] = entry['name']
            if models and result['accounts'][name].get('billing') != 'subscription':
                result['accounts'][name]['models'] = models
            if entry.get('healthy') is False:
                result['accounts'][name].update(status='paused', reason='unhealthy_in_cc_switch')
        result['saved'].append({**entry, 'bound_to': matched, 'models': models,
                                'plan': facts.get('plan'), 'status': 'paused' if entry.get('healthy') is False else 'bound' if matched else 'saved'})
    read_magpie(home, environ, result, bindings)
    previous, _ = read_json(state_dir / 'provider-inventory.json')
    apply_removals(result, previous)
    # A plugin the user switched off in the AgentCompass panel takes its account out of routing at once.
    plugin_state, _ = read_json(state_dir / 'plugins.json')
    plugin_rows = obj(plugin_state.get('plugins'))
    for plugin_id, account in (('dot2api', 'dot'), ('chatgpt-web', 'chatgpt-web')):
        if obj(plugin_rows.get(plugin_id)).get('enabled') is False and account in result['accounts']:
            result['accounts'][account].update(status='paused', reason='plugin_disabled_in_agentcompass')
    for name, row in result['accounts'].items():
        if name in result['launchers']:
            result['launchers'][name]['status'] = row['status']
    # One subscription can be exposed through multiple launchers/products. Keep
    # its quota owner stable instead of giving each alias a full allowance.
    owners = {}
    ordered = sorted(result['accounts'], key=lambda name: (result['accounts'][name]['status'] != 'ready',
                                                          name != result['accounts'][name]['family'], name))
    for name in ordered:
        row = result['accounts'][name]
        ident = row.get('identity')
        if ident and not row.get('quota_account'):
            row['quota_account'] = owners.setdefault((row['family'], ident), name)
    import quota_exhaustion
    for name, row in result['accounts'].items():
        if quota_exhaustion.blocked(name, cache_rows.get(name), path=state_dir / 'quota-exhaustion.json'):
            row.update(status='quota_exhausted', reason='confirmed quota exhausted; refresh quota after recovery')
    return result


def current():
    # Reread local product files each time; do not contact any gateway.
    result = discover()
    home = Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    target = Path(os.environ.get('ROUTER_STATE_DIR') or home / '.agent-compass') / 'provider-inventory.json'
    try:
        write_json(target, result)
    except OSError:
        pass
    return result


def launcher_family(provider):
    if provider in ('claude', 'codex', 'pi', 'omp', 'opencode', 'antigravity-resilient'):
        return provider
    row = current().get('launchers', {}).get(provider) or {}
    return row.get('family') if row.get('status') == 'ready' else None


def account_family(account, accounts=None, inventory=None):
    """Resolve account type from discovery or its explicit bundle specification, never its name."""
    if inventory is None:
        inventory = (accounts or {}).get('_discovery')
    if inventory is None:
        inventory = current()
    row = obj(obj(inventory.get('accounts')).get(account))
    spec = obj(obj((accounts or {}).get('accounts')).get(account))
    if row.get('family') or spec.get('family'):
        return row.get('family') or spec['family']
    families = {b.get('harness') or b.get('provider') for b in obj(spec.get('bundles')).values()
                if isinstance(b, dict)} - {None}
    return next(iter(families)) if len(families) == 1 else None


def configured_plan(family, label):
    label = str(label or '').lower()
    if family == 'claude':
        if '20x' in label or label == 'max20':
            return 'max20'
        if '5x' in label or label == 'max5':
            return 'max5'
    if family == 'codex' and (label == 'pro100' or re.search(r'\b100\b', label)):
        return 'pro100'
    if family == 'antigravity' and 'ultra' in label:
        return 'ultra5' if '5x' in label or label.endswith('ultra5') else 'ultra20' if '20x' in label or label.endswith('ultra20') else 'ultra'
    for name in ('pro', 'plus', 'free', 'team', 'enterprise'):
        if name in label:
            return name
    return label


def unavailable_reason(inventory, account, provider=None):
    row = obj(obj(inventory.get('accounts')).get(account))
    launcher = obj(obj(inventory.get('launchers')).get(provider or account))
    reason = (row.get('reason') or row.get('status')) if row.get('status') not in (None, 'ready') else launcher.get('status')
    if reason and reason != 'ready':
        return f'provider discovery: {reason}; restore the account in its local product or sign in again, then reroute'
    return None


def _auto_catalog(inventory, live, now_epoch):
    """Advertised models per provider: configured/discovered lists, plus the tray model-catalog cache when live."""
    providers = {}
    for name, row in (inventory.get('accounts') or {}).items():
        models = [m for m in (row.get('models') or []) if isinstance(m, str)]
        if models and row.get('status', 'ready') == 'ready':
            providers[name] = {'ok': True, 'type': 'configured', 'checked_at': now_epoch, 'models': models}
    if live:
        home = Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
        cache, _ = read_json(Path(os.environ.get('ROUTER_STATE_DIR') or home / '.agent-compass') / 'model-catalog.json')
        for name, entry in obj(cache.get('providers')).items():
            if not isinstance(entry, dict) or entry.get('type') == 'static':
                continue
            if name in providers:
                ids = {(m.get('id') if isinstance(m, dict) else m) for m in entry.get('models') or []}
                extra = [m for m in providers[name]['models'] if m not in ids]
                entry = {**entry, 'models': list(entry.get('models') or []) + extra}
            providers[name] = entry
    return {'providers': providers}


def _apply_auto_bundles(accounts, inventory, live):
    """Runtime-derived bundles (auto_bundles) replace the retired per-proxy `__high` synthesis.

    Read-only: the `seen` file is read when live; the tray model refresh is the writer.
    """
    import time
    import auto_bundles
    now_epoch = time.time()
    families = {name: row.get('family') for name, row in (inventory.get('accounts') or {}).items()}
    home = Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    state = Path(os.environ.get('ROUTER_STATE_DIR') or home / '.agent-compass')
    seen = auto_bundles.read_seen(state / 'auto-bundles-seen.json') if live else {}
    derived = auto_bundles.derive(accounts, _auto_catalog(inventory, live, now_epoch), now_epoch,
                                  seen=seen, families=families)
    for account, rows in derived['bundles'].items():
        for bundle in rows.values():
            reason = unavailable_reason(inventory, account, bundle.get('provider', account))
            if reason:
                bundle['discovery_unavailable'] = reason
            if bundle.get('effort'):
                accounts.setdefault('dispatch_efforts', {}).setdefault(account, {}).setdefault(bundle['model'], [bundle['effort']])
    for name, spec in derived['models'].items():
        accounts.setdefault('models', {}).setdefault(name, spec)
    for account, rows in derived['bundles'].items():
        target = accounts['accounts'][account].setdefault('bundles', {})
        for bid, bundle in rows.items():
            target.setdefault(bid, bundle)
    accounts['_auto_bundles'] = {'excluded': derived['excluded'],
                                 'legacy_aliases': auto_bundles.legacy_aliases(accounts, derived),
                                 'seen': derived['seen'] if live else {}}


UNVERIFIABLE_SIGN_IN = ('antigravity', 'opencode', 'grok')
UNVERIFIED_SIGN_IN_LABEL = 'installed, sign-in unverified'


def sign_in_proven(row):
    """True when discovery can tell this account is signed in, not merely that its CLI is installed."""
    if not isinstance(row, dict) or row.get('family') not in UNVERIFIABLE_SIGN_IN:
        return True
    sources = row.get('sources') or []
    if isinstance(sources, str):
        sources = [sources]
    return 'usage-cache' in sources or bool(row.get('identity') or row.get('credential_key'))


def launch_verified(row, previous):
    """Decide whether a discovered account may be a confident routing pick.

    Antigravity, OpenCode and Grok report status ready when the CLI is installed, which is not a
    sign-in. The shipped template leaves them unverified. A machine fleet that already marked the
    slot verified, or a usage-cache reading, keeps the previous launchable state. The status string
    itself stays `ready` so provider listings do not change.
    """
    if not isinstance(row, dict):
        return False
    ready = row.get('status') == 'ready'
    if row.get('family') not in UNVERIFIABLE_SIGN_IN or sign_in_proven(row):
        return ready
    return bool(previous is True and ready)


def account_state_label(row):
    """Same words setup uses when a CLI is installed but sign-in cannot be read."""
    status = row.get('status') if isinstance(row, dict) else None
    if isinstance(row, dict) and row.get('family') in UNVERIFIABLE_SIGN_IN and status == 'ready':
        return UNVERIFIED_SIGN_IN_LABEL
    return status


def apply(accounts, inventory=None):
    live = inventory is None
    inventory = current() if inventory is None else inventory
    accounts['_discovery'] = inventory
    specs = accounts['accounts']
    templates = copy.deepcopy(specs)
    if inventory.get('accounts'):
        for name in list(specs):
            family = account_family(name, accounts, inventory)
            if family in ('claude', 'codex') and name != family:
                row = inventory['accounts'].get(name)
                if not row or row.get('status') == 'missing_credentials':
                    specs.pop(name, None)
    known_models = accounts.get('models') if isinstance(accounts.get('models'), dict) else {}
    aliases = accounts.get('aliases') if isinstance(accounts.get('aliases'), dict) else {}

    for name, row in inventory['accounts'].items():
        family = row['family']
        is_litellm = row.get('backend_kind') == 'litellm'
        if name not in specs:
            if row['status'] != 'ready':
                continue
            if is_litellm:
                spec = {
                    'plan': row.get('plan') or 'LiteLLM Proxy',
                    'billing': 'metered',
                    'verified': True,
                    'expires_at': None,
                    'windows': {'spend': {'hours': 168, 'launch_min_remaining_pct': 5}},
                    'burn_pct_per_worker_hour': {'spend': 1.0},
                    'bundles': {},  # auto_bundles derives one bundle per advertised model below
                }
                if isinstance(row.get('budget_usd'), (int, float)) and row['budget_usd'] > 0:
                    spec['capacity_usd'] = {'spend': float(row['budget_usd'])}
                specs[name] = spec
            elif family == 'pi':
                if row.get('quota_account') and row.get('quota_account') != name:
                    continue
                base_tpl = copy.deepcopy(templates.get('codex', {}))
                base_tpl['plan'] = row.get('plan') or 'ChatGPT Pro (Pi)'
                base_tpl['billing'] = 'subscription'
                base_tpl['bundles'] = {
                    f'{name}__{bid}': {**{k: v for k, v in b.items() if k != 'modeId'}, 'provider': 'pi', 'harness': 'pi'}
                    for bid, b in base_tpl.get('bundles', {}).items()
                    if b.get('provider') == 'pi'
                }
                specs[name] = base_tpl
            elif family == 'grok':
                specs[name] = copy.deepcopy(templates.get('grok') or {
                    'plan': 'subscription', 'billing': 'subscription', 'expires_at': None,
                    'unmetered': True, 'windows': {},
                    'bundles': {'grok47_high': {'provider': name, 'harness': 'grok', 'model': 'grok-4.7', 'effort': 'high'}},
                })
            elif family == 'grok-bot':
                specs[name] = {'plan': 'unknown', 'billing': 'unknown',
                    'windows': {}, 'bundles': {
                    'grok_bot': {'provider': name, 'harness': 'grok-bot', 'model': 'grok-bot'},
                }}
            elif family in ('claude', 'codex') and family in templates:
                spec = copy.deepcopy(templates[family])
                if row.get('billing') == 'unknown':
                    spec['billing'] = 'metered'
                spec['bundles'] = {f'{name}__{bid}': {**b, 'provider': name, 'harness': family} for bid, b in spec['bundles'].items()
                                   if b.get('provider', family) == family}
                for bundle in spec['bundles'].values():
                    bundle.setdefault('modeId', 'bypassPermissions' if family == 'claude' else 'full-access')
                efforts = accounts.get('dispatch_efforts')
                if isinstance(efforts, dict) and family in efforts and name not in efforts:
                    efforts[name] = copy.deepcopy(efforts[family])
                specs[name] = spec
            elif row.get('models'):
                specs[name] = {'plan': row.get('plan') or 'discovered',
                    'billing': row.get('billing') or 'unknown', 'windows': {},
                    'bundles': {}}  # auto_bundles derives these below
            else:
                continue
        spec = specs[name]
        spec['verified'] = launch_verified(row, spec.get('verified'))
        spec['discovery_status'] = row['status']
        if family == 'grok':
            window_ids = row.get('usage_windows') or []
            spec['unmetered'] = not bool(window_ids)
            if window_ids:
                spec.setdefault('windows', {}).update({wid: {'hours': 168, 'launch_min_remaining_pct': 5}
                                                       for wid in window_ids if wid not in spec.get('windows', {})})
                for wid in window_ids:
                    spec.setdefault('burn_pct_per_worker_hour', {}).setdefault(wid, 1.0)
        if row.get('allowed_cwd') and 'allowed_cwd' not in spec:
            spec['allowed_cwd'] = row['allowed_cwd']
        if row.get('cwd_regex') and 'cwd_regex' not in spec:
            spec['cwd_regex'] = row['cwd_regex']
        if is_litellm or spec.get('backend_kind') in ('litellm', 'proxy'):
            spec['billing'] = 'metered'
            spec.setdefault('windows', {'spend': {'hours': 168, 'launch_min_remaining_pct': 5}})
            spec.setdefault('burn_pct_per_worker_hour', {'spend': 1.0})
            if isinstance(row.get('budget_usd'), (int, float)) and row['budget_usd'] > 0:
                spec.setdefault('capacity_usd', {})['spend'] = float(row['budget_usd'])
            spec.setdefault('bundles', {})
            continue
        plan = row.get('plan')
        # A plan the user wrote in the account document seeds the template when discovery reports none
        # (claude/codex without a discovered plan stay unknown below).
        template_plan = configured_plan(family, plan or (spec.get('plan') if family not in ('claude', 'codex') else None))
        template = plan_defaults(accounts, family, template_plan) if template_plan else None
        if plan:
            previous_plan = configured_plan(family, spec.get('plan'))
            spec['plan'] = plan
            if previous_plan == template_plan:
                pass  # same named plan can have account-specific promotional capacity
            elif template:
                for key in TEMPLATE_KEYS:
                    if key in template:
                        spec[key] = copy.deepcopy(template[key])
                    else:
                        spec.pop(key, None)
            elif family in ('claude', 'codex'):
                for key in ('capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
                    spec.pop(key, None)
        if template and not (family in ('claude', 'codex') and not plan):
            fill_template_gaps(spec, template)
        if row.get('billing') and row['billing'] != 'unknown':
            spec['billing'] = row['billing']
        if family in ('claude', 'codex') and (not plan or row.get('billing') == 'metered'):
            spec['plan'] = 'API' if row.get('billing') == 'metered' else 'unknown'
            for key in ('capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
                spec.pop(key, None)
            if row.get('billing') == 'metered':
                spec['windows'] = {}
        advertised = row.get('models')
        for bundle in spec.get('bundles', {}).values():
            reason = unavailable_reason(inventory, name, bundle.get('provider', name))
            if reason:
                bundle['discovery_unavailable'] = reason
            if isinstance(advertised, list):
                match = bundle.get('launch_model') if bundle.get('launch_model') in advertised else next((m for m in advertised if (resolve_canonical_model(m, known_models, aliases) or m.rsplit('/', 1)[-1])
                              == (resolve_canonical_model(bundle['model'], known_models, aliases) or bundle['model'])), None)
                if match:
                    bundle['launch_model'] = match
                else:
                    bundle['discovery_unavailable'] = 'model not advertised by the configured provider'
    _apply_auto_bundles(accounts, inventory, live)
    for name, row in inventory['accounts'].items():
        owner = row.get('quota_account')
        if owner and owner != name and name in specs and owner in specs:
            for bid, bundle in specs[name].get('bundles', {}).items():
                specs[owner]['bundles'][bid] = {**bundle, 'provider': bundle.get('provider', name)}
            specs[name]['bundles'] = {}
            specs[name]['quota_account'] = owner
    apply_shared_quota(accounts)
    return accounts


def merge_usage(usage, accounts, now, names=None):
    """Invalidate another credential's cached quota; never probe an upstream."""
    inventory = accounts.get('_discovery') or {}
    result = copy.deepcopy(usage)
    for name, row in (inventory.get('accounts') or {}).items():
        if (names is None or name in names) and row.get('usage_binding') == 'changed':
            result.setdefault('accounts', {})[name] = {
                'status': 'unknown', 'reason': 'credential_changed', 'windows': []}
    # If a launcher is disabled but another exposes the same subscription, its
    # matching fresh reading still belongs to that subscription's quota owner.
    for name, row in (inventory.get('accounts') or {}).items():
        owner = row.get('quota_account')
        source = obj(obj(result.get('accounts')).get(name))
        target = obj(obj(result.get('accounts')).get(owner))
        if (owner and owner != name and (names is None or owner in names)
                and row.get('usage_binding') == 'matches' and source.get('status') == 'fresh'
                and target.get('status') != 'fresh'):
            result.setdefault('accounts', {})[owner] = copy.deepcopy(source)
    rows = result.setdefault('accounts', {})
    for name, source in quota_sources(accounts).items():
        if names is None or name in names:
            rows[name] = {**copy.deepcopy(rows.get(source) or {
                'status': 'unknown', 'reason': 'account_missing', 'windows': []}), 'quota_source': source}
    return result
