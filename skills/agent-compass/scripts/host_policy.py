"""Local dispatch availability. No profile preserves the shared routing defaults."""
import argparse
import json
import os
from pathlib import Path
from datetime import datetime, timezone

import account_state
import accounts_override
import override_store
import state_upgrade


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
    return (Path(configured).expanduser(), True) if configured else (Path.home() / '.agent-compass/host-policy.json', False)


def load_policy():
    path, explicit = policy_path()
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        if not explicit:
            return None
        raise HostPolicyError(state_upgrade.format_issue(
            path, 'host-policy configuration missing', state_upgrade.FIX_TRAY,
        )) from None
    except UnicodeError:
        raise HostPolicyError(state_upgrade.format_issue(
            path, 'host-policy configuration must be UTF-8', state_upgrade.FIX_TRAY,
        )) from None
    except OSError as exc:
        raise HostPolicyError(state_upgrade.format_issue(
            path, f'host-policy configuration unreadable ({exc})', state_upgrade.FIX_TRAY,
        )) from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HostPolicyError(state_upgrade.format_issue(
            path, 'host-policy configuration invalid JSON', state_upgrade.FIX_TRAY,
        )) from None
    try:
        return state_upgrade.validate_host_policy_doc(data, path)
    except ValueError as exc:
        raise HostPolicyError(str(exc)) from None


def sync_discovered(accounts):
    """Admit only unseen, ready registered accounts; a known omission is an intentional deny."""
    policy = load_policy()
    if policy is None:
        return {'mode': 'default', 'added': []}
    if 'known_accounts' not in policy:
        return {'mode': 'static', 'added': []}
    discovery = accounts.get('_discovery') or {}
    rows = discovery.get('accounts') or {}
    specs = accounts.get('accounts') or {}
    now = datetime.now(timezone.utc)
    ready = []
    for name, row in rows.items():
        spec = specs.get(name) or {}
        # Only subscription accounts enroll automatically; metered/unknown billing needs an explicit allow.
        if not account_state.dispatchable(row) or spec.get('verified') is not True or accounts_override.manual_only_billing(name, accounts):
            continue
        if not any(not b.get('disabled') and not b.get('discovery_unavailable')
                   for b in (spec.get('bundles') or {}).values()):
            continue
        expires = spec.get('expires_at')
        if expires:
            try:
                moment = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                if (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)) <= now:
                    continue
            except (AttributeError, TypeError, ValueError):
                continue
        ready.append(name)
    seen_fast = set(policy['known_accounts']) | set(policy['allowed_accounts'])
    if not any(name not in seen_fast for name in ready):
        return {'mode': 'enabled', 'added': []}
    path = policy_path()[0]
    with override_store.locked(path):
        policy = load_policy()
        if policy is None or 'known_accounts' not in policy:
            return {'mode': 'default' if policy is None else 'static', 'added': []}
        seen = set(policy['known_accounts']) | set(policy['allowed_accounts'])
        added = [name for name in ready if name not in seen]
        if added:
            policy['allowed_accounts'].extend(added)
            policy['known_accounts'] = list(dict.fromkeys([*policy['known_accounts'], *policy['allowed_accounts']]))
            override_store.write(path, policy)
        return {'mode': 'enabled', 'added': added}


def account_allowed(policy, account):
    return policy is None or account in policy['allowed_accounts']


def disabled_cause(account, candidates, accounts, policy):
    """(kind, detail) when the account, or every bundle the target can be, is switched off on this host, else None.
    Kinds: `legacy` (the account's leftover meter text), `host_policy` (account not in the policy's allowed accounts),
    `bundles` (ids all unchecked in model management). `candidates` are the account's matching bundle rows."""
    legacy = ((accounts or {}).get('accounts') or {}).get(account, {}).get('legacy_meter')
    if isinstance(legacy, str) and legacy.strip():
        return 'legacy', legacy
    if policy is not None and not account_allowed(policy, account):
        return 'host_policy', account
    if candidates and all(c.get('disabled') for c in candidates):
        return 'bundles', [c['bundle_id'] for c in candidates]
    return None


def disabled_text(account, cause, language):
    """The one wording of a `disabled_cause`, in `en` (the verdict for a parent) or `zh` (the spawn gate's reply)."""
    kind, detail = cause
    if kind == 'legacy':
        return detail
    if language == 'zh':
        return (f"账号 {account} 已在本机禁用（不在 host-policy.json 的 allowed_accounts 中）" if kind == 'host_policy'
                else "档位 " + ", ".join(detail) + " 已在模型管理中停用")
    return (f'account {account} is disabled on this host by its host policy' if kind == 'host_policy'
            else f'bundle {", ".join(detail)} is unchecked in model management')


def judge_allowed(policy, name, account):
    return account_allowed(policy, account) and (policy is None or 'judges' not in policy or name in policy['judges'])


def usage_providers(policy, defaults):
    if policy is None or 'usage_providers' not in policy:
        return list(defaults)
    allowed = set(policy['usage_providers'])
    return [name for name in defaults if name in allowed]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    try:
        allowed = account_allowed(load_policy(), args.account)
    except HostPolicyError as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps({'account': args.account, 'allowed': allowed}))
    return 0 if allowed else 1


if __name__ == '__main__':
    raise SystemExit(main())
