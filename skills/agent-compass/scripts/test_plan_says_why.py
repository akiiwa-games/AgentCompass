"""What a caller of `plan` and `roll` reads when a flag of its own, the parent it runs under, or the user's local settings removed
a route (refusal audit 2026-11-21, part 2: findings 5, 6, 7, 10, 13, 16, the cwd_regex refusal of `record`, and the code-only
items on `account_fleet` and `host_placement`).

The tests assert rules: the text carries the facts the owning function holds (the flag, the account, the setting, the directory,
the harness) and offers no way out that cannot work for them. They never assert which route wins.
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

import account_fleet
import allocator
import candidate_eval
import cwd_policy
import dispatch_flow as flow
import host_herdr
import host_placement
import host_policy
import route_judge
import test_dispatch as td
import test_roll_answers as tra
from router_test_support import isolated_router_state


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup, policy=False)


TASK = {'work_kind': 'code_implementation', 'difficulty': 'hard', 'expected_hours': 0.5}


def task_with_profile(**extra):
    task = dict(TASK, **extra)
    task['profile'] = td.profile_for(task)
    return task


def remote_exclusions(plan):
    """The exclusions of runners that work remotely (the only candidates a local-only task removes for that reason)."""
    return [r for c in plan['ranking'] for r in c['excluded'] if 'runs remotely' in r]


class Base(tra.Plain):
    def plan(self, task, **kw):
        return allocator.plan(task, self.usage, self.now, accounts=self.accounts, **kw)


class WorkspaceMakesTheWorkLocal(Base):
    """Finding 5: --workspace marks the work local-only; the caller reads that its own flag removed the remote routes."""

    def build(self, **over):
        import roll
        said = []
        task = roll.build_task(self.args(self.brief_file(), **over), '# Task\n', 'parent-a', said)
        return task, said

    def test_the_roll_answer_says_workspace_made_the_work_local_only(self):
        # Rule (roll.build_task): --workspace without --local-only sets needs.local_only; the answer says so, names the flag
        # and the way out.
        ws = self.root / 'ws'
        ws.mkdir()
        res, _ = self.run_roll(workspace=str(ws), access='read-only', task_id='t-ws')
        said = [a for a in res.get('assumptions') or () if '--workspace' in a]
        self.assertEqual(len(said), 1, res)
        self.assertIn(host_placement.canonical_dir(str(ws)), said[0])
        for fact in ('local-only', 'remote', 'without --workspace'):
            self.assertIn(fact, said[0])

    def test_no_workspace_assumption_without_workspace_or_with_local_only(self):
        # Rule: the note belongs to --workspace's inference only; an explicit --local-only is the caller's own statement.
        ws = self.root / 'ws'
        ws.mkdir()
        for over in ({}, {'workspace': str(ws), 'local_only': 'needs this repo'}):
            with self.subTest(over=over):
                self.assertEqual(self.build(**over)[1], [])

    def test_a_remote_runner_exclusion_names_the_input_that_marked_the_work_local(self):
        # Rule (candidate_eval, task_intake.LOCAL_ONLY_BY): the exclusion of a remote runner names the input that set
        # needs.local_only, so --workspace and --local-only read differently, and it carries no free text of the task.
        import task_intake
        ws = self.root / 'ws'
        ws.mkdir()
        for over, by in (({'workspace': str(ws)}, '--workspace'), ({'local_only': 'needs this repo'}, '--local-only')):
            with self.subTest(by=by):
                said = remote_exclusions(self.plan(task_with_profile(needs=self.build(**over)[0]['needs'])))
                self.assertTrue(said)
                for reason in said:
                    self.assertIn(task_intake.LOCAL_ONLY_BY[by], reason)
                    self.assertNotIn('needs this repo', reason)
                    self.assertNotIn(str(ws.name), reason)


class LocalSettingsAreSaid(Base):
    """Finding 6: a route the user's local settings exclude names the account or model, the setting, and that it is this
    machine's own setting; when only such settings keep the closest candidates out, none_next says so and offers what works."""

    def nothing_allowed(self):
        return self.plan(task_with_profile(), host_policy_config={'allowed_accounts': ['nonexistent']})

    def test_allowed_accounts_names_the_account_the_file_and_whose_setting_it_is(self):
        # Rule (host_policy.outside_routing, candidate_eval.said_exclusions): the reason a caller reads names each account.
        plan = self.nothing_allowed()
        reason = plan['decision']['reason']
        closest = [c for c in plan['ranking'] if c['id'] in reason.split('Closest: ', 1)[1]]
        self.assertTrue(closest)
        for c in closest:
            self.assertIn(host_policy.outside_routing(c['bundle']['account']), reason)
        self.assertIn(host_policy.NOT_CHOSEN, reason)
        self.assertNotIn(f': {candidate_eval.OUT_OF_AUTOMATIC_ROUTING}', reason)

    def test_none_next_offers_a_named_route_for_allowed_accounts_and_never_a_policy_change(self):
        # Rule (dispatch-rules 5): a named route reaches past allowed_accounts and the checkbox; the user's policy is theirs.
        import roll
        plan = self.nothing_allowed()
        self.assertTrue(plan['decision']['local_settings']['named_route_reaches'])
        text = roll.none_next(plan)
        for fact in ("user's own settings", 'allowed_accounts of host-policy.json', '--provider', '--account', '--model',
                     '--basis'):
            self.assertIn(fact, text)
        for advice in ('edit', 'change the', 'add the account', 'remove'):
            self.assertNotIn(advice, text.lower())

    def test_the_roll_answer_reads_it(self):
        policy = host_policy.policy_path()[0]
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({'allowed_accounts': ['nonexistent']}), encoding='utf-8')
        self.addCleanup(lambda: policy.unlink(missing_ok=True))
        res, code = self.run_roll(task_id='t-policy')
        self.assertEqual((code, res['status']), (2, 'blocked'))
        self.assertIn('allowed_accounts of host-policy.json', res['next_action'])
        self.assertIn('--basis', res['next_action'])

    def test_the_checkbox_names_the_model(self):
        # Rule (host_policy.unchecked_routing): an unchecked bundle names account/model and the checkbox.
        for spec in self.accounts['accounts'].values():
            for bundle in (spec.get('bundles') or {}).values():
                bundle['disabled'] = True
        plan = self.plan(task_with_profile(), host_policy_config=None)
        for c in plan['ranking']:
            if candidate_eval.UNCHECKED in c['excluded']:
                said = candidate_eval.said_exclusions(c)
                self.assertIn(host_policy.unchecked_routing(c['bundle']['account'], c['bundle']['model']) + ': '
                              + host_policy.NOT_CHOSEN, said)
        self.assertIn('unchecked in model management', plan['decision']['reason'])
        self.assertIn('/', plan['decision']['reason'].split('Closest: ', 1)[1])

    def test_cwd_regex_names_the_account_the_pattern_and_the_directory_and_offers_no_named_route(self):
        # Rule (cwd_policy.refusal; route_judge.record_constraint refuses a named route outside cwd_regex too).
        import roll
        for spec in self.accounts['accounts'].values():
            spec['cwd_regex'] = '^D:/only-here'
        plan = self.plan(task_with_profile(cwd='D:/work/elsewhere'), host_policy_config=None)
        reason = plan['decision']['reason']
        for fact in ('^D:/only-here', 'D:/work/elsewhere', "user's own account setting on this machine"):
            self.assertIn(fact, reason)
        settings = plan['decision'].get('local_settings')
        if settings is not None:            # every closest candidate is out by local settings alone
            self.assertFalse(settings['named_route_reaches'])
            text = roll.none_next(plan)
            self.assertIn('cwd_regex holds for a named route too', text)
            self.assertNotIn('--basis', text)

    def test_cwd_regex_without_a_task_directory_names_the_process_directory(self):
        self.assertIn("this process's working directory " + cwd_policy.normalized(), cwd_policy.refusal('a', '^x'))

    def test_record_names_the_directory_and_whose_setting_it_is(self):
        # Rule: the named-route refusal reads the same sentence (cwd_policy.refusal) and says nothing else launches it.
        self.accounts['accounts']['codex']['cwd_regex'] = '^D:/only-here'
        bundle = next(b for b in account_fleet.bundles(self.accounts).values() if b['account'] == 'codex' and not b.get('disabled'))
        args = argparse.Namespace(basis='User asked', provider='codex', account='codex', model=bundle['model'], effort=None,
                                  input=None, usage_cache=None, accounts=None, constraint='user_override', specialty=None,
                                  no_score=True, dry_run=True, keep_whole=None, fast=False, reason_category=None,
                                  parent_id=None)
        task = {'objective': 'x', 'task_id': 't-cwd', 'expected_hours': 1.0, 'cwd': 'D:/work/elsewhere'}
        import usage_reading
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts),                 patch.object(usage_reading, 'usage_snapshot', return_value=self.usage),                 self.assertRaises(ValueError) as refused:
            route_judge.record_constraint(args, task, route_judge.classify_pinned(task, args))
        text = str(refused.exception)
        self.assertIn(cwd_policy.refusal('codex', '^D:/only-here', 'D:/work/elsewhere'), text)
        self.assertIn('named route does not reach past it', text)


class HerdrParent(Base):
    """Finding 7: Herdr says which fact refused, and the plan does not choose what a Herdr parent cannot launch."""

    def test_interactive_says_the_harness_and_the_mode_apart(self):
        # Rule (host_herdr.cannot_run): an unsupported harness names the harness, never a permission mode; a mode the harness
        # cannot reproduce names the mode and the one Herdr launches.
        with self.assertRaises(ValueError) as harness:
            host_herdr.interactive({'provider': 'antigravity', 'model': 'm', 'modeId': None})
        self.assertIn('cannot run the', str(harness.exception))
        self.assertNotIn('permission mode', str(harness.exception))
        with self.assertRaises(ValueError) as mode:
            host_herdr.interactive({'provider': 'claude-lab', 'model': 'm', 'modeId': 'plan'})
        self.assertIn("permission mode 'plan'", str(mode.exception))
        self.assertIn(repr(host_herdr.PERMISSION_MODES['claude']), str(mode.exception))

    def test_a_bundle_herdr_cannot_launch_is_not_told_to_roll_again(self):
        out = {'parent_id': 'parent-a', 'parent_address': 'term-1'}
        res, code = host_herdr.launch(out, {'provider': 'antigravity', 'model': 'm'}, self.brief_file(), 't-h')
        self.assertEqual((code, res['launched']), (2, False))
        self.assertIn('cannot run the', res['error'])
        self.assertNotIn('roll again', res['next'])
        self.assertIn('--basis', res['next'])

    def test_the_plan_excludes_what_a_herdr_parent_cannot_launch_and_nothing_else(self):
        # Rule (candidate_eval + host_herdr.cannot_run): with a Herdr parent, a candidate gains exactly the Herdr exclusion
        # when cannot_run names one for its harness, and keeps every other exclusion it had.
        before = {c['id']: c for c in self.plan(task_with_profile())['ranking']}
        after = {c['id']: c for c in self.plan(task_with_profile(parent_host='herdr'))['ranking']}
        self.assertEqual(set(before), set(after))
        gained = 0
        for bid, c in after.items():
            why = host_herdr.cannot_run(c['bundle'])
            extra = [r for r in c['excluded'] if r not in before[bid]['excluded']]
            self.assertEqual(extra, [why] if why else [], bid)
            gained += bool(why)
        self.assertGreater(gained, 0)       # the reference fleet has harnesses Herdr does not run

    def test_a_parent_that_is_not_herdr_plans_as_before(self):
        # Refactor equivalence: no parent host and a Paseo or native parent read the same plan.
        base = self.plan(task_with_profile())
        for host in ('paseo', 'native', 'orca'):
            with self.subTest(host=host):
                other = self.plan(task_with_profile(parent_host=host))
                self.assertEqual((other['choice'], [(c['id'], c['excluded'], c['score']) for c in other['ranking']]),
                                 (base['choice'], [(c['id'], c['excluded'], c['score']) for c in base['ranking']]))


class BatchUnexpectedError(Base):
    """Finding 10: a batch that meets an unexpected error keeps the results before it and names what did not start."""

    def run_batch(self, briefs, raise_on_second):
        import roll
        real = roll.run
        calls = {'n': 0}

        def run(args, judge):
            calls['n'] += 1
            if calls['n'] == 2:
                raise raise_on_second
            return real(args, judge)
        _, host, judge, _ = self._setup_paseo_env()
        judge.failure_answer = staticmethod(route_judge.failure_answer)
        self.enterContext(td.fake_owners(self))
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), \
                patch.object(roll, 'WORKER_PROMPTS_DIR', self.root / 'wp'), patch.object(roll, 'RESULTS_DIR', self.root / 'rr'), \
                patch.object(flow, 'host_call', host), patch.object(roll, 'run', run), \
                patch.object(roll, 'build_task', lambda *a, _b=roll.build_task, **k: tra.with_profile(_b(*a, **k))), \
                patch.object(roll.scorer_client, 'prefetch_scores', lambda *a, **k: None):
            return roll.run_batch(self.args(briefs, hours=None, score_timeout=120), judge)

    def test_any_exception_class_answers_batch_with_done_failed_and_not_started(self):
        for exc in (sqlite3.OperationalError('database is locked'), KeyError('profile')):
            with self.subTest(exc=type(exc).__name__):
                briefs = [self.brief_file(f'b{i}.md', f'# Batch brief {i}\n') for i in range(3)]
                res, code = self.run_batch(briefs, exc)
                self.assertEqual((res['status'], code), ('batch', 1))
                self.assertEqual([r['brief'] for r in res['results']], [str(briefs[0]), str(briefs[1])])
                self.assertNotEqual(res['results'][0]['status'], 'error')
                failed = res['results'][1]
                answer = route_judge.failure_answer(exc, 'roll')
                self.assertEqual((failed['status'], failed['error_type'], failed['cause']),
                                 ('error', answer['error_type'], answer['cause']))
                self.assertEqual(res['not_started'], [str(briefs[2])])
                self.assertIn('not started', res['next_action'])
                self.setUp()

    def test_a_runtime_error_says_no_host_text(self):
        # Rule (route_judge.failure_cause): a RuntimeError's text may carry a host's reply; only its class is said.
        briefs = [self.brief_file(f'r{i}.md', f'# Runtime brief {i}\n') for i in range(2)]
        res, _ = self.run_batch(briefs, RuntimeError('{"reply": "host free text"}'))
        self.assertNotIn('host free text', json.dumps(res))
        self.assertNotIn('not_started', res)     # the batch goes on after a failure of one brief


class ParentConflict(unittest.TestCase):
    """Finding 13: `resolve_parent` names the input that conflicts with PASEO_AGENT_ID and the way out."""

    def resolve(self, **over):
        import worker_context
        args = argparse.Namespace(**{'parent_host': None, 'parent_id': None, 'parent_address': None, **over})
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a'}), self.assertRaises(ValueError) as refused:
            worker_context.resolve_parent(args)
        return str(refused.exception)

    def test_each_conflicting_input_is_named(self):
        for over, named in (({'parent_host': 'herdr'}, '--parent-host herdr'),
                            ({'parent_host': 'native', 'parent_address': 'inbox'}, '--parent-host native with --parent-address'),
                            ({'parent_id': 'someone-else'}, '--parent-id someone-else')):
            with self.subTest(over=over):
                text = self.resolve(**over)
                self.assertIn(named, text)
                self.assertIn('PASEO_AGENT_ID parent-a', text)
                self.assertIn('Drop --parent-', text)


class ReadOnlyWorkspaceUnreadable(Base):
    """Finding 16: a read-only --workspace whose Paseo cannot be read says the placement was not verified."""

    def test_read_only_and_write_both_say_paseo_was_not_read(self):
        ws = self.root / 'ws'
        ws.mkdir()

        def host(request):
            if request['operation'] == 'workspace_agents':
                return {'ok': False, 'error': 'host_connection_failed'}
            if request['operation'] not in ('create', 'resume'):
                return {'ok': False, 'error': 'agent_not_observed'}
            return {'ok': True, 'agent': self.snapshot({'request': request['request']}, status='running', id='agent-1')}
        for access in ('read-only', 'write'):
            with self.subTest(access=access):
                res, _ = self.run_roll(self.brief_file(f'w-{access}.md', f'# {access}\n'), host=host, workspace=str(ws),
                                       access=access, task_id='t-' + access)
                said = [a for a in res.get('assumptions') or () if 'host_connection_failed' in a]
                self.assertEqual(len(said), 1, res)
                self.assertIn(host_placement.canonical_dir(str(ws)), said[0])


class CodeOnly(Base):
    def test_a_failed_policy_sync_is_said_in_the_plan(self):
        # Rule (account_fleet.load_accounts keeps the error; host_policy.sync_failed is its wording).
        accounts = dict(self.accounts, _host_policy_sync={'mode': 'error', 'added': [], 'error': 'file is locked'})
        plan = allocator.plan(task_with_profile(), self.usage, self.now, accounts=accounts)
        self.assertIn(host_policy.sync_failed('file is locked'), plan['decision'].get('warnings') or ())

    def test_every_surface_roll_builds_is_placed(self):
        # Rule: roll builds a remote runner, paseo, herdr, cli or a native host; each has a placement (no unreachable branch).
        import worker_context
        for surface in (*host_placement.REMOTE_SURFACES, 'paseo', 'herdr', 'cli', *worker_context.NATIVE_HOSTS):
            with self.subTest(surface=surface):
                self.assertTrue(host_placement.place(surface, None, default_cwd='D:/work')['ok'])


class HerdrNamedRoute(Base):
    def test_a_named_route_herdr_cannot_launch_says_the_harness_and_exits_2(self):
        # Rule (host_herdr._launch): a route the user named on a harness Herdr does not run is refused before any terminal,
        # exit 2, naming the harness; the way out is a route Herdr runs, never "roll again".
        import shutil
        bundle = next(b for b in account_fleet.bundles(self.accounts).values()
                      if host_herdr.cannot_run(b) and not b.get('disabled')
                      and b['account'] in ('claude-lab', 'codex', 'antigravity'))
        with patch.object(host_herdr, 'hc', lambda *a, **k: {'terminal': {'state': 'live'}}), \
                patch.object(shutil, 'which', return_value='hc'):
            res, code = self.run_roll(env={'PASEO_AGENT_ID': '', 'HC_TERMINAL_ID': 'term-1'}, parent_host='herdr',
                                      provider=bundle['provider'], account=bundle['account'], model=bundle['model'],
                                      basis='the user named it', task_id='t-named', accounts=None, specialty=None,
                                      no_score=True, keep_whole=None, reason_category=None, input=None)
        self.assertEqual((code, res['status']), (2, 'blocked'))
        self.assertIn(host_herdr.cannot_run(bundle), res['cause'])
        self.assertNotIn('roll again', res['next_action'])


class NotTheUsersCheckbox(Base):
    """Review finding 1: a row off by default in the shipped data, or one discovery no longer sees, is not the user's setting."""

    def test_off_by_default_says_the_shipped_data_and_naming_and_is_not_a_local_setting(self):
        # Rule (auto_bundles: disabled_by_default is public data, reached by naming the model; router-internals.md).
        import auto_bundles
        import roll
        for spec in self.accounts['accounts'].values():
            for bundle in (spec.get('bundles') or {}).values():
                bundle.update(disabled=True, disabled_by_default=True)
        plan = self.plan(task_with_profile(), host_policy_config=None)
        self.assertNotIn(host_policy.NOT_CHOSEN, plan['decision']['reason'])
        self.assertIsNone(plan['decision'].get('local_settings'))
        self.assertNotIn("user's choice", roll.none_next(plan))
        seen = 0
        for c in plan['ranking']:
            if candidate_eval.UNCHECKED in c['excluded']:
                seen += 1
                self.assertIn(auto_bundles.off_by_default(c['bundle']['account'], c['bundle']['model']),
                              candidate_eval.said_exclusions(c))
        self.assertGreater(seen, 0)

    def test_a_row_discovery_no_longer_sees_is_said_by_discovery_alone(self):
        # Rule (auto_bundles: an expired row is disabled and carries discovery_unavailable): one fact, its own words.
        for spec in self.accounts['accounts'].values():
            for bundle in (spec.get('bundles') or {}).values():
                bundle.update(disabled=True, discovery_unavailable=True)
        plan = self.plan(task_with_profile(), host_policy_config=None)
        self.assertNotIn(host_policy.NOT_CHOSEN, plan['decision']['reason'])
        self.assertIsNone(plan['decision'].get('local_settings'))
        for c in plan['ranking']:
            if candidate_eval.UNCHECKED in c['excluded']:
                said = candidate_eval.said_exclusions(c)
                self.assertIn('model not detected by discovery for 7+ days', said)
                self.assertFalse(any('unchecked' in r for r in said), said)


class WorkerDirectory(Base):
    """Review finding 2: an account's cwd_regex is matched against the directory the worker runs in."""

    def restrict(self, account, directory):
        import re
        self.accounts['accounts'][account]['cwd_regex'] = '^' + re.escape(cwd_policy.normalized(directory)) + '$'

    def rows(self, plan, account):
        return [c for c in plan['ranking'] if c['bundle']['account'] == account]

    def build(self, **over):
        import roll
        return tra.with_profile(roll.build_task(self.args(self.brief_file(), **over), '# Task\n', 'parent-a', []))

    def test_run_a_an_account_restricted_to_the_workspace_is_not_refused_from_elsewhere(self):
        ws = self.root / 'allowed-ws'
        ws.mkdir()
        self.restrict('claude-lab', host_placement.canonical_dir(str(ws)))
        plan = self.plan(self.build(workspace=str(ws)), host_policy_config=None)
        self.assertTrue(self.rows(plan, 'claude-lab'))
        for c in self.rows(plan, 'claude-lab'):
            self.assertFalse(any('cwd_regex' in r for r in c['excluded']), c['excluded'])

    def test_run_b_an_account_restricted_to_the_process_directory_is_refused_for_another_workspace(self):
        ws = self.root / 'other-ws'
        ws.mkdir()
        self.restrict('claude-lab', os.getcwd())
        plan = self.plan(self.build(workspace=str(ws)), host_policy_config=None)
        canon = cwd_policy.normalized(host_placement.canonical_dir(str(ws)))
        for c in self.rows(plan, 'claude-lab'):
            self.assertTrue(any('cwd_regex' in r and canon in r for r in c['excluded']), c['excluded'])

    def test_without_workspace_the_parent_directory_is_matched_and_the_launch_lands_there(self):
        # Rule (host_placement.worker_directory): no --workspace -> the parent's directory (PASEO_AGENT_CWD, else the
        # process directory), which the launch gives the worker too.
        there = self.root / 'parent-dir'
        there.mkdir()
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', 'PASEO_AGENT_CWD': str(there)}):
            task = self.build()
            self.assertEqual(task['cwd'], host_placement.worker_directory(None))
        self.assertEqual(cwd_policy.normalized(task['cwd']), cwd_policy.normalized(there))
        for name in self.accounts['accounts']:
            self.restrict(name, there)
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            res, _ = self.run_roll(env={'PASEO_AGENT_CWD': str(there)}, task_id='t-parent-dir')
        self.assertEqual(res['status'], 'launched', res)
        import dispatch_registry
        row = dispatch_registry.inspect(tra.full(res)['worker']['dispatch_id'])
        self.assertEqual(cwd_policy.normalized(row['cwd']), cwd_policy.normalized(there))   # where the launch placed it

    def test_a_launch_run_outside_the_paseo_session_places_the_worker_where_the_command_runs(self):
        # Rule (host_placement.parent_cwd): PASEO_AGENT_CWD is the directory of the Paseo agent PASEO_AGENT_ID names;
        # without that id it is no parent's, so a Herdr launch (run outside the Paseo session) starts the worker in the
        # process directory, the directory the plan matched.
        there = self.root / 'paseo-agent-dir'
        there.mkdir()
        seen = []

        def launch(out, bundle, prompt_file, task_id, *, cwd=None, dispatch_id=None):
            seen.append(cwd)
            return dict(out, launched=True, dispatch_mode='herdr', next='x'), 0
        with patch.object(host_herdr, 'launch', launch):
            res, _ = self.run_roll(env={'PASEO_AGENT_ID': '', 'HC_TERMINAL_ID': 'term-1', 'PASEO_AGENT_CWD': str(there)},
                                   parent_host='herdr', task_id='t-herdr-dir')
        self.assertEqual([cwd_policy.normalized(c) for c in seen], [cwd_policy.normalized(os.getcwd())], res)

    def test_paseo_agent_cwd_is_read_only_with_paseo_agent_id(self):
        there = self.root / 'paseo-agent-dir'
        there.mkdir()
        with patch.dict(os.environ, {'PASEO_AGENT_ID': '', 'PASEO_AGENT_CWD': str(there)}):
            self.assertEqual(host_placement.parent_cwd(), os.getcwd())
        with patch.dict(os.environ, {'PASEO_AGENT_ID': 'parent-a', 'PASEO_AGENT_CWD': str(there)}):
            self.assertEqual(host_placement.parent_cwd(), str(there))

    def test_the_cli_launch_starts_the_worker_in_the_directory_the_plan_matched(self):
        # Rule (host_placement.worker_directory): the cli surface places the worker in the parent's directory too.
        import roll
        there = self.root / 'paseo-agent-dir'
        there.mkdir()
        started = []

        def popen(argv, cwd=None, **kw):
            started.append(cwd)
            raise OSError('test: not started')
        with patch.object(roll.subprocess, 'Popen', popen), patch.object(roll, 'paseo_choice', lambda *a, **k: (False, 'test')):
            res, _ = self.run_roll(env={'PASEO_AGENT_CWD': str(there)}, task_id='t-cli-dir')
        self.assertEqual([cwd_policy.normalized(c) for c in started], [cwd_policy.normalized(there)], res)

    def test_a_roll_whose_worker_runs_in_the_process_directory_plans_as_before(self):
        # Refactor equivalence: the process directory matched by name or by default reads the same plan.
        self.restrict('codex', os.getcwd())
        self.restrict('claude-lab', 'D:/elsewhere')
        before = self.plan(task_with_profile(), host_policy_config=None)
        after = self.plan(task_with_profile(cwd=os.getcwd()), host_policy_config=None)
        self.assertEqual([(c['id'], c['excluded'], c['score']) for c in after['ranking']],
                         [(c['id'], [r.replace("this process's working directory", "the worker's directory") for r in c['excluded']],
                           c['score']) for c in before['ranking']])


class LocalOnlyByIsValidated(Base):
    """Review finding 3: needs.local_only_by is checked where task input is, and a caller's document cannot name a flag."""

    def test_a_value_outside_the_vocabulary_is_refused_with_the_vocabulary(self):
        import task_intake
        with self.assertRaises(ValueError) as refused:
            task_intake.check_caller_task({'needs': {'local_only': True, 'local_only_by': 'bogus'}})
        for token in task_intake.LOCAL_ONLY_BY:
            self.assertIn(token, str(refused.exception))

    def test_a_plan_input_document_reads_as_the_callers_own_mark(self):
        import task_intake
        for by in task_intake.LOCAL_ONLY_BY:
            with self.subTest(by=by):
                document = {'task': dict(TASK, needs={'local_only': True, 'local_only_by': by})}
                task = route_judge._plan_input_task(document, argparse.Namespace(stage='plan', hours=None, can_wait_hours=None))
                said = remote_exclusions(self.plan(task_with_profile(needs=task['needs'])))
                self.assertTrue(said)
                self.assertTrue(all(r.startswith('the caller marked') for r in said), said)

    def test_roll_keeps_the_flag_it_read(self):
        import roll
        ws = self.root / 'ws'
        ws.mkdir()
        task = roll.build_task(self.args(self.brief_file(), workspace=str(ws)), '# Task\n', 'parent-a', [])
        kept = route_judge._plan_input_task({'task': task}, argparse.Namespace(stage='roll', hours=None, can_wait_hours=None))
        self.assertEqual(kept['needs']['local_only_by'], '--workspace')


class HerdrOneHarness(Base):
    """Review finding 5: the plan and the launch read the harness that runs a bundle from one function."""

    def test_a_row_on_a_provider_that_extends_opencode_is_refused_in_the_plan_as_at_the_launch(self):
        config = self.root / '.paseo' / 'config.json'
        doc = json.loads(config.read_text())
        doc['agents']['providers']['oc-claude'] = {'extends': 'opencode', 'enabled': True}
        config.write_text(json.dumps(doc))
        bundle = {'provider': 'oc-claude', 'model': 'claude-sonnet-5-5', 'id': 'oc_sonnet'}
        why = host_herdr.cannot_run(bundle)
        self.assertIsNotNone(why)
        res, code = host_herdr.launch({'parent_id': 'parent-a', 'parent_address': 'term-1'}, bundle, self.brief_file(), 't-oc')
        self.assertEqual(code, 2)
        self.assertEqual(res['error'], f'{why}; reroute, do not substitute.')


class BatchInterrupted(Base):
    """Review finding 6: an interrupt after a launch prints what the batch has, then goes on."""

    def test_keyboard_interrupt_prints_the_results_so_far_and_is_not_swallowed(self):
        import contextlib
        import io
        import roll
        calls = {'n': 0}

        def run(args, judge):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'status': 'launched', 'worker': {'agent_id': 'agent-first'}}, 0
            raise KeyboardInterrupt
        briefs = [self.brief_file(f'k{i}.md', f'# Interrupted brief {i}\n') for i in range(3)]
        _, _, judge, _ = self._setup_paseo_env()
        printed = io.StringIO()
        with patch.object(roll, 'run', run), patch.object(roll.scorer_client, 'prefetch_scores', lambda *a, **k: None), \
                contextlib.redirect_stdout(printed), self.assertRaises(KeyboardInterrupt):
            roll.run_batch(self.args(briefs, hours=None, score_timeout=120), judge)
        answer = json.loads(printed.getvalue())
        self.assertEqual(answer['status'], 'batch')
        self.assertEqual(answer['results'][0]['worker']['agent_id'], 'agent-first')
        self.assertEqual(answer['interrupted'], str(briefs[1]))
        self.assertEqual(answer['not_started'], [str(briefs[2])])


class ResumedWorkerDirectory(Base):
    """Close-out finding A: a resumed worker runs in its own directory, and that is what its account's cwd_regex is
    matched against (host_placement.worker_directory); a directory that cannot be read is matched against nothing."""
    restrict = WorkerDirectory.restrict

    def dirs(self):
        home, parent = self.root / 'worker-home', self.root / 'parent-x'
        home.mkdir()
        parent.mkdir()
        return home, parent

    def resume(self, parent, **over):
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            return self.run_roll(self.brief_file('r.md'), resume='agent-r', env={'PASEO_AGENT_CWD': str(parent)},
                                 verbose=True, **{'task_id': 't-r', **over})

    def test_d6_a_turn_inside_the_restriction_is_placed(self):
        home, parent = self.dirs()
        self.make_worker('agent-r', doc={'cwd': str(home)})
        self.restrict('claude-lab', home)
        res, code = self.resume(parent)
        self.assertEqual((res['status'], code), ('launched', 0), res.get('next_action'))

    def test_d6_a_turn_outside_the_restriction_is_refused_naming_the_worker_directory(self):
        home, parent = self.dirs()
        self.make_worker('agent-r', doc={'cwd': str(home)})
        self.restrict('claude-lab', parent)
        res, code = self.resume(parent)
        self.assertEqual((res['status'], code), ('blocked', 2), res.get('next_action'))
        self.assertIn(f"the worker's directory {cwd_policy.normalized(home)} does not match", res['next_action'])

    def test_resume_with_workspace_matches_the_workspace(self):
        # The host resumes a worker only in its own directory (the bridge's worker_workspace_mismatch), so with
        # --workspace that directory is the workspace.
        import roll
        _, parent = self.dirs()
        ws = self.root / 'ws'
        ws.mkdir()
        self.make_worker('agent-r', doc={'cwd': str(ws)})
        canon = host_placement.canonical_dir(str(ws))
        task = roll.build_task(self.args(self.brief_file(), resume='agent-r', workspace=str(ws)), '# Task\n', 'parent-a', [])
        self.assertEqual(task['cwd'], canon)
        self.restrict('claude-lab', canon)
        res, code = self.resume(parent, workspace=str(ws), access='write')
        self.assertEqual((res['status'], code), ('launched', 0), res.get('next_action'))

    def named(self):
        return dict(provider='claude-lab', account='claude-lab', model='claude-sonnet-5-5', basis='User asked for this worker')

    def test_an_unknown_worker_directory_on_a_restricted_account_is_refused_for_a_plain_and_a_named_resume(self):
        # Rule: cwd_regex is the user's own boundary; neither a named route nor a named worker reaches past it, so a
        # worker whose directory is not known on this machine is not resumed on a restricted account
        # (roll._auto_resumable refuses the same state).
        import dispatch_registry
        for label, record, over in (('record names no cwd', True, {}), ('record gone', False, {}),
                                    ('named route', True, self.named())):
            with self.subTest(label):
                self.setUp()
                _, parent = self.dirs()
                self.make_worker('agent-r')            # neither its Paseo record nor its registry rows name a directory
                if not record:
                    (self.root / '.paseo' / 'agents' / 'workspace' / 'agent-r.json').unlink()
                self.restrict('claude-lab', 'D:/elsewhere')
                res, code = self.resume(parent, **over)
                with dispatch_registry.transaction() as connection:
                    self.assertFalse([r for r in dispatch_registry.rows(connection, 'dispatches')
                                      if r.get('operation') == 'resume'])
                self.assertEqual((res['status'], code), ('blocked', 2), res.get('next_action'))
                self.assertEqual(tra.full(res)['error'], 'resume_worker_directory_unknown')
                for part in ("worker 'agent-r'", 'not known on this machine', 'claude-lab', 'cwd_regex', 'Omit --resume'):
                    self.assertIn(part, res['next_action'])

    def test_an_unknown_worker_directory_without_a_restriction_launches_with_the_assumption(self):
        import dispatch_registry
        _, parent = self.dirs()
        self.make_worker('agent-r')
        res, code = self.resume(parent)
        self.assertEqual((res['status'], code), ('launched', 0), res.get('next_action'))
        said = [a for a in tra.full(res).get('assumptions', []) if 'agent-r' in a and 'not known' in a]
        self.assertTrue(said, tra.full(res).get('assumptions'))
        self.assertIsNone(dispatch_registry.inspect(tra.full(res)['worker']['dispatch_id']).get('cwd'))

    def test_the_registry_directory_of_a_worker_with_no_paseo_record_is_matched_and_carried(self):
        # The directory is the registry's answer for the worker (dispatch_registry.reusable_workers: the Paseo record's
        # cwd, else its latest row's, else the request's), the one roll names where it says the worker works.
        import dispatch_registry
        _, parent = self.dirs()
        self.make_worker('agent-r')
        (self.root / '.paseo' / 'agents' / 'workspace' / 'agent-r.json').unlink()
        with dispatch_registry.transaction() as connection:
            for row in dispatch_registry.rows(connection, 'dispatches'):
                if row.get('agent_id') == 'agent-r':
                    row['cwd'] = str(parent)
                    dispatch_registry.save(connection, row)
        self.restrict('claude-lab', parent)
        res, code = self.resume(parent)
        self.assertEqual((res['status'], code), ('launched', 0), res.get('next_action'))
        row = dispatch_registry.inspect(tra.full(res)['worker']['dispatch_id'])
        self.assertEqual(row.get('cwd') and cwd_policy.normalized(row['cwd']), cwd_policy.normalized(parent))
        self.restrict('claude-lab', 'D:/elsewhere')
        res, code = self.resume(parent, task_id='t-r2')
        self.assertEqual((res['status'], code), ('blocked', 2), res.get('next_action'))
        self.assertIn(f"the worker's directory {cwd_policy.normalized(parent)} does not match", res['next_action'])

    def test_a_caller_task_cannot_claim_an_unread_directory(self):
        import task_intake
        with self.assertRaises(ValueError) as refused:
            task_intake.check_caller_task({'objective': 'x', 'cwd_unread': True})
        self.assertIn('cwd_unread', str(refused.exception))

    def test_an_automatic_resume_needs_the_worker_directory_inside_the_restriction(self):
        # An automatic resume is the router's choice: the plan matched the directory a new worker would get, and the
        # worker it resumes instead runs in its own, which its account's cwd_regex must match too.
        import dispatch_registry
        import roll
        home, parent = self.dirs()
        self.make_worker('agent-r', doc={'cwd': str(home)})
        worker = next(w for w in dispatch_registry.reusable_workers('parent-a') if w['agent_id'] == 'agent-r')
        policy = host_policy.load_policy()
        self.restrict('claude-lab', parent)
        with patch.object(account_fleet, 'load_accounts', return_value=self.accounts):
            self.assertFalse(roll._auto_resumable(worker, policy, accounts=self.accounts))
            self.restrict('claude-lab', home)
            self.assertTrue(roll._auto_resumable(worker, policy, accounts=self.accounts))


class HerdrUndefinedProvider(Base):
    """Close-out nit on finding 5: a provider with no definition is not a harness."""

    def test_a_provider_with_no_definition_is_said_as_such(self):
        why = host_herdr.cannot_run({'provider': 'no-such-provider', 'model': 'm', 'id': 'b'})
        self.assertIn("cannot tell which harness runs provider 'no-such-provider'", why)
        self.assertNotIn('no-such-provider harness', why)


class CompactWarnings(unittest.TestCase):
    """Review of the record branch, close-out finding B: no warning is dropped from the short answer silently."""

    def test_the_short_answer_says_how_many_more_warnings_the_details_file_holds(self):
        import roll
        warnings = [f'warning {i}' for i in range(7)]
        out = roll.compact({'route': {'warnings': warnings}}, 'launched', 'D:/work/details.json')
        self.assertEqual(out['warnings'][:4], warnings[:4])
        self.assertIn('3 more', out['warnings'][4])
        self.assertIn('D:/work/details.json', out['warnings'][4])
        few = roll.compact({'route': {'warnings': warnings[:4]}}, 'launched', 'D:/work/details.json')
        self.assertEqual(few['warnings'], warnings[:4])
        one = roll.compact({'route': {'warnings': warnings[:5]}}, 'launched', 'D:/work/details.json')
        self.assertIn('1 more warning is in', one['warnings'][4])

    def test_with_no_details_file_every_warning_is_given(self):
        # A dry run writes no details file (roll.run): nothing holds the rest, so nothing is cut.
        import roll
        warnings = [f'warning {i}' for i in range(9)]
        out = roll.compact({'route': {'warnings': warnings}}, 'dry_run', None)
        self.assertEqual(out['warnings'], warnings)


if __name__ == '__main__':
    unittest.main()
