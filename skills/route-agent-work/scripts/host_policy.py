"""Local dispatch availability. No profile preserves the shared routing defaults."""
import argparse
import json
import os
from pathlib import Path


class HostPolicyError(ValueError):
    """Safe, local configuration diagnostic (never includes profile contents)."""


def policy_path():
    configured = os.environ.get('ROUTER_HOST_POLICY')
    if not configured and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                configured = winreg.QueryValueEx(key, 'ROUTER_HOST_POLICY')[0]
        except OSError:
            pass
    if configured and not isinstance(configured, str):
        raise HostPolicyError('ROUTER_HOST_POLICY must be a path string.')
    return (Path(configured).expanduser(), True) if configured else (Path.home()/'.route-manager/host-policy.json', False)


def load_policy():
    path, explicit = policy_path()
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        if not explicit:
            return None
        raise HostPolicyError(f'Host policy configuration missing: {path}') from None
    except OSError:
        raise HostPolicyError(f'Host policy configuration unreadable: {path}') from None
    except UnicodeError:
        raise HostPolicyError(f'Host policy configuration must be UTF-8: {path}') from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HostPolicyError(f'Host policy configuration invalid JSON: {path}') from None
    if not isinstance(data, dict) or 'allowed_accounts' not in data:
        raise HostPolicyError(f'Host policy configuration requires allowed_accounts: {path}')
    if set(data) - {'allowed_accounts', 'judges', 'usage_providers'}:
        raise HostPolicyError(f'Host policy configuration has unknown fields: {path}')
    for key, value in data.items():
        if not isinstance(value, list) or any(not isinstance(v, str) or not v or v != v.strip() for v in value):
            raise HostPolicyError(f'Host policy configuration {key} must be a list of nonempty names: {path}')
    if any(v not in ('codex', 'sonnet') for v in data.get('judges', [])):
        raise HostPolicyError(f'Host policy configuration judges must name codex or sonnet: {path}')
    return data


def account_allowed(policy, account):
    return policy is None or account in policy['allowed_accounts']


def judge_allowed(policy, name, account):
    return account_allowed(policy, account) and (policy is None or 'judges' not in policy or name in policy['judges'])


def usage_providers(policy, defaults):
    return [name for name in defaults if account_allowed(policy, name)
            and (policy is None or 'usage_providers' not in policy or name in policy['usage_providers'])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    try:
        allowed = account_allowed(load_policy(), args.account)
    except HostPolicyError as error:
        parser.exit(2, str(error)+'\n')
    print(json.dumps({'account': args.account, 'allowed': allowed}))
    return 0 if allowed else 1


if __name__ == '__main__':
    raise SystemExit(main())
