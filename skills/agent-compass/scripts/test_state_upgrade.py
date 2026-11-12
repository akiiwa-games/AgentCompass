"""Tests for the read-only upgrade contract (`agentcompass doctor` + fail-fast runtime loaders)."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = Path(__file__).resolve().parent
SETUP_DIR = REPO_ROOT / 'setup'
TRAY_DIR = REPO_ROOT / 'tray'
for _entry in (str(REPO_ROOT), str(SCRIPTS_DIR), str(SETUP_DIR), str(TRAY_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from agentcompass import doctor  # noqa: E402
import account_fleet  # noqa: E402
import dispatch_registry  # noqa: E402
import host_policy  # noqa: E402
import host_rules  # noqa: E402
import quota_overrides  # noqa: E402
import router_data  # noqa: E402
import state_upgrade  # noqa: E402
import user_scores  # noqa: E402
from router_test_support import isolated_router_state, profile  # noqa: E402


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).digest()
        for p in sorted(root.rglob('*'))
        if p.is_file()
    }


class StateUpgradeContractTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_router_state(self.addCleanup, policy=False)
        self.home = self.root / 'user-home'
        self.state = self.home / '.agent-compass'
        self.state.mkdir(parents=True, exist_ok=True)
        self.now = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)

    def _env(self) -> dict[str, str]:
        return {
            'HOME': str(self.home),
            'USERPROFILE': str(self.home),
            'CODEX_HOME': str(REPO_ROOT),
            'ROUTER_PROVIDER_HOME': str(self.home),
            'ROUTER_STATE_DIR': str(self.state),
            'PASEO_HOME': str(self.home / '.paseo'),
            'AGENTCOMPASS_PUBLIC_DATA': 'off',
            'AGENTCOMPASS_ANTIGRAVITY_PLAN': 'off',
            'AGENTCOMPASS_LIVE_MODELS': 'off',
        }

    def test_doctor_detects_every_old_local_state_without_rewriting_any_file(self):
        # 1. accounts-override v1
        (self.state / 'accounts-override.json').write_text(
            json.dumps({'schema_version': 1, 'accounts': {'codex': {'bundles': {'sol_high': {'disabled': 'off'}}}}}),
            encoding='utf-8',
        )
        # 2. host-policy with unsupported legacy field
        (self.state / 'host-policy.json').write_text(
            json.dumps({'allowed_accounts': ['codex'], 'legacy_mode': True}),
            encoding='utf-8',
        )
        # 3. user-scores with legacy `date` field
        (self.state / 'user-scores.json').write_text(
            json.dumps({'schema_version': 1, 'scores': {'grok-bot': {'coding': {'level': 2.0, 'date': '2026-11-01'}}}}),
            encoding='utf-8',
        )
        # 4. host-rules overlay referencing ROUTER_WORKER
        rules_dir = self.state / 'host-rules'
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / 'shared.md').write_text('Check ROUTER_WORKER=1 before delegating.\n', encoding='utf-8')
        # 5. ledger containing pre-four-category profile
        old_prof = {
            **profile(),
            'realtime_loop': True,
            'axis_demand': {'divergence': 1, 'global_rigor': 2, 'module_precision': 2, 'vision_tools': 0},
        }
        (self.state / 'router-ledger.jsonl').write_text(
            json.dumps({'stage': 'plan', 'at': self.now.isoformat(), 'profile': old_prof}) + '\n',
            encoding='utf-8',
        )
        # 6. dispatch.sqlite3 containing old-shape decision profile
        db_file = self.state / 'dispatch.sqlite3'
        conn = sqlite3.connect(db_file)
        conn.execute('CREATE TABLE decisions (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        conn.execute('CREATE TABLE dispatches (id TEXT PRIMARY KEY, decision_id TEXT, data TEXT NOT NULL)')
        conn.execute(
            'INSERT INTO decisions VALUES (?, ?)',
            ('d-old', json.dumps({'decision_id': 'd-old', 'allocation_task': {'objective': 'x', 'profile': old_prof}})),
        )
        conn.commit()
        conn.close()
        # 7. usage-activity with route-manager.activity.v1
        (self.state / 'usage-activity.json').write_text(
            json.dumps({'schema': 'route-manager.activity.v1'}), encoding='utf-8',
        )
        # 8. provider-inventory with older schema
        (self.state / 'provider-inventory.json').write_text(
            json.dumps({'schema': 'route-manager.providers.v1'}), encoding='utf-8',
        )
        # 9. router-overrides with untimestamped baseline_windows
        (self.state / 'router-overrides.json').write_text(
            json.dumps({'overrides': [{'id': 'o1', 'stop_on_reset': True, 'baseline_windows': {'usage': {'used_pct': 50}}}]}),
            encoding='utf-8',
        )
        # 10. old Usage Float cache + autostart marker + calibration cache without fetch-manifest.json
        uf_cache = self.home / '.usage-float' / 'usage-cache.json'
        uf_cache.parent.mkdir(parents=True, exist_ok=True)
        uf_cache.write_text('{"providers":{}}', encoding='utf-8')
        (self.state / 'usage-float-autostart-disabled').write_text('', encoding='utf-8')
        cal_cache = self.home / '.cache' / 'agentcompass-calibration'
        cal_cache.mkdir(parents=True, exist_ok=True)
        (cal_cache / 'stale.parquet').write_bytes(b'old')
        # 11. stale hook + global rule block + route-agent-work skill link
        claude_dir = self.home / '.claude'
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / 'settings.json').write_text('{"hooks": "ROUTER_WORKER"}', encoding='utf-8')
        (claude_dir / 'CLAUDE.md').write_text(
            '<!-- agentcompass:begin -->\nUse @@ROUTE_JUDGE@@ and ROUTER_WORKER=1\n<!-- agentcompass:end -->\n',
            encoding='utf-8',
        )
        old_skill = claude_dir / 'skills' / 'route-agent-work'
        old_skill.mkdir(parents=True, exist_ok=True)

        before = _snapshot_tree(self.home)
        with patch.object(doctor, 'listeners', return_value={}):
            report = doctor.collect(self.home)
            rendered = doctor.render(report)
        after = _snapshot_tree(self.home)
        self.assertEqual(before, after, 'doctor must never rewrite user files')

        issues = report['upgrade_issues']
        self.assertGreaterEqual(len(issues), 14, issues)
        self.assertIn('Upgrade checks:', rendered)
        allowed_fixes = (
            'Fix: re-run "agentcompass setup".',
            'Fix: re-toggle in the tray.',
        )
        for issue in issues:
            self.assertTrue(
                issue.endswith(allowed_fixes) or ('Fix: delete "' in issue and issue.endswith('".')),
                f'Unexpected fix wording in {issue!r}',
            )
        for expected_file in (
            self.state / 'accounts-override.json',
            self.state / 'host-policy.json',
            self.state / 'user-scores.json',
            rules_dir / 'shared.md',
            self.state / 'router-ledger.jsonl',
            db_file,
            self.state / 'usage-activity.json',
            self.state / 'provider-inventory.json',
            self.state / 'router-overrides.json',
            uf_cache,
            self.state / 'usage-float-autostart-disabled',
            cal_cache,
            claude_dir / 'settings.json',
            claude_dir / 'CLAUDE.md',
            old_skill,
        ):
            self.assertTrue(any(str(expected_file) in issue for issue in issues), f'Missing issue for {expected_file}')

    def test_runtime_loaders_fail_fast_with_the_exact_same_message_as_doctor(self):
        env = self._env()
        # 1. accounts-override.json (v1, string disabled, allowed_cwd)
        override_path = self.state / 'accounts-override.json'
        for bad_doc in (
            {'schema_version': 1, 'accounts': {}},
            {'schema_version': 2, 'accounts': {'codex': {'bundles': {'sol_high': {'disabled': 'user off'}}}}},
            {'schema_version': 2, 'accounts': {'codex': {'allowed_cwd': ['E:/P4']}}},
        ):
            override_path.write_text(json.dumps(bad_doc), encoding='utf-8')
            expected = state_upgrade.check_accounts_override(override_path)
            self.assertIsNotNone(expected)
            with patch.dict(os.environ, {**env, 'AGENTCOMPASS_ACCOUNTS_OVERRIDE': str(override_path)}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    account_fleet.load_accounts()
            self.assertEqual(str(ctx.exception), expected)
        override_path.unlink()

        # 2. host-policy.json
        policy_path = self.state / 'host-policy.json'
        policy_path.write_text(json.dumps({'allowed_accounts': ['codex'], 'extra': ['bad']}), encoding='utf-8')
        expected_policy = state_upgrade.check_host_policy(policy_path)
        self.assertIsNotNone(expected_policy)
        with patch.dict(os.environ, {**env, 'ROUTER_HOST_POLICY': str(policy_path)}, clear=True):
            with self.assertRaises(host_policy.HostPolicyError) as ctx:
                host_policy.load_policy()
        self.assertEqual(str(ctx.exception), expected_policy)
        policy_path.unlink()

        # 3. user-scores.json
        scores_path = self.state / 'user-scores.json'
        scores_path.write_text(
            json.dumps({'schema_version': 1, 'scores': {'grok-bot': {'coding': {'level': 2.0, 'date': '2026-11-01'}}}}),
            encoding='utf-8',
        )
        expected_scores = state_upgrade.check_user_scores(scores_path)
        self.assertIsNotNone(expected_scores)
        with self.assertRaises(ValueError) as ctx:
            user_scores.load_local(scores_path)
        self.assertEqual(str(ctx.exception), expected_scores)
        scores_path.unlink()

        # 4. host-rules overlay
        rules_dir = self.state / 'host-rules'
        rules_dir.mkdir(parents=True, exist_ok=True)
        overlay = rules_dir / 'shared.md'
        overlay.write_text('Legacy @@ROUTE_JUDGE@@ placeholder\n', encoding='utf-8')
        expected_rule = state_upgrade.check_host_rule_file(overlay)
        self.assertIsNotNone(expected_rule)
        with self.assertRaises(ValueError) as ctx:
            host_rules.load('shared', self.home)
        self.assertEqual(str(ctx.exception), expected_rule)
        overlay.unlink()

        # 5. router-overrides.json
        overrides_file = self.state / 'router-overrides.json'
        overrides_file.write_text(
            json.dumps({'overrides': [{'id': 'o1', 'account': 'codex', 'until': (self.now + timedelta(hours=2)).isoformat(),
                                       'stop_on_reset': True, 'baseline_windows': {'usage': {'used_pct': 20}}}]}),
            encoding='utf-8',
        )
        expected_ov = state_upgrade.check_router_overrides(overrides_file)
        self.assertIsNotNone(expected_ov)
        with self.assertRaises(ValueError) as ctx:
            quota_overrides.load_overrides(self.now, path=overrides_file)
        self.assertEqual(str(ctx.exception), expected_ov)
        overrides_file.unlink()

        # 6. dispatch.sqlite3 with an older-format profile in decisions
        db_file = self.state / 'dispatch.sqlite3'
        with patch.dict(os.environ, {**env, 'ROUTER_DISPATCH_DB': str(db_file)}, clear=True), \
                patch.object(dispatch_registry.provider_inventory, 'launcher_family', return_value='codex'):
            accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
            accounts['accounts']['codex']['verified'] = True
            bundle = dict(accounts['accounts']['codex']['bundles']['sol_high'], bundle_id='sol_high',
                          account='codex', provider='codex')
            task = {'objective': 'Implement feature', 'task_id': 't1', 'parent_agent_id': 'p1', 'expected_hours': 0.5}
            old_alloc = {
                **task,
                'profile': {
                    **profile(),
                    'realtime_loop': False,
                    'axis_demand': {'divergence': 1, 'global_rigor': 2, 'module_precision': 2, 'vision_tools': 0},
                },
            }
            with patch.object(account_fleet, 'load_accounts', return_value=accounts):
                rec = dispatch_registry.issue(
                    {'stage': 'plan', 'choice': 'sol_high', 'selected_bundle': bundle, 'decision': {'action': 'start'}},
                    task,
                    now=self.now,
                    path=db_file,
                    allocation_task=old_alloc,
                )
            expected_db = state_upgrade.check_dispatch_db(db_file)
            self.assertIsNotNone(expected_db)
            launch = {'title': 'worker', 'initialPrompt': 'do work'}
            with self.assertRaises(ValueError) as ctx:
                dispatch_registry.prepare(
                    rec['decision_id'], task, launch, {'read_ok': True, 'accounts': {}}, accounts=accounts,
                    now=self.now, path=db_file,
                )
            self.assertEqual(str(ctx.exception), expected_db)

    def test_tray_skips_old_shape_ledger_rows_and_returns_skipped_count(self):
        ledger_file = self.state / 'router-ledger.jsonl'
        old_row = {
            'stage': 'plan', 'at': self.now.isoformat(), 'choice': 'sol_high', 'objective': 'old task',
            'profile': {
                **profile(),
                'realtime_loop': True,
                'axis_demand': {'divergence': 1, 'global_rigor': 2, 'module_precision': 2, 'vision_tools': 1},
            },
        }
        cur_row = {
            'stage': 'plan', 'at': self.now.isoformat(), 'choice': 'sol_high', 'objective': 'new task',
            'profile': profile(),
        }
        ledger_file.write_text(
            json.dumps(old_row) + '\n' + json.dumps(cur_row) + '\n',
            encoding='utf-8',
        )
        with patch.dict(os.environ, self._env()):
            rows, skipped = router_data.recent_ledger_with_skipped(limit=10, path=ledger_file)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['objective'], 'new task')


if __name__ == '__main__':
    unittest.main()


def test_legacy_verified_false_names_account_and_recovery(tmp_path):
    path = tmp_path / 'accounts-override.json'
    path.write_text(json.dumps({'schema_version': 2, 'accounts': {'example': {'verified': False}}}), encoding='utf-8')
    issue = state_upgrade.check_accounts_override(path)
    assert 'legacy disabled flag remains for example' in issue
    assert 'toggle example in the tray' in issue
    assert 'agentcompass setup' in issue
