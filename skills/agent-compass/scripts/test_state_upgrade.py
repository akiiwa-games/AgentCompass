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


class RetiredReferenceDataTests(unittest.TestCase):
    """Rule (router-internals, Older local state): the product removes the removed reference data channel's cache and
    setting itself, by their exact paths under the state directory, and nothing else."""

    def setUp(self):
        self.root = isolated_router_state(self.addCleanup, policy=False)
        self.state = Path(os.environ['ROUTER_STATE_DIR'])
        self.state.mkdir(parents=True, exist_ok=True)

    def plant(self):
        (self.state / 'public-data' / 'sets' / '0123456789ab').mkdir(parents=True)
        (self.state / 'public-data' / 'state.json').write_text('{"version": "0123456789ab"}', encoding='utf-8')
        (self.state / 'public-data-config.json').write_text('{"enabled": true}', encoding='utf-8')
        (self.state / 'user-scores.json').write_text('{"schema_version": 2, "scores": {}}', encoding='utf-8')
        (self.state / 'public-data-notes.txt').write_text('a sibling with a similar name', encoding='utf-8')
        (self.state / 'grok-bot').mkdir()
        (self.state / 'grok-bot' / 'config.json').write_text('{}', encoding='utf-8')

    def test_the_cleanup_removes_exactly_the_two_leftovers(self):
        self.plant()
        survivors = _snapshot_tree(self.state)
        survivors = {k: v for k, v in survivors.items() if not k.startswith(('public-data' + os.sep, 'public-data-config'))}
        removed = state_upgrade.remove_retired_reference_data(self.state)
        self.assertEqual(sorted(removed), sorted(str(self.state / n) for n in ('public-data', 'public-data-config.json')))
        self.assertFalse(os.path.lexists(self.state / 'public-data'))
        self.assertFalse(os.path.lexists(self.state / 'public-data-config.json'))
        self.assertEqual(_snapshot_tree(self.state), survivors)      # user-scores.json and every sibling survive

    def test_the_cleanup_is_a_no_op_when_nothing_is_left(self):
        (self.state / 'user-scores.json').write_text('{"schema_version": 2, "scores": {}}', encoding='utf-8')
        before = _snapshot_tree(self.state)
        self.assertEqual(state_upgrade.remove_retired_reference_data(self.state), [])
        self.assertEqual(state_upgrade.remove_retired_reference_data(self.state / 'absent'), [])
        self.assertEqual(_snapshot_tree(self.state), before)

    @unittest.skipUnless(os.name == 'nt', 'directory junctions are a Windows feature')
    def test_a_linked_leftover_is_removed_as_a_link_and_its_target_survives(self):
        import _winapi
        target = self.root / 'elsewhere'
        target.mkdir()
        (target / 'keep.json').write_text('{}', encoding='utf-8')
        _winapi.CreateJunction(str(target), str(self.state / 'public-data'))
        self.assertEqual(state_upgrade.remove_retired_reference_data(self.state), [str(self.state / 'public-data')])
        self.assertFalse(os.path.lexists(self.state / 'public-data'))
        self.assertTrue((target / 'keep.json').is_file())

    def test_setup_removes_them_and_its_dry_run_only_lists_them(self):
        import onboard
        self.plant()
        home = self.root / 'setup-home'
        with patch.dict(os.environ, {'ROUTER_STATE_DIR': str(self.state)}):
            listed = onboard.remove_retired_reference_data(home, dry_run=True)
            self.assertEqual(len(listed), 2)
            self.assertTrue((self.state / 'public-data').is_dir())
            self.assertEqual(sorted(onboard.remove_retired_reference_data(home)), sorted(listed))
        self.assertFalse((self.state / 'public-data').exists())
        self.assertTrue((self.state / 'user-scores.json').is_file())


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
        # 4. host-rules overlay holding a token with no current name
        rules_dir = self.state / 'host-rules'
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / 'shared.md').write_text('Run python route_judge.py plan before delegating.\n', encoding='utf-8')
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
        # 10. old Usage Float cache + calibration cache without fetch-manifest.json
        uf_cache = self.home / '.usage-float' / 'usage-cache.json'
        uf_cache.parent.mkdir(parents=True, exist_ok=True)
        uf_cache.write_text('{"providers":{}}', encoding='utf-8')
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
        # 12. cache and setting of the removed between-release reference data channel
        channel_cache = self.state / 'public-data'
        (channel_cache / 'sets' / '0123456789ab').mkdir(parents=True, exist_ok=True)
        (channel_cache / 'state.json').write_text('{"version": "0123456789ab"}', encoding='utf-8')
        channel_config = self.state / 'public-data-config.json'
        channel_config.write_text('{"enabled": false}', encoding='utf-8')

        before = _snapshot_tree(self.home)
        with patch.object(doctor, 'listeners', return_value={}):
            report = doctor.collect(self.home)
            rendered = doctor.render(report)
        after = _snapshot_tree(self.home)
        self.assertEqual(before, after, 'doctor must never rewrite user files')

        issues = report['upgrade_issues']
        # Derived state the product rebuilds or skips itself is not the user's chore: doctor does not list it.
        for healed in (db_file, self.state / 'usage-activity.json', self.state / 'provider-inventory.json'):
            self.assertFalse(any(str(healed) in issue for issue in issues), healed)
        self.assertIn('Upgrade checks:', rendered)
        for issue in issues:
            self.assertRegex(
                issue, r'Fix: (re-run "agentcompass setup"\.|(edit the .*or )?delete "[^"]+"( \([^)]+\))?\.)$',
                f'Unexpected fix wording in {issue!r}',
            )
        for expected_file in (
            self.state / 'accounts-override.json',
            self.state / 'host-policy.json',
            self.state / 'user-scores.json',
            rules_dir / 'shared.md',
            self.state / 'router-ledger.jsonl',
            uf_cache,
            cal_cache,
            claude_dir / 'settings.json',
            claude_dir / 'CLAUDE.md',
            old_skill,
        ):
            self.assertTrue(any(str(expected_file) in issue for issue in issues), f'Missing issue for {expected_file}')
        # Setup does not retire a link it never recorded, so the fix for the old skill link is to delete it.
        self.assertTrue(any(issue.endswith(f'Fix: {state_upgrade.fix_delete(old_skill)}.') for issue in issues))
        # Nothing reads the removed channel's files; setup (and the post-update step) removes them, doctor only names them.
        for leftover in (channel_cache, channel_config):
            self.assertTrue(any(issue.startswith(str(leftover)) and issue.endswith(f'Fix: {state_upgrade.FIX_SETUP}.')
                                for issue in issues), leftover)

    def test_runtime_loaders_fail_fast_with_the_exact_same_message_as_doctor(self):
        env = self._env()
        # 1. accounts-override.json (the shapes with no upgrade step)
        override_path = self.state / 'accounts-override.json'
        for bad_doc in (
            {'schema_version': 1, 'accounts': {}},
            {'schema_version': 2, 'accounts': {'codex': {'allowed_cwd': ['D:/work'], 'cwd_regex': 'game'}}},
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
