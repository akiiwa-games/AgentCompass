"""Read-only upgrade checks for local ~/.agent-compass state, hooks and global rule blocks.

Every diagnostic names the offending file and one of the three upgrade fixes:
  - re-run "agentcompass setup"
  - re-toggle in the tray
  - delete "<file>"

Runtime loaders call the same validators so old-shape files fail fast with the
same message `agentcompass doctor` prints, instead of being silently migrated.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

FIX_SETUP = 're-run "agentcompass setup"'
FIX_TRAY = 're-toggle in the tray'

HOST_POLICY_KEYS = frozenset({'allowed_accounts', 'judges', 'usage_providers', 'known_accounts'})
RULE_BEGIN = '<!-- agentcompass:begin -->'
RULE_END = '<!-- agentcompass:end -->'
LEGACY_RULE_TOKENS = (
    '@@ROUTE_JUDGE@@',
    'ROUTER_WORKER',
    'ROUTER_ACCOUNTS_OVERRIDE',
    'ROUTER_LIVE_MODELS',
    'ROUTE_MANAGER_',
    'route-agent-work',
    'route_judge.py',
)
LEGACY_HOOK_TOKENS = (
    'route-agent-work',
    'cli/_internal/agentcompass/data/setup/router_gate_hook.py',
    'ROUTER_WORKER',
    'ROUTER_PASEO_LEGACY_NO_LABELS',
    'ROUTER_ACCOUNTS_OVERRIDE',
)


def fix_delete(path: Path | str) -> str:
    return f'delete "{path}"'


def format_issue(path: Path | str, detail: str, fix: str) -> str:
    return f'{path}: {detail}. Fix: {fix}.'


def _env(environ: dict | None) -> dict:
    return os.environ if environ is None else environ


def state_dir(home: Path | None = None, environ: dict | None = None) -> Path:
    env = _env(environ)
    configured = str(env.get('ROUTER_STATE_DIR') or '').strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    base = Path(home) if home is not None else Path.home()
    return base / '.agent-compass'


def _find_non_bool_disabled(node, prefix: str = '') -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            at = f'{prefix}.{key}' if prefix else str(key)
            if key == 'disabled':
                if not isinstance(value, bool):
                    return f'non-boolean disabled ({value!r}) at {at}'
            else:
                found = _find_non_bool_disabled(value, at)
                if found:
                    return found
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found = _find_non_bool_disabled(value, f'{prefix}[{index}]')
            if found:
                return found
    return None


def validate_accounts_override_doc(doc, path: Path | str) -> None:
    if not isinstance(doc, dict):
        raise ValueError(format_issue(path, 'older or invalid accounts-override format (expected JSON object)', FIX_TRAY))
    version = doc.get('schema_version')
    if version != 2:
        raise ValueError(format_issue(
            path,
            f'older accounts-override schema_version {version!r} (expected 2)',
            FIX_TRAY,
        ))
    if 'migration_ambiguous_enabled' in doc:
        raise ValueError(format_issue(
            path,
            'older accounts-override field migration_ambiguous_enabled is no longer supported',
            FIX_TRAY,
        ))
    accounts = doc.get('accounts')
    if accounts is not None:
        if not isinstance(accounts, dict):
            raise ValueError(format_issue(path, 'invalid accounts-override.accounts (expected JSON object)', FIX_TRAY))
        for name, spec in accounts.items():
            if not isinstance(spec, dict):
                raise ValueError(format_issue(
                    path, f'invalid accounts-override entry for {name!r} (expected JSON object)', FIX_TRAY,
                ))
            if spec.get('verified') is False:
                raise ValueError(format_issue(
                    path, f'legacy accounts.{name}.verified=false user switch',
                    f'remove accounts.{name}.verified from this file, then check or uncheck {name} in the tray',
                ))
            if 'allowed_cwd' in spec:
                raise ValueError(format_issue(
                    path, f'older field accounts.{name}.allowed_cwd (replaced by cwd_regex)', FIX_TRAY,
                ))
    bad_disabled = _find_non_bool_disabled(doc)
    if bad_disabled:
        raise ValueError(format_issue(path, f'older accounts-override format ({bad_disabled})', FIX_TRAY))


def check_accounts_override(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        return format_issue(path, f'accounts-override unreadable ({exc})', FIX_TRAY)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return format_issue(path, f'accounts-override invalid JSON ({exc})', FIX_TRAY)
    try:
        validate_accounts_override_doc(doc, path)
    except ValueError as exc:
        return str(exc)
    return None


def validate_host_policy_doc(doc, path: Path | str) -> dict:
    if not isinstance(doc, dict) or 'allowed_accounts' not in doc:
        raise ValueError(format_issue(
            path, 'host-policy configuration requires allowed_accounts', FIX_TRAY,
        ))
    unknown = sorted(set(doc) - HOST_POLICY_KEYS)
    if unknown:
        raise ValueError(format_issue(
            path, f'host-policy configuration has unsupported fields ({", ".join(unknown)})', FIX_TRAY,
        ))
    for key, value in doc.items():
        if not isinstance(value, list) or any(not isinstance(v, str) or not v or v != v.strip() for v in value):
            raise ValueError(format_issue(
                path, f'host-policy configuration {key} must be a list of nonempty names', FIX_TRAY,
            ))
    if any(v not in ('codex', 'sonnet') for v in doc.get('judges', [])):
        raise ValueError(format_issue(
            path, 'host-policy configuration judges must name codex or sonnet', FIX_TRAY,
        ))
    return doc


def check_host_policy(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except UnicodeError:
        return format_issue(path, 'host-policy configuration must be UTF-8', FIX_TRAY)
    except OSError as exc:
        return format_issue(path, f'host-policy configuration unreadable ({exc})', FIX_TRAY)
    try:
        doc = json.loads(raw)
    except ValueError:
        return format_issue(path, 'host-policy configuration invalid JSON', FIX_TRAY)
    try:
        validate_host_policy_doc(doc, path)
    except ValueError as exc:
        return str(exc)
    return None


def check_user_scores(path: Path) -> str | None:
    if not path.exists():
        return None
    import user_scores
    try:
        user_scores.load_local(path)
    except ValueError as exc:
        return str(exc)
    return None


def check_host_rule_file(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        return format_issue(path, f'host-rules file unreadable ({exc})', fix_delete(path))
    found = [tok for tok in LEGACY_RULE_TOKENS if tok in text]
    if found:
        return format_issue(
            path,
            f'older host-rules overlay contains removed token(s): {", ".join(found)}',
            fix_delete(path),
        )
    return None


def _validate_profile(profile) -> None:
    import task_intake
    task_intake.validate_profile(profile)


def row_has_old_profile(row: dict) -> bool:
    """True when a ledger row carries a task profile that fails the four-category schema."""
    if not isinstance(row, dict):
        return False
    snap = row.get('decision_snapshot')
    if isinstance(snap, dict):
        snap_task = snap.get('task')
        if isinstance(snap_task, dict) and 'profile' in snap_task and snap_task['profile'] is not None:
            try:
                _validate_profile(snap_task['profile'])
            except ValueError:
                return True
    if 'profile' in row and row['profile'] is not None:
        try:
            _validate_profile(row['profile'])
        except ValueError:
            return True
    return False


def check_ledger(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        return format_issue(path, f'ledger unreadable ({exc})', fix_delete(path))
    bad_json = 0
    old_profiles = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad_json += 1
            continue
        if not isinstance(row, dict):
            bad_json += 1
            continue
        if row_has_old_profile(row):
            old_profiles += 1
    parts = []
    if old_profiles:
        parts.append(f'{old_profiles} older-format profile row(s)')
    if bad_json:
        parts.append(f'{bad_json} malformed JSON row(s)')
    if parts:
        return format_issue(path, 'ledger contains ' + ' and '.join(parts), fix_delete(path))
    return None


def count_old_decision_rows(conn: sqlite3.Connection) -> int:
    old_rows = 0
    for (blob,) in conn.execute('SELECT data FROM decisions').fetchall():
        try:
            doc = json.loads(blob)
        except (TypeError, ValueError):
            old_rows += 1
            continue
        if not isinstance(doc, dict):
            old_rows += 1
            continue
        for key in ('allocation_task', 'task'):
            task = doc.get(key)
            if isinstance(task, dict) and task.get('profile') is not None:
                try:
                    _validate_profile(task['profile'])
                except ValueError:
                    old_rows += 1
                    break
    return old_rows


def check_dispatch_db(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        uri = path.resolve().as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except (OSError, sqlite3.Error) as exc:
        return format_issue(path, f'dispatch database unreadable ({exc})', fix_delete(path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {'decisions', 'dispatches'}.issubset(tables):
            return format_issue(
                path, 'dispatch database has older schema (missing decisions/dispatches tables)', fix_delete(path),
            )
        old_rows = count_old_decision_rows(conn)
        if old_rows:
            return format_issue(
                path, f'dispatch database contains {old_rows} older-format decision row(s)', fix_delete(path),
            )
    except sqlite3.Error as exc:
        return format_issue(path, f'dispatch database corrupt or older schema ({exc})', fix_delete(path))
    finally:
        conn.close()
    return None


def check_activity_file(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        return format_issue(path, f'usage activity file unreadable or invalid JSON ({exc})', fix_delete(path))
    if not isinstance(doc, dict) or doc.get('schema') != 'agent-compass.activity.v1':
        return format_issue(
            path,
            f'older usage activity schema {(doc.get("schema") if isinstance(doc, dict) else type(doc).__name__)!r} '
            '(expected "agent-compass.activity.v1")',
            fix_delete(path),
        )
    return None


def check_provider_inventory(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        return format_issue(path, f'provider inventory cache invalid JSON ({exc})', fix_delete(path))
    if not isinstance(doc, dict):
        return format_issue(path, 'provider inventory cache must be a JSON object', fix_delete(path))
    if 'schema' in doc and doc.get('schema') != 'agent-compass.providers.v2.local':
        return format_issue(
            path,
            f'older provider inventory schema {doc.get("schema")!r} (expected "agent-compass.providers.v2.local")',
            fix_delete(path),
        )
    return None


def check_router_overrides(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        return format_issue(path, f'router overrides invalid JSON ({exc})', fix_delete(path))
    if not isinstance(doc, dict):
        return format_issue(path, 'router overrides must be a JSON object', fix_delete(path))
    rows = doc.get('overrides')
    if rows is not None and not isinstance(rows, list):
        return format_issue(path, 'router overrides.overrides must be a JSON array', fix_delete(path))
    for item in rows or ():
        if not isinstance(item, dict):
            return format_issue(path, 'router override entry must be a JSON object', fix_delete(path))
        if item.get('stop_on_reset') and isinstance(item.get('baseline_windows'), dict):
            for wid, base in item['baseline_windows'].items():
                if isinstance(base, dict) and not base.get('fetched_at'):
                    return format_issue(
                        path,
                        f'older untimestamped baseline_windows[{wid!r}] in router-overrides',
                        fix_delete(path),
                    )
    return None


def check_json_cache(path: Path, label: str) -> str | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        return format_issue(path, f'{label} invalid JSON ({exc})', fix_delete(path))
    if not isinstance(doc, dict):
        return format_issue(path, f'{label} must be a JSON object', fix_delete(path))
    return None


def _check_global_rule_text(path: Path | str, text: str) -> str | None:
    if RULE_BEGIN in text and RULE_END in text:
        start = text.index(RULE_BEGIN)
        end = text.index(RULE_END, start) + len(RULE_END)
        managed = text[start:end]
        found = [tok for tok in LEGACY_RULE_TOKENS if tok in managed]
        if found:
            return format_issue(
                path,
                f'managed global rule block contains older token(s): {", ".join(found)}',
                FIX_SETUP,
            )
        return None
    stripped = text.strip()
    if stripped.startswith('# Agent Routing (Global)') or '@@ROUTE_JUDGE@@' in text or 'route-agent-work' in text:
        return format_issue(
            path,
            'older un-markered global routing rule block',
            FIX_SETUP,
        )
    return None


def check_hooks_and_rules(home: Path, environ: dict | None = None) -> list[str]:
    issues: list[str] = []
    hook_files = [
        home / '.claude' / 'settings.json',
        home / '.codex' / 'hooks.json',
        home / '.gemini' / 'config' / 'hooks.json',
        home / '.pi' / 'agent' / 'extensions' / 'router-gate.ts',
        home / '.config' / 'opencode' / 'plugins' / 'router-gate.js',
    ]
    for pattern, filename in (('.claude-*', 'settings.json'), ('.codex-*', 'hooks.json')):
        try:
            dirs = sorted(p for p in home.glob(pattern) if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            hook_files.append(d / filename)

    bundled = None
    try:
        from agentcompass import install
        bundled = install.bundled_cli(environ)
    except Exception:
        bundled = None

    for hpath in hook_files:
        if not hpath.is_file():
            continue
        try:
            text = hpath.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            issues.append(format_issue(hpath, f'hook configuration unreadable ({exc})', FIX_SETUP))
            continue
        norm = text.replace('\\\\', '/').replace('\\', '/')
        bad_tokens = [tok for tok in LEGACY_HOOK_TOKENS if tok in norm]
        if bad_tokens:
            issues.append(format_issue(
                hpath, f'installed hook references older path/variable ({", ".join(bad_tokens)})', FIX_SETUP,
            ))
            continue
        if ('router_gate_hook.py' in norm or 'gate-hook' in norm) and bundled is not None:
            if str(bundled).replace('\\', '/').lower() not in norm.lower():
                issues.append(format_issue(
                    hpath, 'installed hook does not use the bundled agentcompass.exe', FIX_SETUP,
                ))

    rule_files = [
        home / '.claude' / 'CLAUDE.md',
        home / '.codex' / 'AGENTS.md',
        home / '.gemini' / 'GEMINI.md',
        home / '.gemini' / 'config' / 'GEMINI.md',
        home / '.pi' / 'agent' / 'AGENTS.md',
        home / '.config' / 'opencode' / 'AGENTS.md',
    ]
    for pattern, filename in (('.claude-*', 'CLAUDE.md'), ('.codex-*', 'AGENTS.md')):
        try:
            dirs = sorted(p for p in home.glob(pattern) if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            rule_files.append(d / filename)

    for rpath in rule_files:
        if not rpath.is_file():
            continue
        try:
            text = rpath.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        issue = _check_global_rule_text(rpath, text)
        if issue:
            issues.append(issue)

    env = _env(environ)
    paseo_home = Path(env.get('PASEO_HOME') or (home / '.paseo'))
    paseo_cfg = paseo_home / 'config.json'
    if paseo_cfg.is_file():
        try:
            pdoc = json.loads(paseo_cfg.read_text(encoding='utf-8-sig'))
            prompt = ((pdoc.get('daemon') or {}).get('appendSystemPrompt')) if isinstance(pdoc, dict) else None
            if isinstance(prompt, str) and prompt:
                issue = _check_global_rule_text(paseo_cfg, prompt)
                if issue:
                    issues.append(issue)
        except (OSError, UnicodeError, ValueError):
            pass

    skill_roots = [
        home / '.claude' / 'skills',
        home / '.codex' / 'skills',
        home / '.agents' / 'skills',
        home / '.gemini' / 'skills',
    ]
    for pattern in ('.claude-*', '.codex-*'):
        try:
            dirs = sorted(p for p in home.glob(pattern) if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:
            skill_roots.append(d / 'skills')
    for sroot in skill_roots:
        old_skill = sroot / 'route-agent-work'
        if old_skill.exists() or old_skill.is_symlink():
            issues.append(format_issue(
                old_skill, 'older skill link route-agent-work (renamed to agent-compass)', FIX_SETUP,
            ))
        cur_skill = sroot / 'agent-compass'
        try:
            if cur_skill.exists() or cur_skill.is_symlink():
                resolved = str(cur_skill.resolve()).replace('\\', '/')
                if 'cli/_internal/agentcompass/data/skills' in resolved or not (cur_skill / 'SKILL.md').is_file():
                    issues.append(format_issue(
                        cur_skill, 'skill link points to an older or missing install layout', FIX_SETUP,
                    ))
        except OSError:
            issues.append(format_issue(cur_skill, 'broken skill link', FIX_SETUP))

    return issues


def collect_issues(home: Path | None = None, environ: dict | None = None) -> list[str]:
    """Return per-file upgrade instructions for any old-shape local state. Never writes."""
    env = _env(environ)
    base_home = Path(home) if home is not None else Path.home()
    sdir = state_dir(base_home, env)
    issues: list[str] = []

    override_env = str(env.get('AGENTCOMPASS_ACCOUNTS_OVERRIDE') or '').strip()
    accounts_path = Path(os.path.expandvars(override_env)).expanduser() if override_env else sdir / 'accounts-override.json'
    policy_env = str(env.get('ROUTER_HOST_POLICY') or '').strip()
    policy_path = Path(os.path.expandvars(policy_env)).expanduser() if policy_env else sdir / 'host-policy.json'
    ledger_env = str(env.get('ROUTER_LEDGER') or '').strip()
    ledger_path = Path(os.path.expandvars(ledger_env)).expanduser() if ledger_env else sdir / 'router-ledger.jsonl'
    db_env = str(env.get('ROUTER_DISPATCH_DB') or '').strip()
    db_path = Path(os.path.expandvars(db_env)).expanduser() if db_env else sdir / 'dispatch.sqlite3'
    activity_env = str(env.get('AGENTCOMPASS_ACTIVITY_FILE') or '').strip()
    activity_path = Path(os.path.expandvars(activity_env)).expanduser() if activity_env else sdir / 'usage-activity.json'
    overrides_env = str(env.get('ROUTER_OVERRIDES') or '').strip()
    overrides_path = Path(os.path.expandvars(overrides_env)).expanduser() if overrides_env else sdir / 'router-overrides.json'
    cache_env = str(env.get('ROUTER_USAGE_CACHE') or '').strip()
    cache_path = Path(os.path.expandvars(cache_env)).expanduser() if cache_env else sdir / 'usage-cache.json'

    for issue in (
        check_accounts_override(accounts_path),
        check_host_policy(policy_path),
        check_user_scores(sdir / 'user-scores.json'),
        check_ledger(ledger_path),
        check_dispatch_db(db_path),
        check_activity_file(activity_path),
        check_provider_inventory(sdir / 'provider-inventory.json'),
        check_router_overrides(overrides_path),
        check_json_cache(cache_path, 'usage cache'),
        check_json_cache(sdir / 'live-models.json', 'live-models cache'),
        check_json_cache(sdir / 'model-latch.json', 'model-latch cache'),
        check_json_cache(sdir / 'provider-retry.json', 'provider-retry cache'),
        check_json_cache(sdir / 'model-catalog.json', 'model-catalog cache'),
    ):
        if issue:
            issues.append(issue)

    rules_dir = sdir / 'host-rules'
    if rules_dir.is_dir():
        try:
            rule_files = sorted(rules_dir.glob('*.md'))
        except OSError:
            rule_files = []
        for rfile in rule_files:
            issue = check_host_rule_file(rfile)
            if issue:
                issues.append(issue)

    legacy_uf_off = sdir / 'usage-float-autostart-disabled'
    if legacy_uf_off.exists():
        issues.append(format_issue(
            legacy_uf_off, 'older usage-float autostart marker (replaced by plugins.json)', FIX_TRAY,
        ))

    legacy_uf_cache = base_home / '.usage-float' / 'usage-cache.json'
    if not cache_env and legacy_uf_cache.exists():
        issues.append(format_issue(
            legacy_uf_cache,
            'older Usage Float cache location (AgentCompass reads only usage-cache.json under state dir)',
            fix_delete(legacy_uf_cache),
        ))

    cal_dir = base_home / '.cache' / 'agentcompass-calibration'
    if cal_dir.is_dir() and not (cal_dir / 'fetch-manifest.json').is_file():
        try:
            has_entries = any(cal_dir.iterdir())
        except OSError:
            has_entries = False
        if has_entries:
            issues.append(format_issue(
                cal_dir,
                'older calibration cache missing fetch-manifest.json',
                fix_delete(cal_dir),
            ))

    issues.extend(check_hooks_and_rules(base_home, env))
    return issues
