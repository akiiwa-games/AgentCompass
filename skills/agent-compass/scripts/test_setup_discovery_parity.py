"""Parity between the homes the router dispatches to and the homes `sync_host_globals` writes to.

Only synthetic fake homes and the shipped template are read; the real user profile is never touched.
"""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import accounts_override
import account_fleet
import provider_inventory as inventory
from router_test_support import isolated_router_state, temporary_root


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, discovery=False, policy=False)


def shipped_template():
    return accounts_override.normalize(json.loads((account_fleet.REFERENCES / 'accounts.json').read_text(encoding='utf-8-sig')))


class SetupAndRouterHomeParityTests(unittest.TestCase):
    """Prove that the set of homes the router dispatches to equals the set `sync_host_globals` writes to,
    with all exceptions explicitly documented in this test.
    """

    # Families the router can dispatch to that have no dedicated local global instruction file managed by setup:
    # - dot / chatgpt-web / grok-bot: remote/bridge services without local harness instruction files;
    # - grok: loads ~/.claude/CLAUDE.md, skills and hooks via `[compat.claude] = true` by default;
    # - omp: receives Paseo's `daemon.appendSystemPrompt` via `--append-system-prompt`;
    # - copilot: Paseo-recognized-only ACP slot without local discovery.
    NON_HOME_SYNC_FAMILIES = frozenset({'dot', 'chatgpt-web', 'grok-bot', 'grok', 'omp', 'copilot'})

    def setUp(self):
        setup_dir = str(Path(__file__).resolve().parents[3] / 'setup')
        if setup_dir not in sys.path:
            sys.path.insert(0, setup_dir)
        import sync_host_globals as sg
        self.sg = sg
        self.home = temporary_root(self.addCleanup).resolve()
        self.now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.enterContext(patch.object(sg, 'HOME', self.home))
        sg.set_home(self.home)
        self.addCleanup(sg.set_home, None)
        self.enterContext(patch.object(sg, 'ensure_paseo_bridge_dependencies'))

    def write(self, rel, value):
        target = self.home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value)
        target.write_text(text, encoding='utf-8', newline='\n')
        return target

    def test_router_dispatch_homes_equal_sync_written_homes_with_documented_exceptions(self):
        claude_cred = {'claudeAiOauth': {'accessToken': 'SECRET-claude', 'subscriptionType': 'pro'}}
        codex_cred = {'tokens': {'account_id': 'acct-codex', 'access_token': 'SECRET-codex'}}

        # 1. Routable local homes: primary claude, primary codex, Paseo extends-chain custom claude, signed-in Orca, pi, opencode
        self.write('.claude/.credentials.json', claude_cred)
        self.write('.codex/auth.json', codex_cred)
        custom_claude = self.home / 'custom-homes' / 'team-anthropic'
        self.write('custom-homes/team-anthropic/.credentials.json', claude_cred)
        orca_ready = self.home / 'AppData/Roaming/orca/codex-accounts/acct-ready/home'
        self.write('AppData/Roaming/orca/codex-accounts/acct-ready/home/config.toml', "model = 'gpt-5'\n")
        self.write('AppData/Roaming/orca/codex-accounts/acct-ready/home/auth.json',
                   {'tokens': {'account_id': 'acct-orca-ready', 'access_token': 'SECRET-orca'}})
        self.write('.pi/agent/auth.json', {'openai-codex': {
            'type': 'oauth', 'access': 'SECRET-pi', 'refresh': 'r', 'accountId': 'acct-pi'}})

        # 2. Manual-launch on-disk homes (not auto-routable by the router, but synced so manual launches get rules/skills/hooks):
        #    - ~/.claude-saved with no credentials and no Paseo launcher
        #    - disabled Paseo codex account with an existing custom CODEX_HOME
        #    - Orca account home without auth.json
        #    - agy installed on PATH, ~/.gemini exists, but no usage-cache/tier sign-in proof
        claude_saved = self.home / '.claude-saved'
        claude_saved.mkdir(parents=True)
        codex_disabled = self.home / 'custom-homes' / 'codex-disabled'
        codex_disabled.mkdir(parents=True)
        orca_unsigned = self.home / 'AppData/Roaming/orca/codex-accounts/acct-unsigned/home'
        self.write('AppData/Roaming/orca/codex-accounts/acct-unsigned/home/config.toml', "model = 'gpt-5'\n")
        (self.home / '.gemini').mkdir(parents=True)

        # 3. Excluded consult homes (must be excluded from both router worker discovery and sync)
        (self.home / '.codex-chatgpt-web-consult').mkdir(parents=True)
        (self.home / '.codex-pro-consult').mkdir(parents=True)

        # 4. Non-home / remote / proxy accounts (routable, but carry no local harness instruction home)
        self.write('.dot2api/client.json', {'base_url': 'http://127.0.0.1:8788', 'api_key': 'SECRET-dot'})
        self.write('.codex-chatgpt-web-consult/config.json', {
            'mode': 'browser-only', 'browserInteractionMode': 'automatic',
            'host': '127.0.0.1', 'port': 8790, 'proAvailable': True})
        self.write('.agent-compass/accounts-override.json', {
            'accounts': {
                'team-proxy': {
                    'backend_kind': 'litellm',
                    'endpoint': 'https://proxy.example.test',
                    'api_key': 'sk-proxy',
                    'models': ['gpt-6.1-sol'],
                }
            }
        })
        self.write('.paseo/config.json', {
            'agents': {
                'providers': {
                    'claude-mid': {'extends': 'claude'},
                    'team-anthropic': {'extends': 'claude-mid', 'env': {'CLAUDE_CONFIG_DIR': str(custom_claude)}},
                    'codex-disabled': {'extends': 'codex', 'enabled': False, 'env': {'CODEX_HOME': str(codex_disabled)}},
                    'grok': {'extends': 'acp', 'enabled': True},
                }
            }
        })

        cli_map = {'claude': 'claude', 'codex': 'codex', 'pi': 'pi', 'agy': 'agy', 'opencode': 'opencode', 'grok': 'grok'}
        with patch.object(self.sg.shutil, 'which', side_effect=lambda name, path=None: cli_map.get(name)):
            env = self.sg._sync_environ(self.home)
            inv = inventory.discover(home=self.home, environ=env, now=self.now)
            applied = inventory.apply(shipped_template(), inv)
            with contextlib.redirect_stdout(io.StringIO()):
                self.sg.sync(hooks=True, skills=False)

        manifest = self.sg.load_manifest()
        written_doc_homes = {
            (self.home / '.gemini').resolve() if (self.home / '.gemini').resolve() in Path(k).resolve().parents
            else Path(k).resolve().parent
            for k in manifest['docs']
        }
        sync_homes = {h.resolve() for _, h in self.sg.discovered_homes(inv, self.home)}
        self.assertEqual(written_doc_homes, sync_homes)

        # Partition discovered accounts into routable local homes, non-home routable accounts, and manual-launch homes.
        routable_providers = {
            b.get('provider') or acct_name
            for acct_name, spec in applied['accounts'].items()
            if spec.get('verified')
            for b in (spec.get('bundles') or {}).values()
            if not b.get('discovery_unavailable')
        } | {
            acct_name
            for acct_name, spec in applied['accounts'].items()
            if spec.get('verified') and any(not b.get('discovery_unavailable') for b in (spec.get('bundles') or {}).values())
        }
        router_routable_homes = set()
        manual_only_sync_homes = set()
        for name, row in inv['accounts'].items():
            fam = row.get('family')
            if fam in self.NON_HOME_SYNC_FAMILIES or row.get('backend_kind') == 'litellm':
                self.assertNotIn('home', row)
                continue
            home_path = Path(row['home']).resolve()
            is_routable = name in routable_providers
            if is_routable:
                router_routable_homes.add(home_path)
            elif self.sg.account_home_eligible(row, self.home):
                # Documented exception: existing on-disk or installed-CLI home that is signed out, saved, disabled,
                # or has unverified sign-in (agy) still gets rules/hooks for manual launches.
                self.assertTrue(
                    row.get('status') in ('disabled', 'paused', 'saved', 'missing_credentials')
                    or not inventory.launch_verified(row, False),
                    f'Unexpected non-routable synced account {name}: {row}',
                )
                manual_only_sync_homes.add(home_path)

        expected_routable_homes = {
            (self.home / '.claude').resolve(),
            custom_claude.resolve(),
            (self.home / '.codex').resolve(),
            orca_ready.resolve(),
            (self.home / '.pi' / 'agent').resolve(),
            (self.home / '.config' / 'opencode').resolve(),
        }
        expected_manual_homes = {
            claude_saved.resolve(),
            codex_disabled.resolve(),
            orca_unsigned.resolve(),
            (self.home / '.gemini').resolve(),
        }
        self.assertEqual(router_routable_homes, expected_routable_homes)
        self.assertEqual(manual_only_sync_homes, expected_manual_homes)
        self.assertEqual(sync_homes, router_routable_homes | manual_only_sync_homes)
        for consult in ('.codex-chatgpt-web-consult', '.codex-pro-consult'):
            self.assertNotIn((self.home / consult).resolve(), sync_homes)


if __name__ == '__main__':
    unittest.main()
