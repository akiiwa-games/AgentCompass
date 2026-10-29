"""Register an already signed-in isolated Claude account (any name, any home). Never copies tokens.

Close the hosted Usage Float UI first (shutdown saves its old settings).
Run: python setup/claude2_config.py [--account claude-work] [--home <dir>] [--label "Claude Work"]
The account defaults to claude-2 with its home at ~/.<account>.
Then reload Paseo (no daemon restart) and start the hosted Usage Float UI.
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import uuid


DEFAULT_ACCOUNT = 'claude-2'


def usage_rows(account: str) -> list[str]:
    return [f'{account}:7d', f'{account}:5h']


SECOND_ROWS = usage_rows(DEFAULT_ACCOUNT)


def validate_account(account: str) -> str:
    if not re.fullmatch(r'[a-z][a-z0-9-]*', account or ''):
        raise ValueError('Account names use lowercase letters, digits and hyphens, starting with a letter')
    return account


def default_label(account: str) -> str:
    return 'Claude ' + account.removeprefix('claude-').replace('-', ' ').title()


def provider_config(home: Path, account: str = DEFAULT_ACCOUNT, account_home: Path | None = None,
                    label: str | None = None) -> dict:
    account_home = account_home or home / f'.{account}'
    return {
        'extends': 'claude', 'label': label or default_label(account),
        'description': 'Additional Claude subscription; isolated config and sessions',
        'enabled': True,
        'env': {
            'CLAUDE_CONFIG_DIR': str(account_home),
            # Empty is NOT unset: Claude Code reads the default account's
            # secure storage in that case, even while profile identity is
            # read from CLAUDE_CONFIG_DIR. Keep both locations identical.
            'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(account_home),
            'CLAUDE_CODE_OAUTH_TOKEN': '',
            'ANTHROPIC_API_KEY': '', 'ANTHROPIC_AUTH_TOKEN': '',
            'ANTHROPIC_BASE_URL': '',
        },
    }


def float_config(config: dict, rows: list[str] = SECOND_ROWS) -> dict:
    """Keep all existing visibility choices; enable only the account's two new rows."""
    for key in ('providers', 'provider_order'):
        values = config.setdefault(key, [])
        for row in rows:
            if row not in values:
                values.append(row)
    for key in ('disabled_providers',):
        if isinstance(config.get(key), list):
            config[key] = [row for row in config[key] if row not in rows]
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


def sync_shared_claude_assets(home: Path, account_home: Path | None = None) -> None:
    """Ensure the account home shares CLAUDE.md, settings.json, and skills with ~/.claude."""
    primary = home / '.claude'
    account = account_home or home / f'.{DEFAULT_ACCOUNT}'
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


def launch_settings(spec: dict) -> dict:
    """What decides how the account launches; label and description are display text."""
    return {key: spec.get(key) for key in ('extends', 'enabled', 'env')} if isinstance(spec, dict) else {}


def configure(home: Path, account: str = DEFAULT_ACCOUNT, account_home: Path | None = None,
              label: str | None = None) -> None:
    account = validate_account(account)
    account_home = Path(account_home).expanduser() if account_home else home / f'.{account}'
    if not (account_home / '.credentials.json').is_file():
        raise RuntimeError(f'Sign in with CLAUDE_CONFIG_DIR pointing to {account_home} first')
    sync_shared_claude_assets(home, account_home)
    paseo = home / '.paseo' / 'config.json'
    floating = home / '.usage-float' / 'config.json'
    sources = home / '.route-manager' / 'capacity-sources.json'
    p = load(paseo)
    providers = p.setdefault('agents', {}).setdefault('providers', {})
    expected = provider_config(home, account, account_home, label)
    legacy = provider_config(home, account, account_home, label)
    legacy['env']['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = ''
    current = providers.get(account)
    if current is not None and launch_settings(current) not in (launch_settings(expected), launch_settings(legacy)):
        raise RuntimeError(f'{account} already has a different configuration; review it first')
    if isinstance(current, dict) and not label:
        # Keep the display text the user already has.
        expected.update({k: current[k] for k in ('label', 'description') if k in current})
    providers[account] = expected
    f = float_config(load(floating), usage_rows(account))
    s = load(sources)
    s[account] = {'kind': 'claude', 'account': account, 'roots': [str(account_home / 'projects')]}
    backups = home / '.route-manager' / 'backups' / (f'{account}-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    for path, data in ((paseo, p), (floating, f), (sources, s)):
        save_config(path, data, backups)
        print(f'Configured: {path}')
    print(f'Previous configs backed up under: {backups}')


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--account', default=DEFAULT_ACCOUNT, help='Paseo provider / router account name (for example team-backup)')
    parser.add_argument('--home', type=Path, help='The signed-in CLAUDE_CONFIG_DIR (default ~/.<account>)')
    parser.add_argument('--label', help='Display name (default derived from the account name)')
    args = parser.parse_args(argv)
    configure(Path.home(), args.account, args.home, args.label)


if __name__ == '__main__':
    main()
