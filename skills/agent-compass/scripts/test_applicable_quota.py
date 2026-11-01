"""B3 contract: applicable observed hard limits bind without pricing specs."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import allocator
import dispatch_flow
import dispatch_registry as registry
import route_judge
from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


class ApplicableQuotaTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.accounts=allocator.load_accounts(allocator.REFERENCES/'accounts.json')
        self.accounts['accounts']={'codex':self.accounts['accounts']['codex']}
        self.accounts['accounts']['codex']['verified']=True
        self.accounts['accounts']['codex']['bundles']={'sol_high':{'model':'gpt-6.1-sol','effort':'high'}}
        self.accounts['_discovery']={'accounts':{},'launchers':{}}
        self.assertEqual(list(self.accounts['accounts']['codex']['windows']),['usage'])
        self.bundle={**allocator.bundles(self.accounts)['sol_high'],'bundle_id':'sol_high'}
        self.task={'task_id':'quota-contract','parent_agent_id':'parent-a',
                   'objective':'Implement the independent scoped helper change',
                   'work_kind':'general_implementation','difficulty':'hard','expected_hours':0.2,
                   'needs':{'local_only':True,'local_only_reason':'fixture code'}}

    def usage(self, remaining=0, **window):
        return {'read_ok':True,'accounts':{'codex':{'status':'fresh','fetched_at':self.now.isoformat(),
            'windows':[{'id':'usage','remaining_pct_before_reset':95,'observed_remaining_pct':95,
                        'resets_at':(self.now+timedelta(hours=100)).isoformat()},
                       {'id':'five_hour','remaining_pct_before_reset':remaining,
                        'observed_remaining_pct':remaining,'resets_at':(self.now+timedelta(hours=3)).isoformat(),**window}]}}}

    def plan(self, usage):
        return allocator.plan(self.task,usage,self.now,accounts=self.accounts,overrides=[],host_policy_config=None)

    def rejected(self, usage, bundle=None):
        try:
            registry.check_launch_quota(bundle or self.bundle,self.accounts,usage,self.now)
        except ValueError:
            return True
        return False

    def test_missing_five_hour_spec_blocks_then_fresh_recovery_and_alternative_allow(self):
        exhausted=self.usage(0)
        self.assertTrue(self.rejected(exhausted))
        denied=self.plan(exhausted)
        self.assertIn(denied['choice'],(None,'none'))
        recovered=self.usage(80)
        self.assertFalse(self.rejected(recovered))
        self.assertEqual(self.plan(recovered)['selected_bundle']['account'],'codex')
        alternative=copy.deepcopy(self.accounts['accounts']['codex'])
        alternative['bundles']={'other_sol_high':{'provider':'codex','harness':'codex',
                              'model':'gpt-6.1-sol','effort':'high'}}
        self.accounts['accounts']['codex-other']=alternative
        exhausted['accounts']['codex-other']=copy.deepcopy(recovered['accounts']['codex'])
        self.assertEqual(self.plan(exhausted)['selected_bundle']['account'],'codex-other')

    def test_scope_and_explicit_draws_preserve_the_actual_consumed_pool(self):
        cases=[
            ({'id':'five_hour'},None,True),
            ({'id':'five_hour','models':['claude']},None,False),
            ({'id':'weekly_fable'},None,False),
            ({'id':'extra','models':['gpt']},None,True),
            ({'id':'extra','models':['claude']},None,False),
            ({'id':'unknown_scope'},None,False),
            ({'id':'five_hour'},[],False),
            ({'id':'five_hour'},['usage'],False),
            ({'id':'five_hour'},['five_hour'],True),
        ]
        for window,draws,blocked in cases:
            with self.subTest(window=window,draws=draws):
                bundle=dict(self.bundle)
                if draws is not None: bundle['draws']=draws
                self.assertEqual(self.rejected(self.usage(0,**window),bundle),blocked)
        # A configured model restriction remains authoritative too.
        self.accounts['accounts']['codex']['windows']['five_hour']={'hours':5,'models':['claude']}
        self.assertFalse(self.rejected(self.usage(0)))

    def test_declared_quota_source_wins_over_the_launcher_named_row(self):
        self.accounts['quota_sources']={'codex':'shared-subscription'}
        usage=self.usage(80)
        usage['accounts']['shared-subscription']=self.usage(0)['accounts']['codex']
        self.assertTrue(self.rejected(usage))
        self.assertIn(self.plan(usage)['choice'],(None,'none'))
        usage['accounts']['shared-subscription']=self.usage(80)['accounts']['codex']
        self.assertFalse(self.rejected(usage))
        self.assertEqual(self.plan(usage)['selected_bundle']['account'],'codex')

    def test_uncertain_or_inactive_telemetry_retains_existing_non_exhaustion_policy(self):
        for change in ({'status':'unknown'},{'status':'stale'},{'stale':True},
                       {'fetched_at':(self.now-timedelta(hours=1)).isoformat()}, {'windows':[]}):
            usage=self.usage(0)
            usage['accounts']['codex'].update(change)
            with self.subTest(change=change): self.assertFalse(self.rejected(usage))
        for remaining in (None,'unknown',float('nan')):
            with self.subTest(remaining=remaining): self.assertFalse(self.rejected(self.usage(remaining)))
        self.assertFalse(self.rejected(self.usage(0,resets_at=(self.now-timedelta(seconds=1)).isoformat())))
        # No spec is invented to price a newly observed hard limit.
        inferred=[spec for wid,spec,_ in allocator.applicable_windows(
            self.accounts['accounts']['codex'],self.bundle['model'],self.usage()['accounts']['codex'],
            self.bundle,self.accounts) if wid=='five_hour']
        self.assertEqual(inferred,[{}])

    def test_real_claim_preflight_creates_no_worker_until_a_fresh_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(__import__('os').environ,
                {'ROUTER_DISPATCH_DB':str(Path(tmp)/'dispatch.sqlite3'),'PASEO_AGENT_ID':''}):
            healthy=self.usage(80)
            decision=registry.issue({'stage':'record','choice':'sol_high','selected_bundle':self.bundle,
                'constraint':'user_override','basis':'Test fixture user selected model'},self.task,self.now)
            launch={'title':'Quota fixture','initialPrompt':'Only the scoped fixture assignment'}
            row=registry.prepare(decision['decision_id'],self.task,launch,healthy,accounts=self.accounts,now=self.now)
            host=Mock()
            with patch.object(allocator,'load_accounts',return_value=self.accounts), \
                 patch.object(route_judge,'usage_snapshot',return_value=self.usage(0)):
                with self.assertRaises(ValueError):
                    dispatch_flow.launch(row['dispatch_id'],'parent-a',host=host,watch_seconds=0)
            host.assert_not_called()  # the only path which could create a worker/process
            self.assertEqual(registry.inspect(row['dispatch_id'])['state'],'prepared')
            snapshot={'id':'same-fixture-worker','provider':'codex','model':self.bundle['model'],
                'status':'running','updatedAt':(self.now+timedelta(seconds=10)).isoformat(),
                'currentModeId':'full-access','thinkingOptionId':'high','labels':row['request']['labels']}
            calls=[]
            def available(request):
                calls.append(request['operation'])
                return {'ok':True,'agent':snapshot}
            with patch.object(allocator,'load_accounts',return_value=self.accounts), \
                 patch.object(route_judge,'usage_snapshot',return_value=healthy):
                started=dispatch_flow.launch(row['dispatch_id'],'parent-a',host=available,watch_seconds=0)
            self.assertEqual(started['state'],'started')
            self.assertEqual(calls.count('create'),1)

    def test_usage_projection_retains_only_valid_observed_model_scopes(self):
        raw={'id':'extra','used_pct':100,'resets_at':(self.now+timedelta(hours=3)).isoformat(),
             'models':['claude'],'unrelated_secret':'never expose'}
        projected=route_judge.window_state(raw,self.now)
        self.assertEqual(projected['models'],['claude'])
        self.assertNotIn('unrelated_secret',projected)
        for malformed in ('claude',[None],[]):
            self.assertNotIn('models',route_judge.window_state({**raw,'models':malformed},self.now))


if __name__=='__main__':
    unittest.main()
