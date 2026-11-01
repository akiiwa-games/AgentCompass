"""External hc boundary fixtures; real portable-runtime evidence is recorded separately."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import time
from unittest.mock import patch
from types import SimpleNamespace

import host_herdr as hh
from test_native_hosts import parent


class HerdrContextTests(unittest.TestCase):
    def test_terminal_is_parent_before_provider_and_inherited_router_context(self):
        self.assertEqual(parent({'HC_TERMINAL_ID': 't7', 'CODEX_THREAD_ID': 'provider-thread',
                                 'ROUTER_PARENT_AGENT_ID': 'old-parent'}), ('t7', 'herdr', 't7'))

    def test_nested_paseo_keeps_ownership(self):
        self.assertEqual(parent({'HC_TERMINAL_ID': 't7', 'PASEO_AGENT_ID': 'actual'}),
                         ('actual', 'paseo', 'actual'))

    def test_mismatching_terminal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'actual HC_TERMINAL_ID'):
            parent({'HC_TERMINAL_ID': 't7'}, parent_id='t9')

    def test_cindy_never_uses_askpass_as_an_address(self):
        with self.assertRaisesRegex(ValueError, 'askpass route is not a session ID'):
            parent({'CINDY_INPUT_HANDOFF_ROUTE': 'opaque', 'CODEX_THREAD_ID': 'wrong'})


class HerdrAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'ROUTER_STATE_DIR': str(self.root), 'HC_SESSION': 'fixture'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.prompt = self.root / 'full.md'
        self.prompt.write_bytes(('完整任务\r\n' * 10000).encode())
        self.out = {'parent_id': 't1', 'parent_host': 'herdr', 'parent_address': 't1'}
        self.bundle = {'provider': 'codex', 'account': 'codex', 'model': 'gpt-test',
                       'effort': 'high', 'modeId': 'full-access'}
        self.term = {'address': 't2', 'name': None, 'state': 'live', 'parent': 't1'}

    def test_real_cli_argument_boundary_and_repeat_uses_single_launch(self):
        calls = []
        def boundary(*argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'run':
                self.term['name'] = argv[argv.index('--name') + 1]
                return {'terminal': self.term, 'first_message': None}
            return {'terminal': {'address': 't1', 'state': 'live'}} if argv[0] == 'get' else {'running': True}
        with patch.object(hh, 'hc', side_effect=boundary), patch.object(hh, 'interactive', return_value=(['codex'], {})):
            first, code = hh.launch(self.out, self.bundle, self.prompt, 'task', object())
            second, _ = hh.launch(self.out, self.bundle, self.prompt, 'task', object())
        self.assertEqual(code, 0)
        self.assertTrue(first['launched'])
        self.assertEqual(first['worker'], second['worker'])
        starts = [c for c in calls if c[0] == 'run']
        self.assertEqual(len(starts), 1)
        self.assertIn('--bg', starts[0])
        self.assertIn('--parent', starts[0])
        self.assertIn('herdr', starts[0])
        self.assertNotIn('--outlive-parent', starts[0])
        request = json.loads(Path(first['worker']['request']).read_text())
        self.assertEqual(Path(request['prompt_file']).read_bytes(), self.prompt.read_bytes())
        self.assertNotIn('scripts', ' '.join(first['worker']['continuation_argv']))

    def test_timeout_is_unknown_and_does_not_resend(self):
        def boundary(*argv, **kwargs):
            if argv[0] == 'run':
                raise TimeoutError('reply lost')
            return {'terminal': {'state': 'live'}} if argv[0] == 'get' else {}
        with patch.object(hh, 'hc', side_effect=boundary), patch.object(hh, 'interactive', return_value=([], {})):
            result, _ = hh.launch(self.out, self.bundle, self.prompt, 'task', object())
            again, _ = hh.launch(self.out, self.bundle, self.prompt, 'task', object())
        self.assertIsNone(result['launched'])
        self.assertIsNone(again['launched'])

    def test_concurrent_process_lock_allows_only_one_external_start(self):
        calls=[]
        def boundary(*argv,**kwargs):
            if argv[0]=='run':
                calls.append(argv)
                return {'address':'t2'}
            return {'state':'live'} if argv[0]=='get' else {}
        with patch.object(hh,'hc',side_effect=boundary), patch.object(hh,'interactive',return_value=([],{})):
            with ThreadPoolExecutor(max_workers=4) as pool:
                replies=list(pool.map(lambda _:hh.launch(self.out,self.bundle,self.prompt,'concurrent',object()),range(4)))
        self.assertEqual(len(calls),1)
        self.assertTrue(all(reply[0]['launched'] for reply in replies))
        for file in hh.runs_dir().glob('*.json'):
            json.loads(file.read_text())

    def test_observed_program_and_exit_states_preserve_read_completeness(self):
        path=self.root/'request.json'
        for program,terminal,exit_code,expected in [('done','live',None,'completed'),('blocked','live',None,'needs_input'),
                ('error','live',None,'failed'),(None,'finished',0,'completed'),(None,'finished',9,'failed')]:
            hh.save(path,{'version':1,'state':'launched','name':'ac-fixture','parent':'t1','hc_session':'fixture','terminal':'t2'})
            term={'address':'t2','state':terminal,'status':{'state':program} if program else None,
                  'exit_code':exit_code,'name':'ac-fixture','parent':'t1'}
            with patch.object(hh,'hc',return_value=term),patch.object(hh,'rpc_read',return_value={'text':'all retained text','complete':False,'how':'history'}):
                reply=hh.inspect_request(path)
            self.assertEqual(reply['status'],expected)
            self.assertFalse(reply['output_complete'])
            self.assertIn('full report artifact',reply['next_action'])

    def test_missing_or_offline_host_creates_no_request(self):
        with patch.object(hh, 'hc', side_effect=RuntimeError('offline')), patch.object(hh, 'interactive', return_value=([], {})):
            result, code = hh.launch(self.out, self.bundle, self.prompt, 'task', object())
        self.assertFalse(result['launched'])
        self.assertEqual(code, 2)
        self.assertEqual(list(hh.runs_dir().glob('*.json')), [])

    def test_exact_interactive_codex_mapping_and_no_daemon(self):
        import roll
        with patch.object(roll, 'provider_definition', return_value=('codex', {'CODEX_HOME': 'account-two'})), \
             patch.object(roll, 'cli_command', return_value=['codex']):
            argv, env = hh.interactive(self.bundle, object())
        self.assertIn('--no-daemon', argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertEqual(env['CODEX_HOME'], 'account-two')

    def test_unsupported_permission_cannot_be_silently_widened(self):
        import roll
        with patch.object(roll, 'provider_definition', return_value=('codex', {})):
            with self.assertRaisesRegex(ValueError, 'permission mode'):
                hh.interactive(dict(self.bundle, modeId='read-only'), object())

    def test_claude_and_pi_interactive_modes_are_not_print_modes(self):
        import roll
        for family,mode in [('claude','bypassPermissions'),('pi','full-access')]:
            bundle=dict(self.bundle,provider=family,account=family,modeId=mode)
            with patch.object(roll,'provider_definition',return_value=(family,{})), \
                 patch.object(roll,'cli_command',return_value=[family]):
                argv,_=hh.interactive(bundle,object())
            self.assertNotIn('-p',argv)
            self.assertNotIn('--print',argv)
            self.assertIn('high',argv)
            if family=='claude': self.assertIn('bypassPermissions',argv)
            else: self.assertIn('openai-codex/gpt-test',argv)

    def test_cleanup_rejects_terminal_owned_by_someone_else(self):
        self.term.update(name='ac-wrong', parent='someone-else')
        path = self.root / 'req.json'
        hh.save(path, {'version': 1, 'state': 'unknown', 'name': 'ac-expected', 'parent': 't1', 'hc_session': 'fixture'})
        with patch.object(hh, 'hc', return_value={'terminal': self.term}) as boundary:
            with self.assertRaisesRegex(ValueError, 'ownership'):
                hh.inspect_request(path, cleanup=True)
        self.assertEqual(boundary.call_count, 1)

    def test_unix_retains_exec_replacement_instead_of_a_supervisor_child(self):
        import route_judge as judge
        request=self.root/'unix.json'
        hh.save(request,{'version':1,'state':'launched','name':'ac-fixture','hc_session':'fixture',
            'parent':'parent-fixture','bundle':{},'prompt_file':str(self.prompt),'cwd':str(self.root)})
        exec_call=__import__('unittest.mock',fromlist=['Mock']).Mock(side_effect=RuntimeError('exec replacement'))
        proxy=SimpleNamespace(name='posix',environ=os.environ,chdir=lambda _:None,execvpe=exec_call)
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'fixture'}),patch.object(hh,'os',proxy), \
             patch.object(hh,'interactive',return_value=([sys.executable],{})), \
             patch.object(judge,'subscription_env',return_value={}),patch.object(hh.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError,'exec replacement'):hh.worker(request)
            spawn.assert_not_called()
        self.assertEqual(exec_call.call_args.args[0],sys.executable)


@unittest.skipUnless(os.name=='nt','Windows worker lifetime contract')
class WindowsWorkerLifetimeTests(unittest.TestCase):
    def test_actual_absolute_fixture_stays_live_until_exit_and_propagates_status(self):
        # No hc/provider lookup: the fixture directly supplies an absolute
        # Python argv at the adapter boundary. HC_TERMINAL_ID is fixture data.
        for exit_code in (0,17):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory(prefix='herdr worker ') as tmp:
                root=Path(tmp)
                child=root/'absolute fixture child.py'
                ready=root/'ready.json'
                release=root/'release'
                child.write_text("import json,os,pathlib,sys,time\np=pathlib.Path(sys.argv[1]); "
                    "(p/'ready.json').write_text(json.dumps({'pid':os.getpid(),'executable':sys.executable})); "
                    "limit=time.monotonic()+8\nwhile not (p/'release').exists() and time.monotonic()<limit: time.sleep(.02)\n"
                    "sys.exit(int(sys.argv[2]))\n",encoding='utf-8')
                request=root/'request.json'
                hh.save(request,{'version':1,'state':'launched','name':'ac-fixture','hc_session':'fixture',
                    'parent':'parent-fixture','bundle':{},'prompt_file':str(root/'task.md'),'cwd':tmp})
                (root/'task.md').write_text('Complete fixture task',encoding='utf-8')
                wrapper=root/'wrapper.py'
                wrapper.write_text("import sys,os\nsys.path.insert(0,sys.argv[1])\nimport host_herdr as h,route_judge as j\n"
                    "h.interactive=lambda bundle,judge: ([sys.executable,sys.argv[2],sys.argv[3],sys.argv[4]],{})\n"
                    "j.subscription_env=lambda: dict(os.environ)\n"
                    "raise SystemExit(h.main(['worker','--request',sys.argv[5]]))\n",encoding='utf-8')
                env={key:value for key,value in os.environ.items() if key in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATHEXT')}
                env.update(HC_SESSION='fixture',HC_TERMINAL_ID='fixture-terminal',HOME=tmp,USERPROFILE=tmp,
                    ROUTER_PROVIDER_HOME=tmp,ROUTER_STATE_DIR=str(root/'state'),PATH='')
                with (root/'stdout.txt').open('wb') as stdout,(root/'stderr.txt').open('wb') as stderr:
                    process=subprocess.Popen([sys.executable,'-X','utf8',str(wrapper),str(Path(hh.__file__).parent),
                        str(child),tmp,str(exit_code),str(request)],env=env,stdout=stdout,stderr=stderr,
                        stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        deadline=time.monotonic()+5
                        while not ready.exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(.02)
                        self.assertTrue(ready.exists(),(root/'stderr.txt').read_text(encoding='utf-8'))
                        self.assertEqual(json.loads(ready.read_text())['executable'],sys.executable)
                        time.sleep(.3)
                        self.assertIsNone(process.poll(),'hc supervisor exited before its actual child')
                        release.touch()
                        self.assertEqual(process.wait(timeout=10),exit_code)
                    finally:
                        release.touch()
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=10)

    def test_interrupted_wait_cleans_only_the_owned_child(self):
        import route_judge as judge
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            request=root/'request.json'
            hh.save(request,{'version':1,'state':'launched','name':'ac-fixture','hc_session':'fixture',
                'parent':'parent-fixture','bundle':{},'prompt_file':str(root/'task.md'),'cwd':tmp})
            process=__import__('unittest.mock',fromlist=['Mock']).Mock()
            process.wait.side_effect=KeyboardInterrupt
            previous=Path.cwd()
            try:
                with patch.dict(os.environ,{'HC_SESSION':'fixture','HC_TERMINAL_ID':'fixture'}), \
                     patch.object(hh,'interactive',return_value=([sys.executable],{})), \
                     patch.object(judge,'subscription_env',return_value={}), \
                     patch.object(judge,'stop_process') as stop, \
                     patch.object(hh.subprocess,'Popen',return_value=process):
                    with self.assertRaises(KeyboardInterrupt):hh.worker(request)
                    stop.assert_called_once_with(process)
            finally:os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
