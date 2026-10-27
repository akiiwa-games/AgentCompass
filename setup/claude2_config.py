"""Register an already signed-in isolated Claude account. Never copies tokens.

Close the hosted Usage Float UI first (shutdown saves its old settings).
Run: python setup/claude2_config.py
Then reload Paseo (no daemon restart) and start the hosted Usage Float UI.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import shutil
import uuid


SECOND_ROWS = ['claude-2:7d', 'claude-2:5h']


def provider_config(home: Path) -> dict:
    return {
        'extends': 'claude', 'label': 'Claude 2',
        'description': 'Second Claude subscription; isolated config and sessions',
        'enabled': True,
        'env': {
            'CLAUDE_CONFIG_DIR': str(home / '.claude-2'),
            # Empty is NOT unset: Claude Code reads the default account's
            # secure storage in that case, even while profile identity is
            # read from CLAUDE_CONFIG_DIR. Keep both locations identical.
            'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(home / '.claude-2'),
            'CLAUDE_CODE_OAUTH_TOKEN': '',
            'ANTHROPIC_API_KEY': '', 'ANTHROPIC_AUTH_TOKEN': '',
            'ANTHROPIC_BASE_URL': '',
        },
    }


def float_config(config: dict) -> dict:
    """Keep all existing visibility choices; enable only the two new rows."""
    for key in ('providers', 'provider_order'):
        values = config.setdefault(key, [])
        for row in SECOND_ROWS:
            if row not in values:
                values.append(row)
    for key in ('disabled_providers',):
        if isinstance(config.get(key), list):
            config[key] = [row for row in config[key] if row not in SECOND_ROWS]
    return config


def save_config(path: Path, data: dict, backups: Path) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    if path.exists() and path.read_text(encoding='utf-8-sig') == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups / f'{path.parent.name}-{path.name}')
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError(f'Expected object: {path}')
    return data


def sync_shared_claude_assets(home: Path) -> None:
    """Ensure ~/.claude-2 shares CLAUDE.md, settings.json, and skills with ~/.claude."""
    primary = home / '.claude'
    account = home / '.claude-2'
    if not primary.is_dir() or not account.is_dir():
        return
    src_md = primary / 'CLAUDE.md'
    if src_md.is_file():
        shutil.copy2(src_md, account / 'CLAUDE.md')
    src_settings = primary / 'settings.json'
    dst_settings = account / 'settings.json'
    if src_settings.is_file():
        base = load(src_settings)
        existing = load(dst_settings)
        merged_env = dict(base.get('env') or {})
        for k, v in (existing.get('env') or {}).items():
            if v is not None and v != '':
                merged_env[k] = v
        if merged_env:
            base['env'] = merged_env
        dst_settings.write_text(json.dumps(base, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    skills_src = primary / 'skills'
    skills_dst = account / 'skills'
    if skills_src.is_dir() and not skills_dst.exists():
        import subprocess
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(skills_dst), str(skills_src)], check=False, capture_output=True)


def configure(home: Path) -> None:
    account = home / '.claude-2'
    if not (account / '.credentials.json').is_file():
        raise RuntimeError('Sign in with CLAUDE_CONFIG_DIR pointing to ~/.claude-2 first')
    sync_shared_claude_assets(home)
    paseo = home / '.paseo' / 'config.json'
    floating = home / '.usage-float' / 'config.json'
    sources = home / '.route-manager' / 'capacity-sources.json'
    p = load(paseo)
    providers = p.setdefault('agents', {}).setdefault('providers', {})
    expected = provider_config(home)
    legacy = provider_config(home)
    legacy['env']['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = ''
    if 'claude-2' in providers and providers['claude-2'] not in (expected, legacy):
        raise RuntimeError('claude-2 already has a different configuration; review it first')
    providers['claude-2'] = expected
    f = float_config(load(floating))
    s = load(sources)
    s['claude-2'] = {'kind': 'claude', 'account': 'claude-2', 'roots': [str(account / 'projects')]}
    backups = home / '.route-manager' / 'backups' / ('claude-2-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    for path, data in ((paseo, p), (floating, f), (sources, s)):
        save_config(path, data, backups)
        print(f'Configured: {path}')
    print(f'Previous configs backed up under: {backups}')


if __name__ == '__main__':
    configure(Path.home())
