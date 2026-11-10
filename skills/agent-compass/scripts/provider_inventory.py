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
import account_state
import antigravity_plan
import grok_bot_paths
from model_resolution import resolve_canonical_model
import provider_retry
import public_data
import usage_cache

MAX_BYTES = 5 * 1024 * 1024
# The collector's words for a 401/403, in either language it writes.
COLLECTOR_REJECTED = {'claude': ('登录凭据需由 Claude Code 更新', 'Login credentials must be updated by Claude Code'),
                      'codex': ('登录凭据需由 Codex 更新', 'Login credentials must be updated by Codex'),
                      'grok': ('登录凭据需由 Grok 更新', 'Login credentials must be updated by Grok')}
GROK_BOT_CLI = grok_bot_paths.runtime_dir() / grok_bot_paths.ENTRY
# Paseo built-in providers recognized from their Paseo entry without a model catalog.
PASEO_RECOGNIZED_ONLY = ('copilot', 'muse', 'dsh')
# Oh My Pi signs in to subscriptions with OAuth (agent.db `auth_credentials`). omp's provider id -> the AgentCompass
# family whose identity (auth_facts), plan templates and model rows such a login uses. A provider without a family
# here is not routed: AgentCompass has no identity, plan or model data for it.
OMP_OAUTH_FAMILIES = {'openai-codex': 'codex', 'anthropic': 'claude'}


PLAN_TEMPLATES = Path(__file__).resolve().parents[1] / 'references/plan-templates.json'
TEMPLATE_KEYS = ('windows', 'capacity_usd', 'capacity_change', 'burn_pct_per_worker_hour', 'cadence', 'unmetered')
POOL_KEYS = ('windows', 'burn_pct_per_worker_hour', 'capacity_usd', 'capacity_change', 'observed', 'expires_at')
_SHIPPED_PLAN_TEMPLATES = None
_STDLIB_WHICH = shutil.which
_WHICH_CACHE = {}
_REGISTRY_EXE_CACHE = {}
_LAST_PUBLISHED_INVENTORY = {}
_RESOLVE_CACHE = {}
_TOML_CACHE = {}
_DEFAULT_HOME_RESOLVED = Path.home().resolve()
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolved(path: Path) -> Path:
    key = str(path)
    res = _RESOLVE_CACHE.get(key)
    if res is None:
        res = path.resolve()
        if len(_RESOLVE_CACHE) > 512:
            _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE[key] = res
    return res




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
    inventory = obj(accounts.get('_discovery'))
    inv_accounts = obj(inventory.get('accounts'))
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
        source_row = obj(inv_accounts.get(source_name))
        unavail = account_state.unavailable_text(source_row)
        if unavail:
            reason = f'provider discovery: {unavail}'
            bot['discovery_status'] = source_row['status']
            bot['verified'] = False
            for bundle in obj(bot.get('bundles')).values():
                if isinstance(bundle, dict):
                    bundle.setdefault('discovery_unavailable', reason)


def plan_defaults(accounts, family, plan):
    """Quota template for a subscription plan, independent of any configured account instance.

    The account document's `plan_templates` overrides the shipped references/plan-templates.json
    per family and plan. A template of `{"alias": "<plan>"}` resolves to that plan's template. Only
    `windows`, `capacity_usd`, `capacity_change` and `burn_pct_per_worker_hour` are copied onto an
    account; other keys (provenance, basis, price) are documentation.
    """
    global _SHIPPED_PLAN_TEMPLATES
    source = public_data.effective(PLAN_TEMPLATES)  # the fetched public copy when it is newer than the shipped one
    if _SHIPPED_PLAN_TEMPLATES is None or _SHIPPED_PLAN_TEMPLATES[0] != source or read_json is not _STDLIB_READ_JSON:
        shipped, _ = read_json(source)
        if read_json is _STDLIB_READ_JSON and shipped:
            _SHIPPED_PLAN_TEMPLATES = (source, shipped)
    else:
        shipped = _SHIPPED_PLAN_TEMPLATES[1]
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


_STDLIB_READ_JSON = read_json



def read_toml(value):
    if not isinstance(value, str):
        return {}
    cached = _TOML_CACHE.get(value)
    if cached is not None:
        return cached
    try:
        parsed = tomllib.loads(value)
    except ValueError:
        parsed = {}
    if len(_TOML_CACHE) > 256:
        _TOML_CACHE.clear()
    _TOML_CACHE[value] = parsed
    return parsed


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
                'credential_key': digest(_resolved(home), token) if token else None,
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
            'credential_identity': digest('codex', token, account or '') if token else None,
            'credential_key': digest(_resolved(home), str(token) + ':' + str(account or '')) if token else None,
            'plan': plan_name(family, claims.get('chatgpt_plan_type'))}


def _extract_grok_auth_token(auth):
    if not isinstance(auth, dict):
        return None
    top = auth.get('access_token')
    if isinstance(top, str) and top:
        return top
    oidc = None
    legacy = None
    for scope, value in auth.items():
        if not isinstance(value, dict):
            continue
        key = value.get('key')
        if not isinstance(key, str) or not key:
            continue
        s = str(scope)
        if s.startswith('https://auth.x.ai::'):
            oidc = key
        elif s == 'https://accounts.x.ai/sign-in' or '/sign-in' in s:
            legacy = key
    return oidc or legacy


def grok_auth_facts(grok_home, environ=None):
    auth_doc, _ = read_json(Path(grok_home) / 'auth.json')
    file_tok = _extract_grok_auth_token(auth_doc)
    if file_tok:
        token = file_tok
    else:
        environ = environ or {}
        token = str(environ.get('GROK_API_KEY') or environ.get('GROK_TOKEN') or '').strip() or None
    return {
        'present': bool(token),
        'identity': digest('grok', token) if token else None,
        'credential_identity': digest('grok', token) if token else None,
        'credential_key': digest(_resolved(Path(grok_home)), token) if token else None,
    }


def omp_credentials(agent_dir):
    """Oh My Pi's stored sign-ins from agent.db, read-only and bounded; omp itself is never run.

    Returns {'logins', 'api_keys', 'problem'}. `logins` are the enabled OAuth credentials of providers in
    OMP_OAUTH_FAMILIES, in omp's order, as {'provider', 'family', 'data'}. `api_keys` is True when an enabled
    credential of another type (an API key) is stored. `problem` is 'unreadable' for a corrupt, locked or foreign
    database and None otherwise (a missing database included).
    """
    found = {'logins': [], 'api_keys': False, 'problem': None}
    path = Path(agent_dir) / 'agent.db'
    if not path.is_file():
        return found
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.5)) as con:
            rows = con.execute("SELECT provider, credential_type, data FROM auth_credentials "
                               "WHERE disabled_cause IS NULL OR disabled_cause = '' ORDER BY id").fetchall()
    except sqlite3.Error:
        return dict(found, problem='unreadable')
    for provider, kind, raw in rows:
        if kind != 'oauth':
            found['api_keys'] = True
            continue
        family = OMP_OAUTH_FAMILIES.get(provider)
        try:
            data = json.loads(raw) if family and isinstance(raw, str) and len(raw) <= MAX_BYTES else None
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get('access'), str) and data['access']:
            found['logins'].append({'provider': provider, 'family': family, 'data': data})
    return found


def omp_models(agent_dir):
    """{omp provider id: [model ids]} from omp's own model cache (models.db), read-only; {} when unreadable."""
    path = Path(agent_dir) / 'models.db'
    if not path.is_file():
        return {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.5)) as con:
            rows = con.execute('SELECT provider_id, models FROM model_cache').fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for provider_id, raw in rows:
        try:
            models = json.loads(raw) if isinstance(raw, str) and len(raw) <= MAX_BYTES else []
        except ValueError:
            continue
        ids = [m.get('id') for m in array(models) if isinstance(m, dict) and isinstance(m.get('id'), str)]
        if ids:   # cache keys carry a version suffix: `openai-codex:0.159.0`
            out.setdefault(str(provider_id).split(':', 1)[0], []).extend(ids)
    return out


def omp_auth(family, data):
    """An omp OAuth record in the shape auth_facts reads for the family's own CLI, plus its profile."""
    if family == 'claude':
        return ({'claudeAiOauth': {'accessToken': data.get('access'), 'refreshToken': data.get('refresh'),
                                   'subscriptionType': data.get('subscriptionType') or data.get('plan')}},
                {'accountUuid': data.get('accountUuid') or data.get('accountId'), 'emailAddress': data.get('email'),
                 'organizationUuid': data.get('organizationUuid') or data.get('orgId')})
    return {'tokens': {'access_token': data.get('access'), 'account_id': data.get('accountId'),
                       'id_token': data.get('idToken') or data.get('id_token')}}, None


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


def _which(product, path_value):
    if shutil.which is not _STDLIB_WHICH:
        return shutil.which(product, path=path_value)
    import time
    now_mono = time.monotonic()
    key = (product, path_value)
    cached = _WHICH_CACHE.get(key)
    if cached is not None and now_mono - cached[0] < 15.0:
        return cached[1]
    found = shutil.which(product, path=path_value)
    if not found and path_value is not None and path_value == os.environ.get('PATH'):
        # A long-running process (the tray, a daemon) keeps the PATH it started with; a CLI installed since then is
        # on the PATH a new process would get, so look there too.
        fresh = _fresh_windows_path()
        if fresh:
            found = shutil.which(product, path=fresh)
    _WHICH_CACHE[key] = (now_mono, found)
    return found


def _fresh_windows_path():
    """The PATH a newly started process gets: machine then user `Path` from the registry. Empty off Windows.

    Also empty when this process runs as another profile than the signed-in user's (USERPROFILE pointed at another
    home, as a sandbox or a foreign-profile check does): that user's registry PATH names CLIs that profile does not have.
    """
    if os.name != 'nt':
        return ''
    import winreg
    owner = _registry_profile()
    here = os.environ.get('USERPROFILE')
    if not owner or not here or os.path.normcase(os.path.abspath(here)) != os.path.normcase(os.path.abspath(owner)):
        return ''
    parts = []
    for hive, subkey in ((winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                         (winreg.HKEY_CURRENT_USER, 'Environment')):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                parts.append(os.path.expandvars(str(winreg.QueryValueEx(key, 'Path')[0])))
        except OSError:
            continue
    return os.pathsep.join(parts)


def _registry_profile():
    """The signed-in user's profile folder (HKCU `Volatile Environment` USERPROFILE); '' when it cannot be read."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Volatile Environment') as key:
            return os.path.expandvars(str(winreg.QueryValueEx(key, 'USERPROFILE')[0]))
    except OSError:
        return ''


def _registry_executables(product):
    import time
    now_mono = time.monotonic()
    cached = _REGISTRY_EXE_CACHE.get(product)
    if cached is not None and now_mono - cached[0] < 60.0:
        return list(cached[1])
    import winreg
    paths = []
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
    _REGISTRY_EXE_CACHE[product] = (now_mono, tuple(paths))
    return paths


def installed_executables(product, home, environ):
    """Bounded OS installation hints; never execute the application or scan a drive."""
    paths = []
    found = _which(product, environ.get('PATH'))
    if found:
        paths.append(_resolved(Path(found)))
    explicit = environ.get('MAGPIE_EXE' if product == 'magpie' else 'CC_SWITCH_EXE')
    if explicit:
        paths.insert(0, Path(explicit).expanduser())
    is_default_home = _resolved(home) == _DEFAULT_HOME_RESOLVED
    if is_default_home:
        repository = _REPO_ROOT
        for directory in (repository, repository / product, repository / 'tools', repository / 'tools' / product):
            candidate = directory / (product + ('.exe' if os.name == 'nt' else ''))
            if candidate.is_file():
                paths.append(candidate)
    if os.name != 'nt' or not is_default_home:
        return paths
    paths.extend(_registry_executables(product))
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


def _expand_home(value, home):
    text = str(value or '').strip()
    if text == '~':
        return Path(home)
    if text.startswith(('~/', '~\\')):
        return Path(home) / text[2:]
    return Path(os.path.expandvars(text)).expanduser()


def consult_home(home, environ):
    """The ChatGPT Web bridge profile the consultation client uses: PRO_CONSULT_HOME, else the default folder."""
    value = str(environ.get('PRO_CONSULT_HOME') or '').strip()
    return _expand_home(value, home) if value else Path(home) / '.codex-chatgpt-web-consult'


def is_consult_home(path, home, environ=None):
    """True when `path` is an isolated ChatGPT Web / Pro consultation profile rather than a Codex worker home."""
    environ = os.environ if environ is None else environ
    p = Path(path)
    if p.name.lstrip('.').startswith(('codex-chatgpt-web', 'codex-pro-consult')):
        return True
    return _resolved(p) == _resolved(consult_home(home, environ))


def orca_user_data_dirs(home=None):
    """Orca's Electron userData directories under `home` (Windows roaming, macOS Application Support, Linux ~/.config).

    ORCA_USER_DATA_PATH is deliberately not read: an inherited value would point a test or sandbox home at the real install.
    """
    home = Path(home) if home is not None else Path.home()
    found = {}
    for p in (home / 'AppData' / 'Roaming' / 'orca',
              home / 'Library' / 'Application Support' / 'orca',
              home / '.config' / 'orca'):
        if p.is_dir():
            found.setdefault(str(_resolved(p)), p)
    return list(found.values())


def orca_codex_homes(home=None):
    """Codex homes Orca manages per signed-in account. Orca mirrors only config.toml into them, so AGENTS.md and skills never arrive."""
    return [h for ud in orca_user_data_dirs(home)
            for h in sorted((ud / 'codex-accounts').glob('*/home')) if h.is_dir()]


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
    cache = usage_cache.read(None, home, environ)[0] or {}
    cache_rows = cache.get('providers') if isinstance(cache.get('providers'), dict) else {}
    cache_polls = cache.get('poll') if isinstance(cache.get('poll'), dict) else {}
    cache_attempts = cache.get('attempts') if isinstance(cache.get('attempts'), dict) else {}
    result = {'schema': 'agent-compass.providers.v2.local', 'generated_at': now.isoformat(),
              'accounts': {}, 'launchers': {}, 'saved': [], 'source_paths': {},
              'sources': {'paseo': paseo_error or 'read'}}
    # Conventional alternate homes (.claude-*, .codex-*) are also shown when they have no launcher.
    slots = {'claude': ('claude', home / '.claude'), 'codex': ('codex', home / '.codex')}
    for family in ('claude', 'codex'):
        for other in sorted(home.glob(f'.{family}-*')):
            if not other.is_dir():
                continue
            if family == 'codex' and is_consult_home(other, home, environ):
                continue
            slot_name = other.name.lstrip('.')
            slots[slot_name] = (family, other)
    for name in definitions:
        family, env, _ = resolve_definition(name, definitions)
        if family in ('claude', 'codex'):
            root_key = 'CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'
            candidate = home / f'.{name}'
            fallback_home = candidate if candidate.is_dir() else home / ('.' + family)
            raw_root = env.get(root_key)  # the account's configuration only, never the caller's environment
            target_home = _expand_home(raw_root, home) if raw_root else fallback_home
            if family == 'codex' and is_consult_home(target_home, home, environ):
                continue
            slots[name] = (family, target_home)
    orca_slots = set()
    seen_codex_homes = {_resolved(h) for fam, h in slots.values() if fam == 'codex'}
    for orca_home in orca_codex_homes(home):
        if is_consult_home(orca_home, home, environ) or _resolved(orca_home) in seen_codex_homes:
            continue
        slot_name = f'orca-{orca_home.parent.name}'
        slots[slot_name] = ('codex', orca_home)
        orca_slots.add(slot_name)
        seen_codex_homes.add(_resolved(orca_home))
    # Sources retain private binding material only until this function returns.
    bindings = {}
    for name, (family, default_home) in slots.items():
        _, env, enabled = resolve_definition(name, definitions)
        root_key = 'CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'
        raw_root = env.get(root_key)
        account_home = _expand_home(raw_root, home) if raw_root else default_home
        raw_sec = env.get('CLAUDE_SECURESTORAGE_CONFIG_DIR') if family == 'claude' else ''
        credential_home = _expand_home(raw_sec, home) if raw_sec else account_home
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
                             credential_key=digest(_resolved(credential_home), oauth_token), plan=None)
        else:
            try:
                config = {'auth': auth, 'config': (account_home / 'config.toml').read_text(encoding='utf-8-sig')}
            except OSError:
                config = {'auth': auth}
        base, token, models = product_config(family, config)
        token = token if isinstance(token, str) else ''
        is_api = bool(base and token)
        is_orca = name in orca_slots
        bound = name == family or name in definitions or is_orca
        row = {'family': family, 'provider': name, 'home': str(account_home),
               'cli_installed': bool(_which(family, environ.get('PATH'))),
               'sources': ['native'] + (['paseo'] if name in definitions else []) + (['orca'] if is_orca else []),
               'status': 'ready' if bound and enabled and (facts['present'] or is_api) else
                         'disabled' if not enabled else 'saved' if not bound else 'missing_credentials',
               'plan': facts['plan'], 'plan_source': 'credential' if facts['plan'] else None,
               'credential_plan': configured_plan(family, facts['plan']) if facts['plan'] else None,
               'credential_key': facts['credential_key'],
               'identity': facts['identity'], 'billing': ('metered' if base in ('https://api.anthropic.com', 'https://api.openai.com')
                                                         else 'unknown') if is_api else 'subscription'}
        bindings[name] = {'identity': facts['identity'], 'base': base, 'key': digest(token) if token else None}
        bindings[name]['credential_identity'] = facts.get('credential_identity')
        bindings[name]['credential_version'] = facts.get('credential_version')
        if is_api:
            row.update(plan=None, plan_source=None, credential_plan=None,
                       credential_key=digest('api', _resolved(account_home), base, token),
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
        if is_api and is_magpie:
            row['billing'] = 'subscription'
        if (cached.get('credential_key') and cached.get('credential_key') == row.get('credential_key')):
            row['usage_binding'] = 'matches'
            if row['status'] == 'ready' and account_state.usage_refused_plan(cached):
                retry_at = account_state.usage_refused_retry_at(
                    cached, poll=cache_polls.get(name), attempt=cache_attempts.get(name), now=now
                )
                row.update(
                    status=account_state.PLAN_INACTIVE,
                    reason=account_state.usage_refused_reason(cached, retry_at=retry_at),
                    **({'plan_inactive_at': cached.get('error_at') or cached.get('fetched_at')}
                       if (cached.get('error_at') or cached.get('fetched_at')) else {}),
                    **({'plan_inactive_retry_at': retry_at} if retry_at else {}),
                )
            elif row['status'] == 'ready' and account_state.usage_refused_credentials(cached, COLLECTOR_REJECTED[family]):
                row.update(status=account_state.AUTH_FAILED, reason='credentials_rejected_by_collector')
            label = cached.get('plan_label') if isinstance(cached.get('plan_label'), str) else ''
            online = configured_plan(family, label) if label.strip() else None
            if online and family == 'codex' and not is_api:
                # This reading was taken with the current credential, so the usage API's plan_type is newer than the
                # id_token's claim, which only changes when the Codex CLI rewrites auth.json.
                claim = configured_plan(family, row.get('plan')) if row.get('plan') else None
                if claim and claim != online:
                    row['plan_conflict'] = {'online': online, 'credential': claim}
                row.update(plan=online, plan_source='usage_api', plan_read_at=cached.get('fetched_at'))
            elif not row.get('plan') and online:
                row.update(plan=online, plan_source='collector', plan_read_at=cached.get('fetched_at'))
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
    agy_present = bool(_which('agy', environ.get('PATH')))
    agy_cli = bool(agy_present or _which('gemini', environ.get('PATH')))
    # agy answers /usage only for a signed-in account, so a quota reading is the sign-in proof.
    agy_read = bool(array(agy.get('windows')))
    # /usage carries no tier; the tier comes from Google with agy's own sign-in (antigravity_plan), which is also a sign-in proof.
    agy_tier = antigravity_plan.cached_plan(state_dir, now)
    agy_tier_read_at = antigravity_plan.read_cache(state_dir).get('read_at') if agy_tier else None
    result['accounts']['antigravity'] = {'family': 'antigravity', 'provider': 'antigravity-resilient',
        'home': str(home / '.gemini'), 'cli_installed': agy_cli,
        'sources': ['native'] + (['usage-cache'] if agy_read else []) + (['account-tier'] if agy_tier else []),
        'status': 'ready' if agy_present else 'missing_client',
        'plan': agy.get('plan_label') or agy_tier,
        'plan_source': 'usage_api' if agy.get('plan_label') else 'account-tier' if agy_tier else None,
        **({'plan_read_at': agy.get('fetched_at') if agy.get('plan_label') else agy_tier_read_at}
           if (agy.get('plan_label') or agy_tier_read_at) else {}),
        'billing': 'subscription'}
    opencode = result['launchers']['opencode']
    installed = bool(_which('opencode', environ.get('PATH')))
    if opencode['status'] == 'ready' and not installed:
        opencode['status'] = 'missing_client'
    result['accounts']['opencode'] = {'family': 'opencode', 'provider': 'opencode',
        'home': str(home / '.config/opencode'), 'cli_installed': installed,
        'sources': ['native'], 'status': opencode['status'], 'plan': 'free', 'billing': 'subscription'}
    # Grok's ACP launcher is not an OAuth Claude/Codex slot. A cached reading is
    # metadata only; it cannot by itself prove that a client is installed.
    grok_cached = obj(cache_rows.get('grok'))
    grok_home = home / '.grok'
    grok_native = bool(_which('grok', environ.get('PATH'))) or grok_home.is_dir()
    grok_defined = 'grok' in definitions
    _, _, grok_enabled = resolve_definition('grok', definitions)
    grok_facts = grok_auth_facts(grok_home, environ)
    grok_windows = [w for w in array(grok_cached.get('windows')) if isinstance(w, dict) and w.get('id')]
    grok_has_failure = bool(
        grok_cached.get('error_status') is not None
        or grok_cached.get('error_code')
        or grok_cached.get('error_at')
        or (grok_cached.get('stale') is True and grok_cached.get('error'))
        or (grok_cached.get('available') is False and grok_cached.get('error'))
    )
    grok_ckey = grok_cached.get('credential_key')
    if grok_ckey:
        grok_matches = bool(grok_facts['credential_key'] and grok_ckey == grok_facts['credential_key'])
    elif grok_has_failure:
        grok_matches = bool(grok_facts['present'])
    else:
        grok_matches = bool(grok_cached)
    grok_sources = (['native'] if grok_native else []) + (['paseo'] if grok_defined else [])
    if grok_cached and grok_matches and (grok_windows or not grok_has_failure):
        grok_sources.append('usage-cache')
    grok_status = 'disabled' if not grok_enabled else 'ready' if grok_defined or grok_native else 'missing_client'
    grok_row = {
        'family': 'grok', 'provider': 'grok',
        'sources': grok_sources, 'status': grok_status,
        'plan': (grok_cached.get('plan_label') if grok_matches else None) or 'subscription',
        'billing': 'subscription',
        **({'credential_key': grok_facts['credential_key'], 'identity': grok_facts['identity']}
           if grok_facts['present'] else {}),
    }
    bindings['grok'] = {
        'identity': grok_facts['identity'],
        'credential_identity': grok_facts['credential_identity'],
        'base': '',
        'key': None,
    }
    # Preserve window IDs to share the collector's quota accounting when present.
    if grok_matches and grok_cached:
        grok_row['usage_binding'] = 'matches'
        if grok_windows:
            grok_row['usage_windows'] = [w['id'] for w in grok_windows]
        if grok_has_failure:
            grok_row['usage_read_failed'] = True
        if grok_row['status'] == 'ready' and account_state.usage_refused_plan(grok_cached):
            grok_retry_at = account_state.usage_refused_retry_at(
                grok_cached, poll=cache_polls.get('grok'), attempt=cache_attempts.get('grok'), now=now
            )
            grok_row.update(
                status=account_state.PLAN_INACTIVE,
                reason=account_state.usage_refused_reason(grok_cached, retry_at=grok_retry_at),
                **({'plan_inactive_at': grok_cached.get('error_at') or grok_cached.get('fetched_at')}
                   if (grok_cached.get('error_at') or grok_cached.get('fetched_at')) else {}),
                **({'plan_inactive_retry_at': grok_retry_at} if grok_retry_at else {}),
            )
        elif grok_row['status'] == 'ready' and account_state.usage_refused_credentials(grok_cached, COLLECTOR_REJECTED.get('grok', ())):
            grok_row.update(status=account_state.AUTH_FAILED, reason='credentials_rejected_by_collector')
    elif grok_cached and grok_ckey:
        grok_row['usage_binding'] = 'changed'
    result['accounts']['grok'] = grok_row
    result['launchers']['grok'] = {'family': 'grok', 'status': grok_row['status']}
    # Oh My Pi is Paseo's built-in `omp` provider. The `omp` account is its API-key side (~/.omp/agent/models.yml or
    # stored API keys), so it is metered: explicit selection only. OAuth subscription logins are accounts of their own
    # below.
    omp_native = (bool(_which('omp', environ.get('PATH')))
                  or (home / '.bun' / 'bin' / ('omp.exe' if os.name == 'nt' else 'omp')).is_file())
    omp_defined = 'omp' in definitions
    _, _, omp_enabled = resolve_definition('omp', definitions)
    omp_status = ('disabled' if not omp_enabled or not omp_defined
                  else 'ready' if omp_native or obj(definitions.get('omp')).get('command') else 'missing_client')
    omp_sources = (['native'] if omp_native else []) + (['paseo'] if omp_defined else [])
    result['accounts']['omp'] = {'family': 'omp', 'provider': 'omp',
        'sources': omp_sources, 'status': omp_status, 'plan': 'API (Oh My Pi)', 'billing': 'metered'}
    result['launchers']['omp'] = {'family': 'omp', 'status': omp_status}
    # Each OAuth subscription login in omp is its own account. Like Pi on ChatGPT, one that signs in as a native
    # account (same identity) shares that account's quota (`quota_account`); otherwise it is the subscription's only
    # way in and routes on the family's plan defaults. Billing is subscription either way.
    omp_dir = home / '.omp' / 'agent'
    omp_found = omp_credentials(omp_dir)
    logins = omp_found['logins']
    cached_models = omp_models(omp_dir) if logins else {}
    models_file = any((omp_dir / f).is_file() for f in ('models.yml', 'models.yaml'))
    if omp_status == 'ready' and not models_file and not omp_found['api_keys']:
        # Nothing shows an API key (no models.yml, no stored key; keys only in environment variables cannot be seen).
        # With only subscription logins there is no API side to report; an unreadable database hides both.
        if omp_found['problem']:
            result['accounts']['omp'].update(status='missing_credentials', reason='agent.db unreadable')
        elif logins:
            del result['accounts']['omp']
            result['launchers']['omp']['api_side'] = False
    taken = {}
    for login in logins:
        provider, family = login['provider'], login['family']
        taken[provider] = taken.get(provider, 0) + 1
        name = f'omp-{provider}' + (f'-{taken[provider]}' if taken[provider] > 1 else '')
        auth, profile = omp_auth(family, login['data'])
        facts = auth_facts(family, auth, omp_dir, profile)
        owner = next((cname for cname, crow in result['accounts'].items()
                      if crow.get('family') == family and crow.get('identity') and crow['identity'] == facts['identity']),
                     None)
        plan = facts['plan'] or (result['accounts'][owner].get('plan') if owner else None)
        result['accounts'][name] = {
            'family': 'omp', 'provider': 'omp', 'quota_family': family, 'omp_provider': provider,
            'home': str(omp_dir), 'sources': omp_sources, 'status': omp_status,
            'plan': plan, 'identity': facts['identity'], 'billing': 'subscription',
            **({'omp_models': cached_models[provider]} if cached_models.get(provider) else {}),
            **({'quota_account': owner} if owner else {}),
        }
        bindings[name] = {'identity': facts['identity'], 'credential_identity': facts.get('credential_identity'),
                          'base': '', 'key': None}
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
    raw_pi = resolve_definition('pi', definitions)[1].get('PI_CODING_AGENT_DIR')
    pi_home = _expand_home(raw_pi, home) if raw_pi else home / '.pi/agent'
    pi_auth, _ = read_json(pi_home / 'auth.json')
    pi_codex = obj(pi_auth.get('openai-codex'))
    pi_installed = bool(_which('pi', environ.get('PATH')))
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
        pi_matches = bool(pi_cached.get('credential_key') and pi_cached.get('credential_key') == pi_facts.get('credential_key'))
        # Pi's login carries no id_token; its plan is what the usage API or the account it signs in as reports.
        pi_online = configured_plan('codex', pi_cached.get('plan_label')) if pi_matches and pi_cached.get('plan_label') else None
        pi_plan = pi_facts['plan'] or pi_online or (result['accounts'][matched_codex].get('plan') if matched_codex else None)
        pi_row = {
            'family': 'pi',
            'quota_family': 'codex',
            'provider': 'pi',
            'home': str(pi_home),
            'cli_installed': pi_installed,
            'sources': ['native'] + (['paseo'] if 'pi' in definitions else []),
            'status': 'ready' if pi_launcher['status'] == 'ready' else pi_launcher['status'],
            'plan': pi_plan,
            'credential_key': pi_facts['credential_key'],
            'identity': pi_facts['identity'],
            'billing': 'subscription',
            **({'plan_source': 'usage_api', 'plan_read_at': pi_cached.get('fetched_at')} if not pi_facts['plan'] and pi_online else {}),
            **({'quota_account': matched_codex} if matched_codex else {}),
        }
        bindings['pi'] = {'identity': pi_facts['identity'], 'credential_identity': pi_facts.get('credential_identity'),
                          'base': '', 'key': None}
        if pi_matches:
            pi_row['usage_binding'] = 'matches'
            if pi_row['status'] == 'ready' and account_state.usage_refused_plan(pi_cached):
                pi_retry_at = account_state.usage_refused_retry_at(
                    pi_cached, poll=cache_polls.get('pi'), attempt=cache_attempts.get('pi'), now=now
                )
                pi_row.update(
                    status=account_state.PLAN_INACTIVE,
                    reason=account_state.usage_refused_reason(pi_cached, retry_at=pi_retry_at),
                    **({'plan_inactive_at': pi_cached.get('error_at') or pi_cached.get('fetched_at')}
                       if (pi_cached.get('error_at') or pi_cached.get('fetched_at')) else {}),
                    **({'plan_inactive_retry_at': pi_retry_at} if pi_retry_at else {}),
                )
            elif pi_row['status'] == 'ready' and account_state.usage_refused_credentials(pi_cached, COLLECTOR_REJECTED['codex']):
                pi_row.update(status=account_state.AUTH_FAILED, reason='credentials_rejected_by_collector')
        elif pi_cached:
            pi_row['usage_binding'] = 'changed'
        result['accounts']['pi'] = pi_row
    elif pi_installed or pi_home.is_dir() or (home / '.pi').is_dir():
        result['accounts']['pi'] = {
            'family': 'pi',
            'provider': 'pi',
            'home': str(pi_home),
            'cli_installed': pi_installed,
            'sources': ['native'] + (['paseo'] if 'pi' in definitions else []),
            'status': 'disabled' if pi_launcher['status'] == 'disabled' else 'missing_credentials',
            'plan': None,
            'billing': 'subscription',
        }
    override_env = (environ.get('AGENTCOMPASS_ACCOUNTS_OVERRIDE') or environ.get('ROUTE_MANAGER_ACCOUNTS_OVERRIDE')
                    or environ.get('ROUTER_ACCOUNTS_OVERRIDE'))
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
    # The bridge is only ready when its config reports proAvailable: a ready bridge is a Pro web seat.
    if result['accounts']['chatgpt-web']['status'] == 'ready':
        result['accounts']['chatgpt-web']['plan'] = 'pro'
    # Remote external runner. No model benchmarks, inferred quota or credential reads.
    bot_config, bot_error = read_json(state_dir / 'grok-bot/config.json')
    bot_configured = bot_error is None and isinstance(bot_config.get('bot_id'), str) and bool(bot_config['bot_id'].strip())
    bot_status = 'missing_configuration' if not bot_configured else \
        'ready' if GROK_BOT_CLI.is_file() and _which('node', environ.get('PATH')) else 'missing_client'
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
    _assign_quota_owners(result['accounts'])
    import quota_exhaustion
    exhaustion_path = state_dir / 'quota-exhaustion.json'
    exhausted_accounts = obj(read_json(exhaustion_path)[0].get('accounts')) if exhaustion_path.is_file() else {}
    for name, row in result['accounts'].items():
        if not obj(exhausted_accounts.get(name)).get('blocked'):
            continue
        latched = quota_exhaustion.exhausted_windows(name, cache_rows.get(name), path=exhaustion_path)
        if quota_exhaustion.ALL in latched:
            row.update(status='quota_exhausted', reason='confirmed quota exhausted; refresh quota after recovery')
        elif latched:
            # Only these windows are out; the account's other windows stay routable.
            row['exhausted_windows'] = sorted(latched)
    _share_online_plan(result['accounts'])
    _settle_account_states(result, cache_rows, previous, state_dir, now, environ, override_doc, bindings)
    return result


def _assign_quota_owners(accounts):
    owners = {}
    ordered = sorted(
        accounts,
        key=lambda name: (
            accounts[name]['status'] != 'ready',
            accounts[name]['status'] not in account_state.LISTED_STATES,
            name != accounts[name]['family'],
            name,
        ),
    )
    for name in ordered:
        row = accounts[name]
        ident = row.get('identity')
        if ident and (row.get('family') in ('claude', 'codex') or not row.get('quota_account')):
            row['quota_account'] = owners.setdefault((row['family'], ident), name)
    for name in ordered:
        row = accounts[name]
        ident = row.get('identity')
        qfam = row.get('quota_family') or row['family']
        if ident and row.get('quota_account') and (qfam, ident) in owners:
            row['quota_account'] = owners[(qfam, ident)]


def _share_online_plan(rows):
    """Accounts on one subscription have one plan: the newest usage-API plan_type among them beats an older claim."""
    online = {}
    for row in rows.values():
        owner = row.get('quota_account')
        if owner and row.get('plan_source') == 'usage_api' and str(row.get('plan_read_at') or '') >= online.get(owner, ('', ''))[0]:
            online[owner] = (str(row.get('plan_read_at') or ''), row['plan'])
    for row in rows.values():
        owner = row.get('quota_account')
        if owner in online and row.get('billing') == 'subscription':
            read_at, plan = online[owner]
            if row.get('plan_source') == 'usage_api' and str(row.get('plan_read_at') or '') >= read_at:
                continue
            claim = row.get('plan')
            if claim and row.get('plan_source') == 'credential' and configured_plan('codex', claim) != plan:
                row['plan_conflict'] = {'online': plan, 'credential': claim}
            row.update(plan=plan, plan_source='usage_api_shared', plan_read_at=read_at)


def _reads_usage(name, row, override_doc=None):
    """Subscription accounts the collector reads. An installed agy with no proven sign-in is not an account yet."""
    if row.get('billing') != 'subscription':
        return False
    family = row.get('family')
    if family in ('claude', 'codex', 'pi'):
        return True
    if family == 'antigravity':
        verified = obj(obj((override_doc or {}).get('accounts')).get(name)).get('verified') is True
        return sign_in_proven(row) or verified
    if family == 'omp':
        return row.get('quota_account') not in (None, name)   # a login of its own has no collector
    return family == 'grok' and bool(row.get('usage_windows') or row.get('usage_read_failed'))


def _settle_account_states(result, cache_rows, previous, state_dir, now, environ, override_doc, bindings=None):
    """Give each account its one state (account_state). A snapshot with no rows at all means no collector has run on
    this machine; route_judge.readiness_gap reports that, so accounts are not each marked as failing."""
    rows = result['accounts']
    members = {}
    for name, row in rows.items():
        members.setdefault(row.get('quota_account') or name, []).append(name)
    previous_rows = obj(previous.get('accounts'))
    hours = account_state.exclude_after_hours(environ, override_doc)
    latches = account_state.latched(state_dir)

    def _cred_id(slot):
        return (bindings or {}).get(slot, {}).get('credential_identity') or rows[slot].get('credential_key')

    def _refused_at(slot):
        crow = obj(cache_rows.get(slot))
        return account_state._time(crow.get('error_at') or crow.get('fetched_at'))

    # A sibling with a good reading newer than a cached refusal supersedes that stale refusal (e.g. after renewal).
    for group in members.values():
        good_by_slot = {}
        for slot in group:
            if rows[slot].get('usage_binding') == 'changed':
                continue
            issue, good_iso = account_state.reading(cache_rows.get(slot), now)
            qfam = rows[slot].get('quota_family') or rows[slot].get('family')
            if issue is None and not account_state.plan_is_inactive(qfam, rows[slot].get('plan')):
                good_time = account_state._time(good_iso)
                if good_time is not None:
                    good_by_slot[slot] = good_time
        newest_group_good = max(good_by_slot.values(), default=None)
        for slot in group:
            crow = obj(cache_rows.get(slot))
            ref_at = _refused_at(slot)
            if rows[slot].get('status') == account_state.PLAN_INACTIVE and account_state.usage_refused_plan(crow):
                if newest_group_good is not None and (ref_at is None or newest_group_good > ref_at):
                    rows[slot]['status'] = account_state.READY
                    rows[slot].pop('reason', None)
                    rows[slot].pop('plan_inactive_at', None)
                    rows[slot].pop('plan_inactive_retry_at', None)
            elif rows[slot].get('status') == account_state.AUTH_FAILED:
                cid = _cred_id(slot)
                newest_cred_good = max((t for s, t in good_by_slot.items() if cid and _cred_id(s) == cid), default=None)
                if newest_cred_good is not None and (ref_at is None or newest_cred_good > ref_at):
                    rows[slot]['status'] = account_state.READY
                    rows[slot].pop('reason', None)

    for name, row in rows.items():
        reads = _reads_usage(name, row, override_doc)
        group = sorted(members.get(row.get('quota_account') or name) or [name], key=lambda other: other != name)
        problem, last_good = None, None
        if reads:
            # Readings of the same subscription count for each other; a reading taken with another credential does not.
            found = [('credential_changed', None) if rows[other].get('usage_binding') == 'changed'
                     else account_state.reading(cache_rows.get(other), now) for other in group]
            good = [last for issue, last in found if issue is None]
            times = sorted(last for _, last in found if last)
            problem, last_good = (None, max(good)) if good else (found[0][0], times[-1] if times else None)
            row['last_success_at'] = last_good
        latch_name = name if name in latches else row.get('quota_account') if row.get('quota_account') in latches else None
        latch = latches.get(latch_name) if latch_name else None
        if latch and not latch.get('credential_key'):
            latch_row = rows.get(latch_name) or row
            if latch_row.get('credential_key'):
                online_plan = latch_row.get('plan') if latch_row.get('plan_source') in account_state.ONLINE_PLAN_SOURCES else None
                account_state.seal_latch_credential(latch_name, latch_row['credential_key'], online_plan=online_plan,
                                                    state_dir=state_dir, expected_observed_at=latch.get('observed_at'))
                latch['credential_key'] = latch_row['credential_key']
                if online_plan and not latch.get('online_plan'):
                    latch['online_plan'] = online_plan
        if latch and account_state.latch_outlived(latch, last_good if problem is None else None, row, now=now):
            if account_state.clear_latch(latch_name, state_dir=state_dir, expected_observed_at=latch.get('observed_at')):
                latches.pop(latch_name, None)
                latch = None
        # Re-login recovery: an account held because its provider refused it (provider_retry) lifts the same way, on a
        # good usage reading newer than the hold or another credential. Accounts without a usage reader keep a clear.
        if reads:
            try:
                provider_retry.lift_after_reading(name, last_good=last_good if problem is None else None,
                                                  credential_key=row.get('credential_key'),
                                                  path=Path(state_dir) / provider_retry.FILE
                                                  if state_dir and not os.environ.get('ROUTER_PROVIDER_RETRY') else None)
            except OSError:
                pass
        if row.get('status') != account_state.READY:
            continue
        inactive_peer = next(
            (rows[other] for other in group
             if rows[other].get('usage_binding') != 'changed' and rows[other].get('status') == account_state.PLAN_INACTIVE),
            None,
        )
        cred_id = _cred_id(name)
        auth_peer = next(
            (rows[other] for other in group
             if rows[other].get('usage_binding') != 'changed' and rows[other].get('status') == account_state.AUTH_FAILED
             and cred_id and cred_id == _cred_id(other)),
            None,
        )
        if inactive_peer is not None:
            row.update(status=account_state.PLAN_INACTIVE, reason=inactive_peer.get('reason'))
            for k in ('plan_inactive_at', 'plan_inactive_retry_at'):
                if inactive_peer.get(k):
                    row[k] = inactive_peer[k]
        elif auth_peer is not None:
            row.update(status=account_state.AUTH_FAILED, reason=auth_peer.get('reason'))
        elif account_state.plan_is_inactive(row.get('quota_family') or row.get('family'), row.get('plan')):
            row.update(status=account_state.PLAN_INACTIVE, reason=f"plan {row.get('plan')}")
        elif latch:
            row.update(status=account_state.PLAN_INACTIVE, reason=account_state.latch_reason(latch),
                       plan_inactive_at=latch.get('observed_at'),
                       plan_inactive_retry_at=account_state.latch_retry_at(latch))
        elif reads and problem and cache_rows:
            cached_at = next((obj(cache_rows.get(o)).get('error_at') or obj(cache_rows.get(o)).get('fetched_at')
                              for o in group
                              if obj(cache_rows.get(o)).get('error_at') or obj(cache_rows.get(o)).get('fetched_at')), None)
            account_state.failing(row, problem, last_good, previous_rows.get(name), now, hours, cached_at=cached_at)
        if name in result['launchers'] and not account_state.dispatchable(row):
            result['launchers'][name]['status'] = row['status']
    row = rows.get('grok')
    bot_row = rows.get('grok-bot')
    if (isinstance(row, dict) and isinstance(bot_row, dict)
            and bot_row.get('status') == account_state.READY
            and row.get('status') in (account_state.PLAN_INACTIVE, account_state.AUTH_FAILED, account_state.USAGE_FAILING)
            and not account_state.dispatchable(row)):
        for k in ('status', 'failing_since', 'exclude_after_hours', 'usage_excluded',
                  'last_success_at', 'plan_inactive_at', 'plan_inactive_retry_at'):
            if k in row:
                bot_row[k] = row[k]
        if row.get('reason'):
            bot_row['reason'] = row.get('reason')
        if 'grok-bot' in result['launchers']:
            result['launchers']['grok-bot']['status'] = bot_row['status']
    _assign_quota_owners(rows)


# The variables that point an account directory elsewhere, with the directory each names by default.
ACCOUNT_DIR_VARIABLES = (('CLAUDE_CONFIG_DIR', '.claude'), ('CLAUDE_SECURESTORAGE_CONFIG_DIR', '.claude'),
                         ('CODEX_HOME', '.codex'), ('PI_CODING_AGENT_DIR', '.pi/agent'))


def environment_rewrites_accounts(home, environ=None):
    """True when this process's environment points an account directory somewhere else (a worker started on another
    account). Discovery ignores those variables, but such a process does not speak for the machine's shared files."""
    environ = os.environ if environ is None else environ
    for key, default in ACCOUNT_DIR_VARIABLES:
        value = environ.get(key)
        if value and _expand_home(value, Path(home)).resolve() != (Path(home) / default).resolve():
            return True
    return False


def current():
    # Reread local product files each time; do not contact any gateway.
    if _SCOPE and 'current' in _SCOPE[0]:
        return copy.deepcopy(_SCOPE[0]['current'])
    result = discover()
    home = Path(os.environ.get('ROUTER_PROVIDER_HOME') or Path.home())
    target = Path(os.environ.get('ROUTER_STATE_DIR') or home / '.agent-compass') / 'provider-inventory.json'
    try:
        fingerprint = json.dumps({k: v for k, v in result.items() if k != 'generated_at'}, sort_keys=True)
        if ((_LAST_PUBLISHED_INVENTORY.get(target) != fingerprint or not target.is_file())
                and not environment_rewrites_accounts(home)):
            write_json(target, result)
            _LAST_PUBLISHED_INVENTORY[target] = fingerprint
    except OSError:
        pass
    if _SCOPE:
        _SCOPE[0]['current'] = copy.deepcopy(result)
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


# The plan names each family reports or a user may write, word for word once the vendor's name is dropped. Anything
# else stays what it is, has no template, and is priced at UNKNOWN_PLAN_PRICED_AS with a warning. `prolite` is the usage
# API's name for the $100 tier (an inference from the Codex binary and the price ladder), so it is `pro100`, never `pro`.
PLAN_NAMES = {
    'claude': {'pro': 'pro', 'max': 'max', 'max5': 'max5', 'max 5x': 'max5', 'max x5': 'max5', '5x': 'max5',
               'x5': 'max5', 'max20': 'max20', 'max 20x': 'max20', 'max x20': 'max20', '20x': 'max20',
               'x20': 'max20', 'free': 'free', 'team': 'team', 'enterprise': 'enterprise'},
    'codex': {'plus': 'plus', 'pro': 'pro', 'pro100': 'pro100', 'pro 100': 'pro100', '100': 'pro100',
              'prolite': 'pro100', 'pro lite': 'pro100', 'free': 'free', 'team': 'team', 'enterprise': 'enterprise'},
    'antigravity': {'pro': 'pro', 'ultra': 'ultra', 'ultra5': 'ultra5', 'ultra 5x': 'ultra5',
                    'ultra x5': 'ultra5', 'ultra20': 'ultra20', 'ultra 20x': 'ultra20', 'ultra x20': 'ultra20',
                    'free': 'free', 'free tier': 'free'},
}
UNKNOWN_PLAN_PRICED_AS = {'claude': 'pro', 'codex': 'plus', 'antigravity': 'pro'}


def configured_plan(family, label):
    raw = str(label or '').lower()
    names = PLAN_NAMES.get(family)
    if names is None:
        return raw
    for candidate in (raw, account_state.strip_pool_count(raw)):
        words = account_state.plan_words(candidate)
        if words in names:
            return names[words]
        without_vendor = account_state.plan_without_vendor(candidate)
        if without_vendor in names:
            return names[without_vendor]
    return raw


def unavailable_reason(inventory, account, provider=None):
    accounts = obj(inventory.get('accounts'))
    prov_row = obj(accounts.get(provider)) if provider and provider != account else {}
    row = prov_row if prov_row.get('status') in account_state.LISTED_STATES else obj(accounts.get(account))
    launcher = obj(obj(inventory.get('launchers')).get(provider or account))
    if account_state.unavailable_text(row):
        return f'provider discovery: {account_state.unavailable_text(row)}'
    reason = (row.get('reason') or row.get('status')) if row.get('status') is not None and not account_state.dispatchable(row) else launcher.get('status')
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
# Plans whose models run without any account: there is no sign-in to prove (OpenCode's free models).
NO_SIGN_IN_PLANS = {'opencode': ('free',)}


def needs_no_sign_in(row):
    return isinstance(row, dict) and row.get('plan') in NO_SIGN_IN_PLANS.get(row.get('family'), ())


def sign_in_proven(row):
    """True when discovery can tell this account is signed in, not merely that its CLI is installed."""
    if not isinstance(row, dict) or row.get('family') not in UNVERIFIABLE_SIGN_IN or needs_no_sign_in(row):
        return True
    sources = row.get('sources') or []
    if isinstance(sources, str):
        sources = [sources]
    return bool({'usage-cache', 'account-tier'} & set(sources)) or bool(row.get('identity') or row.get('credential_key'))


def launch_verified(row, previous):
    """Decide whether a discovered account may be a confident routing pick.

    Antigravity, OpenCode and Grok report status ready when the CLI is installed, which is not a
    sign-in. The shipped template leaves them unverified. A machine fleet that already marked the
    slot verified, or a usage-cache reading, keeps the previous launchable state. The status string
    itself stays `ready` so provider listings do not change.
    """
    if not isinstance(row, dict):
        return False
    ready = account_state.dispatchable(row)
    if row.get('family') not in UNVERIFIABLE_SIGN_IN or sign_in_proven(row):
        return ready
    return bool(previous is True and ready)


def account_state_label(row):
    """Same words setup uses when a CLI is installed but sign-in cannot be read."""
    status = row.get('status') if isinstance(row, dict) else None
    if (isinstance(row, dict) and row.get('family') in UNVERIFIABLE_SIGN_IN and status == 'ready'
            and not needs_no_sign_in(row)):
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
            # Oh My Pi's API-key side (the template `omp` account) when discovery found only subscription logins.
            if (family == 'omp' and name not in inventory['accounts']
                    and obj(obj(inventory.get('launchers')).get('omp')).get('api_side') is False):
                specs.pop(name, None)
    known_models = accounts.get('models') if isinstance(accounts.get('models'), dict) else {}
    aliases = accounts.get('aliases') if isinstance(accounts.get('aliases'), dict) else {}

    for name, row in inventory['accounts'].items():
        family = row['family']
        is_litellm = row.get('backend_kind') == 'litellm'
        if name not in specs:
            if row['status'] not in account_state.LISTED_STATES:
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
                base_tpl['plan'] = row.get('plan') or 'unknown'
                base_tpl['billing'] = 'subscription'
                base_tpl['bundles'] = {
                    f'{name}__{bid}': {**{k: v for k, v in b.items() if k != 'modeId'}, 'provider': 'pi', 'harness': 'pi'}
                    for bid, b in base_tpl.get('bundles', {}).items()
                    if b.get('provider') == 'pi'
                }
                specs[name] = base_tpl
            elif family == 'omp' and row.get('quota_family') in templates:
                # The family's own bundles, launched through omp with omp's `<provider>/<model>` ids. A login that
                # shares a native account's quota hands these to that account below (quota_account), beside its
                # native and Pi siblings; harness_order ranks the siblings.
                qfamily = row['quota_family']
                spec = copy.deepcopy(templates[qfamily])
                spec['billing'] = 'subscription'
                cached = set(row.get('omp_models') or [])
                spec['bundles'] = {
                    f'{name}__{bid}': {'model': b['model'], **({'effort': b['effort']} if b.get('effort') else {}),
                                       'provider': 'omp', 'harness': 'omp',
                                       'launch_model': f"{row['omp_provider']}/{b.get('launch_model') or b['model']}"}
                    for bid, b in spec.get('bundles', {}).items()
                    if b.get('provider', qfamily) == qfamily and (not cached or (b.get('launch_model') or b['model']) in cached)}
                specs[name] = spec
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
        quota_family = row.get('quota_family') or family   # an omp OAuth login uses the plans of the family it signs in to
        # A plan the user wrote in the account document seeds the template when discovery reports none
        # (claude/codex without a discovered plan stay unknown below).
        template_plan = configured_plan(quota_family, plan or (spec.get('plan') if family not in ('claude', 'codex') else None))
        template = plan_defaults(accounts, quota_family, template_plan) if template_plan else None
        # A plan no template names is unknown: it is priced at the family's smallest paid plan and warned about, never
        # at the largest. An account on a metered API key has no plan to know.
        priced_as = UNKNOWN_PLAN_PRICED_AS.get(quota_family) if template is None and row.get('billing') == 'subscription' else None
        spec.pop('plan_unknown', None)
        spec.pop('plan_priced_as', None)
        if priced_as and not account_state.plan_is_inactive(quota_family, plan):
            template = plan_defaults(accounts, quota_family, priced_as)
            spec.update(plan_unknown=str(plan or template_plan or 'not reported'), plan_priced_as=priced_as)
        elif template is None and family not in ('claude', 'codex'):
            # A family with no known-plan table routes on its documented default plan, never on no capacity
            # (plan-templates.json `<family>.default`).
            template = plan_defaults(accounts, quota_family, 'default')
        if plan:
            previous_plan = configured_plan(quota_family, spec.get('plan'))
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
            elif spec.get('plan_priced_as'):
                for key in TEMPLATE_KEYS:
                    if key in template:
                        spec[key] = copy.deepcopy(template[key])
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
    from allocator import bundle_harness
    for name, row in inventory['accounts'].items():
        owner = row.get('quota_account')
        if owner and owner != name and name in specs and owner in specs:
            # A second account on the owner's subscription (Pi signed in as the Codex account) re-derives what the
            # owner already launches through the same launcher; the owner's rows and its enable/disable curation stand.
            def launch_key(bid, bundle, account):
                model = resolve_canonical_model(bundle.get('model'), known_models, aliases) or bundle.get('model')
                return bundle.get('provider', account), bundle_harness(bundle, account, bid), model, bundle.get('effort')
            offered = {launch_key(bid, b, owner) for bid, b in specs[owner].get('bundles', {}).items()}
            for bid, bundle in specs[name].get('bundles', {}).items():
                if launch_key(bid, bundle, name) not in offered:
                    specs[owner]['bundles'][bid] = {**bundle, 'provider': bundle.get('provider', name)}
            specs[name]['bundles'] = {}
            specs[name]['quota_account'] = owner
    apply_shared_quota(accounts)
    if live:
        import live_models
        if live_models.enabled():
            accounts['_live_models'] = live_models.load_or_refresh(accounts=accounts)
    return accounts


def merge_usage(usage, accounts, now, names=None):
    """Invalidate another credential's cached quota; never probe an upstream."""
    inventory = accounts.get('_discovery') or {}
    result = copy.deepcopy(usage)
    sources = quota_sources(accounts)
    relevant = None if names is None else (set(names) | {sources[n] for n in names if n in sources})
    for name, row in (inventory.get('accounts') or {}).items():
        if (relevant is None or name in relevant) and row.get('usage_binding') == 'changed':
            result.setdefault('accounts', {})[name] = {
                'status': 'unknown', 'reason': 'credential_changed', 'windows': []}
    # If a launcher is disabled but another exposes the same subscription, its
    # matching fresh reading still belongs to that subscription's quota owner.
    for name, row in (inventory.get('accounts') or {}).items():
        owner = row.get('quota_account')
        source = obj(obj(result.get('accounts')).get(name))
        target = obj(obj(result.get('accounts')).get(owner))
        owner_row = obj(obj(inventory.get('accounts')).get(owner))
        # Only a reading of the owner's own identity crosses over, and never onto a credential that has since changed.
        if (owner and owner != name and (names is None or owner in names)
                and row.get('usage_binding') == 'matches' and source.get('status') == 'fresh'
                and target.get('status') != 'fresh'
                and row.get('identity') and row.get('identity') == owner_row.get('identity')
                and owner_row.get('usage_binding') != 'changed'):
            result.setdefault('accounts', {})[owner] = copy.deepcopy(source)
    rows = result.setdefault('accounts', {})
    for name, source in quota_sources(accounts).items():
        if names is None or name in names:
            rows[name] = {**copy.deepcopy(rows.get(source) or {
                'status': 'unknown', 'reason': 'account_missing', 'windows': []}), 'quota_source': source}
    return result
