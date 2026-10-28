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
import shutil
import sqlite3
import tempfile
import tomllib
from model_resolution import resolve_canonical_model

MAX_BYTES = 5 * 1024 * 1024
GROK_BOT_CLI = Path(__file__).resolve().parents[1] / 'grok-bot/runtime/bin/gbot.mjs'


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


def discover(home=None, environ=None, now=None):
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path(environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    now = now or datetime.now(timezone.utc)
    state_dir = Path(environ.get('ROUTER_STATE_DIR') or home / '.route-manager')
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
                }
                result['launchers'][pname] = {'family': pfamily, 'status': pstatus}
    for name, config_path in (('dot', home / '.dot2api/client.json'),
                              ('chatgpt-web', home / '.codex-chatgpt-web-consult/config.json')):
        _, error = read_json(config_path)
        result['accounts'][name] = {'family': name, 'provider': name, 'sources': ['native'],
                                    'status': 'ready' if error is None else 'missing_configuration'}
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
    # A plugin the user switched off in the Route Manager panel takes its account out of routing at once.
    plugin_state, _ = read_json(state_dir / 'plugins.json')
    plugin_rows = obj(plugin_state.get('plugins'))
    for plugin_id, account in (('dot2api', 'dot'), ('chatgpt-web', 'chatgpt-web')):
        if obj(plugin_rows.get(plugin_id)).get('enabled') is False and account in result['accounts']:
            result['accounts'][account].update(status='paused', reason='plugin_disabled_in_route_manager')
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
    return result


def current():
    # Reread local product files each time; do not contact any gateway.
    result = discover()
    home = Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    target = Path(os.environ.get('ROUTER_STATE_DIR') or home / '.route-manager') / 'provider-inventory.json'
    try:
        write_json(target, result)
    except OSError:
        pass
    return result


def launcher_family(provider):
    if provider in ('claude', 'codex', 'pi', 'opencode', 'antigravity-resilient'):
        return provider
    row = current().get('launchers', {}).get(provider) or {}
    return row.get('family') if row.get('status') == 'ready' else None


def configured_plan(family, label):
    label = str(label or '').lower()
    if family == 'claude':
        if '20x' in label or label == 'max20':
            return 'max20'
        if '5x' in label or label == 'max5':
            return 'max5'
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


def apply(accounts, inventory=None):
    inventory = current() if inventory is None else inventory
    accounts['_discovery'] = inventory
    specs = accounts['accounts']
    templates = copy.deepcopy(specs)
    if inventory.get('accounts'):
        for name in list(specs):
            if name.startswith(('claude-', 'codex-')):
                row = inventory['accounts'].get(name)
                if not row or row.get('status') == 'missing_credentials':
                    specs.pop(name, None)
    known_models = accounts.get('models') if isinstance(accounts.get('models'), dict) else {}
    aliases = accounts.get('aliases') if isinstance(accounts.get('aliases'), dict) else {}

    def _synthesize_proxy_bundles(pname, pfamily, model_list):
        bundles = {}
        for raw_m in model_list or []:
            if not isinstance(raw_m, str) or not raw_m.strip():
                continue
            canon = resolve_canonical_model(raw_m, known_models, aliases) or raw_m.rsplit('/', 1)[-1]
            m_harness = pfamily if pfamily not in ('claude', 'codex') else 'claude' if canon.startswith('claude-') else 'codex'
            mode = 'bypassPermissions' if m_harness == 'claude' else ('build' if m_harness == 'opencode' else 'full-access')
            bid = f"{pname}__{canon.replace('-', '_').replace('.', '_')}_high"
            if bid in bundles and bundles[bid]['launch_model'] != raw_m:
                bid += '_' + digest(raw_m)[:8]
            bundles[bid] = {
                'provider': pname,
                'harness': m_harness,
                'model': canon,
                'launch_model': raw_m,
                'effort': 'high',
                **({'modeId': mode} if m_harness in ('claude', 'codex', 'pi', 'opencode') else {}),
            }
            accounts.setdefault('dispatch_efforts', {}).setdefault(pname, {}).setdefault(canon, ['high'])
        return bundles

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
                    'bundles': _synthesize_proxy_bundles(name, family, row.get('models') or ['gpt-6.1-sol']),
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
                    f'{name}__{bid}': {**b, 'provider': 'pi', 'harness': 'pi', 'modeId': 'full-access'}
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
                    'windows': {'weekly': {'hours': 168, 'launch_min_remaining_pct': 5},
                                'trial': {'launch_min_remaining_pct': 5}}, 'bundles': {
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
                    'bundles': _synthesize_proxy_bundles(name, family, row['models'])}
            else:
                continue
        spec = specs[name]
        spec['verified'] = row['status'] == 'ready'
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
        if is_litellm or spec.get('backend_kind') in ('litellm', 'proxy'):
            spec['billing'] = 'metered'
            spec.setdefault('windows', {'spend': {'hours': 168, 'launch_min_remaining_pct': 5}})
            spec.setdefault('burn_pct_per_worker_hour', {'spend': 1.0})
            if isinstance(row.get('budget_usd'), (int, float)) and row['budget_usd'] > 0:
                spec.setdefault('capacity_usd', {})['spend'] = float(row['budget_usd'])
            if not spec.get('bundles'):
                spec['bundles'] = _synthesize_proxy_bundles(
                    name, family, row.get('models') or spec.get('models') or ['gpt-6.1-sol']
                )
            continue
        plan = row.get('plan')
        if plan:
            previous_plan = configured_plan(family, spec.get('plan'))
            spec['plan'] = plan
            template = (templates.get('claude-2') if family == 'claude' and plan == 'pro' else
                        templates.get('claude') if family == 'claude' and plan in ('max5', 'max20') else
                        templates.get('codex') if family == 'codex' and plan == 'pro' else None)
            if previous_plan == plan:
                pass  # same named plan can have account-specific promotional capacity
            elif template:
                for key in ('windows', 'capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
                    if key in template:
                        spec[key] = copy.deepcopy(template[key])
                    else:
                        spec.pop(key, None)
                if family == 'claude' and plan == 'max5':
                    spec['capacity_usd'] = {'seven_day': 595, 'five_hour': 94, 'weekly_fable': 298}
                    spec['burn_pct_per_worker_hour'] = {'seven_day': 1.08, 'weekly_fable': 1.08}
                    spec.pop('capacity_change', None)
            elif family in ('claude', 'codex'):
                for key in ('capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
                    spec.pop(key, None)
            if family == 'claude' and plan == 'pro' and name != 'claude':
                has_max_peer = any(
                    other != name and r.get('family') == 'claude' and r.get('status') == 'ready'
                    and configured_plan('claude', r.get('plan')) in ('max5', 'max20')
                    for other, r in inventory['accounts'].items()
                )
                if has_max_peer:
                    for b in spec.get('bundles', {}).values():
                        if b.get('model') == 'claude-opus-5-5' and not b.get('disabled'):
                            b['disabled'] = f'{name} is a Claude Pro account ($156/week); Opus is reserved for the Max account'
        if row.get('billing') and row['billing'] != 'unknown':
            spec['billing'] = row['billing']
        if family in ('claude', 'codex') and (not plan or row.get('billing') == 'metered'):
            spec['plan'] = 'API' if row.get('billing') == 'metered' else 'unknown'
            for key in ('capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour'):
                spec.pop(key, None)
            if row.get('billing') == 'metered':
                spec['windows'] = {}
        advertised = row.get('models')
        if isinstance(advertised, list):
            existing_models = {resolve_canonical_model(b['model'], known_models, aliases) or b['model']
                               for b in spec.get('bundles', {}).values()}
            additional = [m for m in advertised if (resolve_canonical_model(m, known_models, aliases) or m.rsplit('/', 1)[-1]) not in existing_models]
            spec.setdefault('bundles', {}).update(_synthesize_proxy_bundles(name, family, additional))
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
    for name, row in inventory['accounts'].items():
        owner = row.get('quota_account')
        if owner and owner != name and name in specs and owner in specs:
            for bid, bundle in specs[name].get('bundles', {}).items():
                specs[owner]['bundles'][bid] = {**bundle, 'provider': bundle.get('provider', name)}
            specs[name]['bundles'] = {}
            specs[name]['quota_account'] = owner
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
    return result
