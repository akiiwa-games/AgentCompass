"""Notification ownership and caller guidance, with no live workers."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import roll

from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


class RollNotificationTests(unittest.TestCase):
    def test_paseo_only_promises_registered_notifications(self):
        args = SimpleNamespace(title='test', workspace=None, wait=0)
        decision = {'decision_id': 'decision', 'selected_bundle': {'provider': 'codex'}}
        for status, expected in (
            ('watching', 'will be sent'), ('delivered', 'has been delivered'),
            ('stopped', 'must collect'), ('delivery_failed', 'must collect'),
            (None, 'must collect'),
        ):
            with self.subTest(status=status):
                receipt = {'state': 'started', 'agent_id': 'observed-worker', 'observed_at': '2026-11-01T00:00:00Z'}
                if status:
                    receipt['completion_notification'] = {'status': status}
                with patch.object(roll.dispatch_flow, 'reconcile'), \
                        patch.object(roll.registry, 'prepare', return_value={'dispatch_id': 'dispatch'}), \
                        patch.object(roll.dispatch_flow, 'launch', return_value=receipt):
                    result, code = roll.paseo(args, {}, decision, {}, 'fixture', 'parent', {})
                self.assertEqual(code, 0)
                self.assertIn(expected, result['next'])
                self.assertEqual(result['worker'].get('completion_notification'), receipt.get('completion_notification'))

    def test_claim_or_notifier_without_observed_worker_is_pending_with_public_continuation(self):
        args=SimpleNamespace(title='test',workspace=None,wait=0)
        decision={'decision_id':'decision','selected_bundle':{'provider':'codex'}}
        for state in ('launching','expired'):
            receipt={'state':state,'launched_at':'2026-11-01T00:00:00Z',
                     'completion_notification':{'status':'watching'}}
            with patch.object(roll.dispatch_flow,'reconcile'), \
                 patch.object(roll.registry,'prepare',return_value={'dispatch_id':'same-dispatch'}), \
                 patch.object(roll.dispatch_flow,'launch',return_value=receipt):
                result,code=roll.paseo(args,{},decision,{},'fixture','parent',{})
            self.assertIsNone(result['launched'])
            self.assertEqual(code,0)
            status=roll.status_of(result,code)
            self.assertEqual(status,'pending')
            compact=roll.compact(result,status,'receipt.json')
            self.assertEqual(compact['next_argv'][-6:],['--dispatch-id','same-dispatch','--parent-id','parent','--watch-seconds','120'])
            self.assertNotIn('scripts',' '.join(compact['next_argv']))
            self.assertNotIn('Worker launched',compact['next_action'])

    def test_cli_background_reply_explicitly_belongs_to_caller(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Mock(pid=1234)
            args = SimpleNamespace(wait=0)
            judge = Mock()
            judge.subscription_env.return_value = {}
            with patch.object(roll, 'CLI_RUNS_DIR', Path(folder)), \
                    patch.object(roll, 'provider_definition', return_value=('codex', {})), \
                    patch.object(roll, 'cli_command', return_value=['fake-codex']), \
                    patch.object(roll.subprocess, 'Popen', return_value=process):
                result, code = roll.cli(args, {'parent_id': 'parent', 'parent_host': 'native',
                    'parent_address': 'inbox'}, {'provider': 'codex', 'model': 'fixture'}, 'fixture', judge)
            self.assertEqual(code, 0)
            self.assertIn('No automatic completion notification', result['next'])
            self.assertIn('must watch worker.output and worker.stderr', result['next'])
            self.assertIn('1234', result['next'])


if __name__ == '__main__':
    unittest.main()
